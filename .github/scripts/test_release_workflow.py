import io
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import monorepo
import release_workflow as release


class FakeGitHub:
    def __init__(self, root, cycle):
        self.root = root
        self.repository = cycle.repository
        self.runs = {
            cycle.source_run_id: {
                "id": cycle.source_run_id, "event": "push", "path": release.WORKFLOW,
                "head_sha": cycle.merge_sha, "head_branch": cycle.branch, "run_attempt": 1,
                "repository": {"full_name": cycle.repository},
            },
        }
        self.source_pr = {
            "number": cycle.source_pr, "merged_at": "2026-01-01T00:00:00Z",
            "merge_commit_sha": cycle.merge_sha,
            "base": {"ref": cycle.branch, "repo": {"full_name": cycle.repository}},
        }
        self.jobs = {
            cycle.source_run_id: [
                {
                    "id": index, "name": f"CI / {name}", "run_attempt": 1,
                    "status": "completed", "conclusion": "success", "steps": [],
                }
                for index, name in enumerate(release.CI_JOBS, 1)
            ],
        }
        self.artifacts = []
        self.plans = {}
        self.prs = []
        self.releases = {}
        self.writes = []

    def workflow_run(self, run_id):
        return self.runs[run_id]

    def pages(self, path, key=None):
        if path.split("?")[0].endswith("/actions/artifacts"):
            return self.artifacts
        if "/jobs?" in path:
            run_id = int(path.split("/runs/")[1].split("/")[0])
            return self.jobs.get(run_id, [])
        if "/commits/" in path and path.endswith("/pulls"):
            return [self.source_pr]
        if "/pulls?" in path:
            return self.prs
        raise AssertionError(f"Unexpected API listing: {path}")

    def api(self, path, method="GET", data=None):
        if method == "GET" and path.endswith(f"/pulls/{self.source_pr['number']}"):
            return self.source_pr
        if method == "POST" and path.endswith("/releases"):
            self.writes.append((path, data))
            record = {**data, "html_url": f"https://github.com/{self.repository}/releases/1"}
            self.releases[data["tag_name"]] = record
            return record
        if method == "POST" and path.endswith("/pulls"):
            self.writes.append((path, data))
            pr = {
                **data, "number": 30, "state": "open", "merged_at": None,
                "head": {"sha": release.remote_head(self.root, data["head"]), "ref": data["head"]},
                "base": {"ref": data["base"]},
                "html_url": f"https://github.com/{self.repository}/pull/30",
            }
            self.prs.append(pr)
            return pr
        raise AssertionError(f"Unexpected API call: {method} {path}")

    def optional(self, path):
        return self.releases.get(unquote(path.split("/releases/tags/")[1]))

    def download(self, artifact):
        return self.plans[artifact["id"]]

    def checkpoint(self, plan, origin=None, expired=False):
        artifact_id = len(self.artifacts) + 1
        origin = origin or plan.cycle.source_run_id
        self.artifacts.append({
            "id": artifact_id,
            "name": (
                f"release-state-{plan.cycle.action}-{plan.cycle.merge_sha}-"
                f"{artifact_id}-{plan.stage}"
            ),
            "expired": expired, "workflow_run": {"id": origin},
        })
        self.plans[artifact_id] = plan


class ReleaseFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="release-workflow-")
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "work with spaces"
        self.remote = self.parent / "remote.git"
        release.git(self.parent, "init", "--bare", "--quiet", str(self.remote))
        release.git(self.parent, "init", "--quiet", "--initial-branch=main", str(self.root))
        for name, value in {
            "user.name": "Test", "user.email": "test@example.invalid",
            "commit.gpgsign": "false", "tag.gpgsign": "false",
            "core.autocrlf": "false", "core.hooksPath": str(self.parent / "no-hooks"),
        }.items():
            release.git(self.root, "config", "--local", name, value)
        release.git(self.root, "remote", "add", "origin", str(self.remote))
        for action in ("dummy", "other"):
            directory = self.root / action
            directory.mkdir()
            (directory / "action.yml").write_text("name: test\n", encoding="utf-8", newline="\n")
            (directory / "main.sh").write_text("echo unchanged\n", encoding="utf-8", newline="\n")
            (directory / "RELEASE-NOTES.md").write_text(
                f"# Release Notes - {action}\n\n## v0.0.1\n\nOld notes.\n",
                encoding="utf-8", newline="\n",
            )
        (self.root / ".semrelease").mkdir()
        (self.root / ".semrelease" / "this_release").write_text(
            "# Shared staging file\n\n#!dummy\n- feat(action): do the thing\n\n"
            "#!other/v2.0.0\n- fix(action): preserve this section\n",
            encoding="utf-8", newline="\n",
        )
        release.git(self.root, "add", ".")
        release.git(self.root, "commit", "--quiet", "-m", "Fixture")
        self.merge = release.git(self.root, "rev-parse", "HEAD")
        release.git(self.root, "branch", "release/dummy")
        release.git(self.root, "push", "--quiet", "origin", "main", "release/dummy")
        self.cycle = release.Cycle(
            "example/actions", "dummy", "release/dummy", self.merge, 17, 7, "main",
        )
        self.client = FakeGitHub(self.root, self.cycle)
        self.path = self.parent / "release-state.json"
        event = self.parent / "event.json"
        event.write_text(json.dumps({"created": False, "deleted": False, "forced": False}))
        self.environment = {
            "GITHUB_REPOSITORY": self.cycle.repository, "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event),
            "GITHUB_REF": "refs/heads/release/dummy", "GITHUB_RUN_ID": "17",
            "GITHUB_RUN_ATTEMPT": "1", "DEFAULT_BRANCH": "main",
            "CAPTURED_BRANCH": self.cycle.branch, "CAPTURED_SHA": self.merge,
            "ACTION_NAME": "dummy", "RELEASE_BRANCH": self.cycle.branch,
            "MERGE_SHA": self.merge, "SOURCE_RUN_ID": "17", "SOURCE_PR": "7",
            "RELEASE_RESULT": "SUCCESS", "RELEASE_VERSION": "0.1.0",
            "RELEASE_NOTES": "### Features\n\n- New functionality \u2728",
            "CI_RUN_ID": "17", "CI_RUN_ATTEMPT": "1", "CI_TARGET_SHA": self.merge,
            "GITHUB_OUTPUT": str(self.parent / "outputs"),
            "GITHUB_STEP_SUMMARY": str(self.parent / "summary"),
        }
        environment = patch.dict(os.environ, self.environment)
        environment.start()
        self.addCleanup(environment.stop)

    def calculate(self):
        with patch("monorepo.release_preview"):
            release.calculate(self.root, self.client, self.path)
        return release.load(self.path, self.cycle)

    def prepare(self):
        self.calculate()
        release.prepare(self.root, self.client, self.path)
        return release.load(self.path, self.cycle)

    def freeze(self):
        plan = self.prepare()
        self.client.checkpoint(plan)
        release.push_notes(self.root, self.client, self.path)
        release.freeze(self.root, self.client, self.path)
        return release.load(self.path, self.cycle)

    def remote_tag(self, name):
        value = release.git(self.root, "ls-remote", "--refs", "origin", f"refs/tags/{name}")
        return value.split()[0] if value else None


class HistoryTests(unittest.TestCase):
    def test_preserves_title_and_prepends_newest_entry_before_older_history(self):
        original = b"# Title\n\n## v1.0.0\n\nOlder.\n"
        updated = release.prepend_history(original, "1.1.0", "First\n\n  Indented.")
        self.assertEqual(
            updated, b"# Title\n\n## v1.1.0\n\nFirst\n\n  Indented.\n\n## v1.0.0\n\nOlder.\n",
        )

    def test_existing_matching_version_is_noop_not_a_substring_match(self):
        original = b"# Title\n\n## v1.0.0\n\nExisting.\n"
        self.assertEqual(release.prepend_history(original, "1.0.0", "Existing."), original)
        updated = release.prepend_history(original, "1.0.1", "New.")
        self.assertEqual(updated.count(b"## v1.0.1\n"), 1)
        self.assertEqual(updated.count(b"## v1.0.0\n"), 1)

    def test_conflicting_or_duplicate_headings_fail(self):
        for original in (
            b"# Title\n\n## v1.0.0\n\nDifferent.\n",
            b"# Title\n\n## v1.0.0\n\nSame.\n\n## v1.0.0\n\nSame.\n",
        ):
            with self.subTest(original=original):
                with self.assertRaises(ValueError):
                    release.prepend_history(original, "1.0.0", "Same.")

    def test_ignores_canonical_heading_inside_fenced_example(self):
        original = b"# Title\n\n## v0.9.0\n\n```markdown\n## v1.0.0\n```\n"
        updated = release.prepend_history(original, "1.0.0", "New.")
        self.assertEqual(updated.count(b"## v1.0.0\n"), 2)
        self.assertEqual(release.prepend_history(updated, "1.0.0", "New."), updated)

    def test_rejects_blank_title_empty_notes_and_ambiguous_generated_headings(self):
        for content, notes in ((b"", "Notes"), (b"\n", "Notes"), (b"# T\n", " \n"), (b"# T\n", "## v1.0.0\n")):
            with self.subTest(content=content, notes=notes):
                with self.assertRaises(ValueError):
                    release.prepend_history(content, "1.0.0", notes)

    def test_preserves_bom_unicode_and_title_without_final_newline(self):
        updated = release.prepend_history(b"\xef\xbb\xbf# Title", "1.0.0", "\u00e9")
        self.assertEqual(updated, b"\xef\xbb\xbf# Title\n\n## v1.0.0\n\n\xc3\xa9\n")


class CalculationTests(ReleaseFixture):
    def test_records_strict_outputs_and_ci_provenance(self):
        plan = self.calculate()
        self.assertEqual(plan.stage, "calculated")
        self.assertEqual(plan.ci_target_sha, self.merge)
        self.assertEqual(plan.ci_run_id, 17)
        self.assertEqual(plan.requested_version, "")
        self.assertEqual(plan.release_notes, self.environment["RELEASE_NOTES"])
        self.assertEqual(release.Plan.decode(self.path.read_bytes()), plan)

    def test_incomplete_or_unsuccessful_calculations_never_create_state(self):
        for variable, value in (
            ("RELEASE_RESULT", "SKIPPED"), ("RELEASE_RESULT", "FAILED"),
            ("RELEASE_VERSION", ""), ("RELEASE_VERSION", "v1.0.0"),
            ("RELEASE_VERSION", "01.0.0"), ("RELEASE_VERSION", "1.0.0-rc.1"),
            ("RELEASE_NOTES", ""), ("RELEASE_NOTES", " \n"),
            ("CI_TARGET_SHA", "f" * 40),
        ):
            with self.subTest(variable=variable, value=value):
                with patch.dict(os.environ, {variable: value}), self.assertRaises(ValueError):
                    self.calculate()
                self.assertFalse(self.path.exists())

    def test_fixed_header_must_match_calculated_version(self):
        plan = self.calculate()
        with self.assertRaisesRegex(ValueError, "fixed"):
            replace(plan, requested_version="9.0.0").validate()

    def test_missing_or_failed_native_ci_cannot_authorize_a_calculation(self):
        for conclusion in ("failure", "cancelled", "skipped"):
            with self.subTest(conclusion=conclusion):
                self.client.jobs[17][-1]["conclusion"] = conclusion
                with self.assertRaisesRegex(ValueError, "CI evidence"):
                    self.calculate()
                self.assertFalse(self.path.exists())

    def test_failed_jobs_rerun_reuses_successes_from_earlier_attempts(self):
        self.client.jobs[17][-1]["conclusion"] = "failure"
        self.client.jobs[17].append({
            **self.client.jobs[17][-1], "id": 9, "run_attempt": 2, "conclusion": "success",
        })
        self.assertEqual(release.ci_evidence(self.client, 17, 2), 2)
        self.assertIsNone(release.ci_evidence(self.client, 17, 1))

    def test_inputs_are_scoped_and_sandbox_legacy_global_version_directives(self):
        with patch("release_workflow.emit") as emit:
            release.inputs(self.root, self.client, self.path)
        outputs = emit.call_args.kwargs
        self.assertTrue(outputs["needs_calculation"])
        self.assertEqual(outputs["version"], "")
        self.assertEqual(outputs["tag_prefix"], "dummy/v")
        self.assertEqual(json.loads(outputs["change_log_json"]), "- feat(action): do the thing")
        self.assertEqual(
            (self.root / ".semrelease" / "this_release").read_text(encoding="utf-8"),
            "- feat(action): do the thing\n",
        )
        self.assertIn("#!other", release.run(self.root, "git", "show", f"{self.merge}:.semrelease/this_release").decode())

    def test_saved_calculation_is_reused_even_after_release_baseline_changes(self):
        plan = self.calculate()
        self.client.checkpoint(plan)
        release.restore(self.root, self.client, self.path)
        with patch("release_workflow.emit") as emit:
            release.inputs(self.root, self.client, self.path)
        self.assertFalse(emit.call_args.kwargs["needs_calculation"])
        with self.assertRaisesRegex(ValueError, "reused"):
            self.calculate()

    def test_plan_rejects_wrong_types_schemas_and_cycles(self):
        plan = self.calculate()
        for changes in (
            {"schema": 2}, {"ci_run_id": True}, {"release_notes": None},
            {"ci_target_sha": "f" * 40}, {"changelog_digest": "bad"},
            {"timestamp": "2026-01-01T00:00:00"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(plan, **changes).validate()
        with self.assertRaisesRegex(ValueError, "different cycle"):
            release.load(self.path, replace(self.cycle, source_pr=8))
        for content in (b"null", b"[]", b"{}", b"{"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                release.Plan.decode(content)


class ContextTests(ReleaseFixture):
    def test_captures_pinned_merge_and_runs_ci_when_no_successful_evidence_exists(self):
        self.client.jobs[17] = []
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        values = emit.call_args.kwargs
        self.assertTrue(values["eligible"])
        self.assertTrue(values["run_ci"])
        self.assertEqual(values["merge_sha"], self.merge)
        self.assertEqual(values["source_pr"], 7)
        self.assertEqual(values["trusted_sha"], self.merge)

    def test_branch_creation_deletion_forced_or_direct_push_is_not_a_release(self):
        event = Path(self.environment["GITHUB_EVENT_PATH"])
        for flag in ("created", "deleted", "forced"):
            event.write_text(json.dumps({flag: True}))
            with self.subTest(flag=flag), patch("release_workflow.emit") as emit:
                release.context(self.root, self.client, self.path)
                emit.assert_called_once_with(eligible=False)
        event.write_text("{}")
        self.client.source_pr["merged_at"] = None
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
            emit.assert_called_once_with(eligible=False)

    def test_saved_state_skips_second_ci_on_retry(self):
        self.client.checkpoint(self.calculate())
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        self.assertFalse(emit.call_args.kwargs["run_ci"])
        self.assertEqual(emit.call_args.kwargs["ci_run_id"], 17)

    def test_manual_recovery_uses_original_sha_not_dispatch_sha(self):
        self.client.checkpoint(self.calculate())
        environment = {
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
            "RECOVERY_ACTION": "dummy", "RECOVERY_RUN_ID": "17",
            "CAPTURED_SHA": "f" * 40, "CAPTURED_BRANCH": "main",
        }
        with patch.dict(os.environ, environment), patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        self.assertEqual(emit.call_args.kwargs["merge_sha"], self.merge)
        self.assertFalse(emit.call_args.kwargs["run_ci"])
        with patch.dict(os.environ, {**environment, "GITHUB_REF": "refs/heads/dev/dummy"}):
            with self.assertRaisesRegex(ValueError, "default branch"):
                release.context(self.root, self.client, self.path)

    def test_open_promotion_blocks_another_cycle(self):
        self.client.prs = [{"state": "open", "body": "Different cycle", "merged_at": None}]
        with self.assertRaisesRegex(ValueError, "previous promotion"):
            release.context(self.root, self.client, self.path)

    def test_expired_state_fails_instead_of_recalculating(self):
        self.client.checkpoint(self.calculate(), expired=True)
        with self.assertRaisesRegex(ValueError, "expired"):
            release.context(self.root, self.client, self.path)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            release.restore(self.root, self.client, self.path)

    def test_deleted_state_is_detected_from_upload_step_history(self):
        self.client.jobs[17][0]["steps"] = [{
            "name": "Save calculated release plan", "conclusion": "success",
        }]
        with self.assertRaisesRegex(ValueError, "deleted"):
            release.context(self.root, self.client, self.path)

    def test_manual_dispatch_cannot_turn_an_ineligible_push_into_a_release(self):
        self.client.jobs[17] = []
        environment = {
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
            "RECOVERY_ACTION": "dummy", "RECOVERY_RUN_ID": "17",
        }
        with patch.dict(os.environ, environment):
            with self.assertRaisesRegex(ValueError, "never started pinned CI"):
                release.context(self.root, self.client, self.path)

    def test_missing_state_after_notes_commit_cannot_recalculate(self):
        self.prepare()
        release.push_notes(self.root, self.client, self.path)
        with self.assertRaisesRegex(ValueError, "no matching recovery state"):
            release.context(self.root, self.client, self.path)

    def test_noop_notes_publication_with_lost_state_cannot_recalculate(self):
        release.git(self.root, "tag", "dummy/v0.1.0", self.merge)
        release.git(self.root, "push", "--quiet", "origin", "refs/tags/dummy/v0.1.0")
        with self.assertRaisesRegex(ValueError, "already has a release tag"):
            release.context(self.root, self.client, self.path)

    def test_checkpoints_from_untrusted_workflows_or_other_release_branches_are_ignored(self):
        plan = self.calculate()
        for event, branch, path in (
            ("pull_request", "release/dummy", release.WORKFLOW),
            ("push", "release/other", release.WORKFLOW),
            ("push", "release/dummy", ".github/workflows/ci.yml"),
            ("workflow_dispatch", "dev/dummy", release.WORKFLOW),
        ):
            with self.subTest(event=event, branch=branch, path=path):
                self.client.runs[18] = {
                    **self.client.runs[17], "id": 18, "event": event,
                    "head_branch": branch, "path": path,
                }
                self.client.artifacts.clear()
                self.client.checkpoint(plan, origin=18)
                records, _ = release.checkpoints(self.client, "dummy", "main")
                self.assertEqual(records, [])

    def test_conflicting_calculations_or_prepared_commits_stop_recovery(self):
        plan = self.calculate()
        records = [(plan, 1, 17), (replace(plan, version="2.0.0"), 2, 17)]
        with self.assertRaisesRegex(ValueError, "Conflicting calculations"):
            release.select_checkpoint(records, self.cycle)
        release.prepare(self.root, self.client, self.path)
        prepared = release.load(self.path, self.cycle)
        records = [(prepared, 1, 17), (replace(prepared, release_sha="f" * 40), 2, 17)]
        with self.assertRaisesRegex(ValueError, "Conflicting prepared"):
            release.select_checkpoint(records, self.cycle)


class ReleaseIntegrationTests(ReleaseFixture):
    def test_complete_release_preserves_code_other_sections_and_manual_merge(self):
        plan = self.freeze()
        self.assertEqual(
            release.git(self.root, "diff-tree", "--no-commit-id", "--name-only", "-r", plan.release_sha),
            "dummy/RELEASE-NOTES.md",
        )
        self.assertEqual(
            release.git(self.root, "diff-tree", "--no-commit-id", "--name-only", "-r", plan.cleanup_sha),
            ".semrelease/this_release",
        )
        history = release.run(self.root, "git", "show", f"{plan.release_sha}:dummy/RELEASE-NOTES.md")
        self.assertTrue(history.startswith(b"# Release Notes - dummy\n\n## v0.1.0\n"))
        self.assertLess(history.index(b"## v0.1.0"), history.index(b"## v0.0.1"))
        release.publish(self.root, self.client, self.path)
        for tag in ("dummy/v0.1.0", "dummy/v0", "dummy/v0.1"):
            self.assertEqual(self.remote_tag(tag), plan.release_sha)
        self.assertIsNone(self.remote_tag("other/v2.0.0"))
        record = self.client.releases[plan.tag]
        self.assertEqual(record["target_commitish"], plan.release_sha)
        self.assertEqual(record["make_latest"], "false")
        self.assertEqual(record["body"], self.environment["RELEASE_NOTES"])
        release.cleanup(self.root, self.client, self.path)
        cleaned = release.run(self.root, "git", "show", f"{plan.cleanup_sha}:.semrelease/this_release")
        self.assertEqual(cleaned, b"# Shared staging file\n\n#!other/v2.0.0\n- fix(action): preserve this section\n")
        release.promote(self.root, self.client, self.path)
        self.assertEqual(self.client.prs[0]["base"]["ref"], "main")
        self.assertEqual(self.client.prs[0]["head"]["sha"], plan.cleanup_sha)
        self.assertIn(self.cycle.marker, self.client.prs[0]["body"])
        self.assertEqual(release.remote_head(self.root, "main"), self.merge)
        self.assertEqual(release.git(self.root, "rev-parse", "HEAD"), self.merge)
        self.assertEqual([path.rsplit("/", 1)[-1] for path, _ in self.client.writes], ["releases", "pulls"])

    def test_notes_push_network_failure_is_reconciled_without_second_commit(self):
        plan = self.prepare()
        self.client.checkpoint(plan)
        original = release.push_branch

        def lost_response(*args):
            original(*args)
            raise subprocess.CalledProcessError(1, ["git", "push"], stderr=b"Network reset")

        with patch("release_workflow.push_branch", side_effect=lost_response):
            with self.assertRaises(subprocess.CalledProcessError):
                release.push_notes(self.root, self.client, self.path)
        self.assertEqual(release.remote_head(self.root, self.cycle.branch), plan.release_sha)
        release.restore(self.root, self.client, self.path)
        release.prepare(self.root, self.client, self.path)
        self.assertEqual(release.load(self.path, self.cycle), plan)
        with patch("release_workflow.push_branch") as push:
            release.push_notes(self.root, self.client, self.path)
            push.assert_not_called()
        release.freeze(self.root, self.client, self.path)
        content = release.run(self.root, "git", "show", f"{plan.release_sha}:dummy/RELEASE-NOTES.md")
        self.assertEqual(content.count(b"## v0.1.0\n"), 1)

    def test_calculation_only_checkpoint_reconstructs_identical_preparation(self):
        calculation = self.calculate()
        self.client.checkpoint(calculation)
        release.prepare(self.root, self.client, self.path)
        first = release.load(self.path, self.cycle)
        release.push_notes(self.root, self.client, self.path)
        release.save(self.path, calculation)
        release.prepare(self.root, self.client, self.path)
        self.assertEqual(release.load(self.path, self.cycle), first)
        release.push_notes(self.root, self.client, self.path)

    def test_release_creation_network_failure_reuses_tags_and_release(self):
        plan = self.freeze()
        original = self.client.api

        def lost_response(path, method="GET", data=None):
            result = original(path, method, data)
            if method == "POST" and path.endswith("/releases"):
                raise subprocess.CalledProcessError(1, ["gh", "api"], stderr=b"Network reset")
            return result

        with patch.object(self.client, "api", side_effect=lost_response):
            with self.assertRaises(subprocess.CalledProcessError):
                release.publish(self.root, self.client, self.path)
        tag = self.remote_tag(plan.tag)
        release.publish(self.root, self.client, self.path)
        self.assertEqual(self.remote_tag(plan.tag), tag)
        self.assertEqual(len(self.client.writes), 1)

    def test_cleanup_and_promotion_retry_do_not_duplicate_commits_or_prs(self):
        plan = self.freeze()
        release.publish(self.root, self.client, self.path)
        release.cleanup(self.root, self.client, self.path)
        release.cleanup(self.root, self.client, self.path)
        self.assertEqual(release.remote_head(self.root, self.cycle.branch), plan.cleanup_sha)
        release.promote(self.root, self.client, self.path)
        release.promote(self.root, self.client, self.path)
        self.assertEqual(len(self.client.prs), 1)
        self.assertEqual(len(self.client.writes), 2)

    def test_fresh_runner_reconstructs_unpushed_cleanup_from_frozen_plan(self):
        plan = self.freeze()
        fresh = self.parent / "recovery"
        release.git(self.parent, "clone", "--quiet", "--branch", "main", str(self.remote), str(fresh))
        self.client.root = fresh
        release.prepare(fresh, self.client, self.path)
        self.assertEqual(release.load(self.path, self.cycle), plan)
        release.publish(fresh, self.client, self.path)
        release.cleanup(fresh, self.client, self.path)
        self.assertEqual(release.remote_head(fresh, self.cycle.branch), plan.cleanup_sha)

    def test_existing_exact_notes_entry_needs_no_preparation_commit(self):
        plan = self.calculate()
        plan = replace(plan, version="0.0.1", release_notes="Old notes.")
        release.save(self.path, plan)
        release.prepare(self.root, self.client, self.path)
        self.assertEqual(release.load(self.path, self.cycle).release_sha, self.merge)

    def test_unexpected_branch_advance_stops_notes_publication_and_cleanup(self):
        plan = self.prepare()
        moved = release.commit_file(
            self.root, self.merge, "dummy/main.sh", b"echo different\n", plan.timestamp, "Other work\n",
        )
        release.push_branch(self.root, self.cycle.branch, self.merge, moved)
        with self.assertRaisesRegex(ValueError, "advanced"):
            release.push_notes(self.root, self.client, self.path)
        release.save(self.path, replace(plan, stage="frozen"))
        with self.assertRaisesRegex(ValueError, "advanced"):
            release.publish(self.root, self.client, self.path)
        self.assertIsNone(self.remote_tag(plan.tag))

    def test_stale_branch_lease_rejects_push_instead_of_overwriting(self):
        plan = self.prepare()
        moved = release.commit_file(
            self.root, self.merge, "dummy/main.sh", b"other\n", plan.timestamp, "Other\n",
        )
        release.push_branch(self.root, self.cycle.branch, self.merge, moved)
        with self.assertRaises(subprocess.CalledProcessError):
            release.push_branch(self.root, self.cycle.branch, self.merge, plan.release_sha)
        self.assertEqual(release.remote_head(self.root, self.cycle.branch), moved)

    def test_missing_publication_cannot_remove_staged_notes(self):
        plan = self.freeze()
        with self.assertRaisesRegex(ValueError, "Publication"):
            release.cleanup(self.root, self.client, self.path)
        self.assertEqual(release.remote_head(self.root, self.cycle.branch), plan.release_sha)

    def test_conflicting_github_release_is_never_overwritten(self):
        plan = self.freeze()
        release.publish(self.root, self.client, self.path)
        self.client.releases[plan.tag]["body"] = "Different release"
        with self.assertRaisesRegex(ValueError, "conflicts"):
            release.publish(self.root, self.client, self.path)
        self.assertEqual(len(self.client.writes), 1)

    def test_release_without_its_exact_tag_is_not_silently_repaired(self):
        plan = self.freeze()
        self.client.releases[plan.tag] = {
            "tag_name": plan.tag, "draft": False, "prerelease": False,
            "body": plan.release_notes, "html_url": "https://github.com/example/actions/releases/1",
        }
        with self.assertRaisesRegex(ValueError, "without its exact tag"):
            release.publish(self.root, self.client, self.path)
        self.assertIsNone(self.remote_tag(plan.tag))
        self.assertFalse(self.client.writes)

    def test_exact_tag_at_another_commit_is_never_moved(self):
        plan = self.freeze()
        release.git(self.root, "tag", plan.tag, self.merge)
        release.git(self.root, "push", "--quiet", "origin", f"refs/tags/{plan.tag}")
        with self.assertRaisesRegex(ValueError, "different commit"):
            release.publish(self.root, self.client, self.path)
        self.assertEqual(self.remote_tag(plan.tag), self.merge)
        self.assertIsNone(self.remote_tag("dummy/v0"))
        self.assertFalse(self.client.writes)

    def test_cleanup_push_network_failure_is_reconciled(self):
        plan = self.freeze()
        release.publish(self.root, self.client, self.path)
        original = release.push_branch

        def lost_response(*args):
            original(*args)
            raise subprocess.CalledProcessError(1, ["git", "push"], stderr=b"Network reset")

        with patch("release_workflow.push_branch", side_effect=lost_response):
            with self.assertRaises(subprocess.CalledProcessError):
                release.cleanup(self.root, self.client, self.path)
        with patch("release_workflow.push_branch") as push:
            release.cleanup(self.root, self.client, self.path)
            push.assert_not_called()
        self.assertEqual(release.remote_head(self.root, self.cycle.branch), plan.cleanup_sha)

    def test_closed_promotion_requires_manual_reopening_and_never_merges(self):
        self.freeze()
        release.publish(self.root, self.client, self.path)
        release.cleanup(self.root, self.client, self.path)
        release.promote(self.root, self.client, self.path)
        self.client.prs[0]["state"] = "closed"
        with self.assertRaisesRegex(ValueError, "reopen it manually"):
            release.promote(self.root, self.client, self.path)


class ArtifactTests(ReleaseFixture):
    def zip_plan(self, plan, names=("release-state.json",)):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name in names:
                archive.writestr(name, json.dumps(asdict(plan)))
        return stream.getvalue()

    def test_download_verifies_digest_and_single_plan_filename(self):
        plan = self.calculate()
        content = self.zip_plan(plan)
        client = release.GitHub(self.root)
        artifact = {"id": 9, "digest": f"sha256:{release.digest(content)}"}
        with patch("release_workflow.run", return_value=content):
            self.assertEqual(client.download(artifact), plan)
            with self.assertRaisesRegex(ValueError, "digest"):
                client.download({**artifact, "digest": "sha256:" + "0" * 64})
        for names in (("../release-state.json",), ("release-state.json", "extra.txt")):
            content = self.zip_plan(plan, names)
            with patch("release_workflow.run", return_value=content):
                with self.assertRaisesRegex(ValueError, "only"):
                    client.download({"id": 9, "digest": f"sha256:{release.digest(content)}"})

    def test_only_explicit_http_404_is_treated_as_missing_release(self):
        client = release.GitHub(self.root)
        for status in (403, 429, 500):
            error = subprocess.CalledProcessError(1, ["gh"], stderr=f"gh: error (HTTP {status})".encode())
            with patch.object(client, "api", side_effect=error), self.assertRaises(subprocess.CalledProcessError):
                client.optional("repos/example/actions/releases/tags/dummy%2Fv1.0.0")
        error = subprocess.CalledProcessError(1, ["gh"], stderr=b"gh: Not Found (HTTP 404)")
        with patch.object(client, "api", side_effect=error):
            self.assertIsNone(client.optional("missing"))

    def test_pagination_keeps_all_artifacts(self):
        client = release.GitHub(self.root)
        with patch.object(client, "api", side_effect=[
            {"artifacts": list(range(100))}, {"artifacts": [100]},
        ]) as api:
            self.assertEqual(client.pages("artifacts", "artifacts"), list(range(101)))
        self.assertIn("page=2", api.call_args.args[0])

    def test_cli_preserves_command_stderr_without_interpreting_runner_commands(self):
        error = subprocess.CalledProcessError(1, ["git", "push"], stderr="Network failed\n::warning::literal")
        with patch("release_workflow.publish", side_effect=error):
            with patch("sys.argv", ["release_workflow.py", "publish"]):
                with patch("sys.stdout", new_callable=io.StringIO) as output:
                    with self.assertRaises(SystemExit) as exit_code:
                        release.main()
        self.assertEqual(exit_code.exception.code, 1)
        text = output.getvalue()
        self.assertIn("❌ Release workflow failed: Network failed", text)
        self.assertIn("Network failed\n::warning::literal", text)
        self.assertLess(text.index("::stop-commands::"), text.index("Network failed"))


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        from ruamel.yaml import YAML

        yaml = YAML(typ="safe")
        self.workflow = yaml.load((monorepo.ROOT / release.WORKFLOW).read_text(encoding="utf-8"))
        self.ci = yaml.load((monorepo.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        self.jobs = self.workflow["jobs"]
        self.steps = self.jobs["Release"]["steps"]
        self.named = {step.get("name"): step for step in self.steps}

    def test_python_workflows_use_utf8_for_console_messages(self):
        for workflow in (self.workflow, self.ci):
            with self.subTest(name=workflow["name"]):
                self.assertEqual(workflow["env"]["PYTHONIOENCODING"], "utf-8")

    def test_release_trigger_recovery_and_concurrency_are_action_scoped(self):
        self.assertEqual(self.workflow["on"]["push"]["branches"], ["release/**"])
        self.assertEqual(set(self.workflow["on"]["workflow_dispatch"]["inputs"]), {"action", "run-id"})
        self.assertFalse(self.workflow["concurrency"]["cancel-in-progress"])
        self.assertIn("github.ref_name", self.workflow["concurrency"]["group"])
        self.assertIn("inputs.action", self.workflow["concurrency"]["group"])
        self.assertIn("!release/**", self.ci["on"]["push"]["branches"])

    def test_context_precedes_one_pinned_ci_without_release_preview(self):
        context = self.jobs["CaptureContext"]
        self.assertTrue(any(step.get("uses") == "./workflow-context" for step in context["steps"]))
        ci = self.jobs["CI"]
        self.assertEqual(ci["needs"], "CaptureContext")
        self.assertEqual(ci["uses"], "./.github/workflows/ci.yml")
        self.assertEqual(ci["with"], {
            "target-sha": "${{ needs.CaptureContext.outputs.merge-sha }}",
            "target-branch": "${{ needs.CaptureContext.outputs.branch }}",
            "skip-release-preview": True,
        })
        self.assertEqual(self.jobs["Release"]["needs"], ["CaptureContext", "CI"])
        self.assertIn("needs.CaptureContext.result == 'success'", self.jobs["Release"]["if"])
        self.assertIn("needs.CI.result == 'success'", self.jobs["Release"]["if"])
        self.assertIn("needs.CaptureContext.outputs.run-ci == 'false'", self.jobs["Release"]["if"])

    def test_reusable_ci_exposes_actual_target_and_preserves_normal_previews(self):
        call = self.ci["on"]["workflow_call"]
        self.assertFalse(call["inputs"]["skip-release-preview"]["default"])
        self.assertEqual(call["outputs"]["target-sha"]["value"], "${{ jobs.CaptureContext.outputs.target-sha }}")
        capture = self.ci["jobs"]["CaptureContext"]["steps"]
        self.assertEqual(capture[0]["with"]["ref"], "${{ inputs.target-sha || github.sha }}")
        self.assertIn("!inputs.skip-release-preview", self.ci["jobs"]["PreviewRelease"]["if"])

    def test_calculation_checkpoints_notes_freeze_and_publication_are_ordered(self):
        required = [
            "Restore the latest release checkpoint", "Select the pinned action section",
            "Calculate release version and notes", "Validate and record the calculation",
            "Save calculated release plan", "Prepare the notes-only and cleanup commits",
            "Save prepared release plan", "Commit the release-notes update",
            "Verify the committed release plan", "Freeze release plan",
            "Publish immutable release and compatibility aliases",
            "Clean only the published changelog section", "Create or reuse the promotion PR for manual merge",
        ]
        positions = [self.steps.index(self.named[name]) for name in required]
        self.assertEqual(positions, sorted(positions))
        semrelease = self.named["Calculate release version and notes"]
        self.assertTrue(semrelease["with"]["dry-run"])
        self.assertEqual(semrelease["with"]["target-sha"], "${{ env.MERGE_SHA }}")
        self.assertEqual(semrelease["with"]["version"], "${{ steps.inputs.outputs.version }}")
        self.assertEqual(semrelease["with"]["change-log"], "${{ fromJSON(steps.inputs.outputs.change-log-json) }}")
        self.assertIn("needs-calculation == 'true'", semrelease["if"])
        self.assertNotIn("if", self.named["Prepare the notes-only and cleanup commits"])
        self.assertFalse(any(step.get("continue-on-error") for step in self.steps))

    def test_privileged_steps_use_trusted_code_and_repository_scoped_app_token(self):
        self.assertEqual(self.workflow["permissions"], {
            "contents": "read", "actions": "read", "pull-requests": "read",
        })
        checkout = self.named["Checkout trusted automation"]
        self.assertEqual(checkout["with"]["ref"], "${{ needs.CaptureContext.outputs.trusted-sha }}")
        self.assertFalse(checkout["with"]["persist-credentials"])
        token = self.named["Create repository-scoped automation token"]["with"]
        self.assertEqual(token["client-id"], "${{ vars.RELEASE_APP_CLIENT_ID }}")
        self.assertEqual(token["permission-contents"], "write")
        self.assertEqual(token["permission-pull-requests"], "write")
        self.assertNotIn("permission-administration", token)
        for name in (
            "Commit the release-notes update", "Publish immutable release and compatibility aliases",
            "Clean only the published changelog section", "Create or reuse the promotion PR for manual merge",
        ):
            self.assertEqual(self.named[name]["env"]["GH_TOKEN"], "${{ steps.app.outputs.token }}")
        for step in self.steps:
            self.assertNotIn("${{", step.get("run", ""))
            if "uses" in step and not step["uses"].startswith("./"):
                self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()

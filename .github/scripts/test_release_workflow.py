import io
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

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
            "head": {"ref": f"dev/{cycle.action}"},
            "base": {"ref": cycle.branch, "repo": {"full_name": cycle.repository}},
        }
        self.source_prs = {}
        self.jobs = {
            cycle.source_run_id: [
                {
                    "id": index, "name": f"CI / {name}", "run_attempt": 1,
                    "status": "completed", "conclusion": "success", "steps": [],
                }
                for index, name in enumerate(release.CI_JOBS, 1)
            ],
        }
        self.prs = []
        self.releases = {}
        self.writes = []

    def workflow_run(self, run_id):
        return self.runs[run_id]

    def pages(self, path, key=None):
        if "/attempts/" in path and path.endswith("/jobs"):
            run_id = int(path.split("/runs/")[1].split("/")[0])
            attempt = int(path.split("/attempts/")[1].split("/")[0])
            return [job for job in self.jobs.get(run_id, []) if job["run_attempt"] == attempt]
        if "/commits/" in path and path.endswith("/pulls"):
            commit = path.split("/commits/")[1].split("/")[0]
            if commit == self.source_pr["merge_commit_sha"]:
                return [self.source_pr]
            return []
        if "/pulls?" in path:
            query = parse_qs(urlsplit(path).query)
            if "head" not in query and query["state"] == ["closed"]:
                sources = [self.source_pr] + [
                    pr for prs in self.source_prs.values() for pr in prs
                ]
                return [pr for pr in sources if pr["base"]["ref"] == query["base"][0]]
            prs = [
                pr for pr in self.prs
                if pr["state"] == query["state"][0]
                and pr["head"]["ref"] == query["head"][0].split(":", 1)[1]
                and pr["base"]["ref"] == query["base"][0]
            ]
            for pr in prs:
                pr["head"]["sha"] = release.remote_head(self.root, pr["head"]["ref"])
            return prs
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
            number = 30 + len(self.prs)
            pr = {
                **data, "number": number, "state": "open", "merged_at": None,
                "head": {"sha": release.remote_head(self.root, data["head"]), "ref": data["head"]},
                "base": {"ref": data["base"]},
                "html_url": f"https://github.com/{self.repository}/pull/{number}",
            }
            self.prs.append(pr)
            return pr
        if method == "PATCH" and "/pulls/" in path:
            self.writes.append((path, data))
            pr = next(pr for pr in self.prs if pr["number"] == int(path.rsplit("/", 1)[1]))
            pr.update(data)
            pr["head"]["sha"] = release.remote_head(self.root, pr["head"]["ref"])
            return pr
        raise AssertionError(f"Unexpected API call: {method} {path}")

    def optional(self, path):
        return self.releases.get(unquote(path.split("/releases/tags/")[1]))


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
        self.prepare()
        release.push_notes(self.root, self.client, self.path)
        release.freeze(self.root, self.client, self.path)
        return release.load(self.path, self.cycle)

    def complete_release(self, promote=True):
        plan = self.freeze()
        release.publish(self.root, self.client, self.path)
        release.cleanup(self.root, self.client, self.path)
        if promote:
            release.promote(self.root, self.client, self.path)
        return plan

    def start_cycle(self, parent, version="0.2.0"):
        run_id = self.cycle.source_run_id + 1
        merge = release.commit_file(
            self.root, parent, ".semrelease/this_release",
            (
                "# Shared staging file\n\n#!other/v2.0.0\n- fix(action): preserve this section\n\n"
                f"#!dummy\n- feat(action): next change {run_id}\n"
            ).encode("utf-8"),
            "2026-01-02T00:00:00+00:00", f"Next verified PR merge {run_id}\n",
        )
        release.push_branch(self.root, self.cycle.branch, parent, merge)
        release.git(self.root, "checkout", "--quiet", "--detach", merge)
        self.merge = merge
        self.cycle = replace(
            self.cycle, merge_sha=merge, source_run_id=run_id, source_pr=self.cycle.source_pr + 1,
        )
        self.path = self.parent / f"release-state-{run_id}.json"
        self.client.runs[run_id] = {
            **self.client.runs[run_id - 1], "id": run_id, "head_sha": merge,
        }
        self.client.source_prs[self.client.source_pr["merge_commit_sha"]] = [self.client.source_pr]
        self.client.source_pr = {
            **self.client.source_pr, "number": self.cycle.source_pr, "merge_commit_sha": merge,
        }
        self.client.jobs[run_id] = [dict(job) for job in self.client.jobs[run_id - 1]]
        values = {
            "GITHUB_RUN_ID": str(run_id), "SOURCE_RUN_ID": str(run_id),
            "SOURCE_PR": str(self.cycle.source_pr), "CAPTURED_SHA": merge, "MERGE_SHA": merge,
            "CI_RUN_ID": str(run_id), "CI_TARGET_SHA": merge,
            "RELEASE_VERSION": version, "RELEASE_NOTES": f"### Features\n\n- Next release {run_id}",
        }
        self.environment.update(values)
        os.environ.update(values)

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

    def test_previous_attempt_successes_cannot_authorize_a_new_calculation(self):
        with patch.dict(os.environ, {"GITHUB_RUN_ATTEMPT": "2", "CI_RUN_ATTEMPT": "2"}):
            with self.assertRaisesRegex(ValueError, "Current-attempt"):
                self.calculate()
            self.assertFalse(self.path.exists())
            self.client.jobs[17].append({
                **self.client.jobs[17][-1], "id": 9, "run_attempt": 2,
            })
            with self.assertRaisesRegex(ValueError, "Current-attempt"):
                self.calculate()
            self.assertFalse(self.path.exists())
            self.client.jobs[17] = [
                {**job, "run_attempt": 2} for job in self.client.jobs[17][:len(release.CI_JOBS)]
            ]
            self.assertEqual(self.calculate().ci_run_attempt, 2)

    def test_ci_evidence_must_identify_this_run_and_attempt(self):
        for changes in (
            {"CI_RUN_ID": "18"}, {"CI_RUN_ATTEMPT": "2"}, {"GITHUB_RUN_ID": "18"},
        ):
            with self.subTest(changes=changes), patch.dict(os.environ, changes):
                with self.assertRaisesRegex(ValueError, "run|attempt"):
                    self.calculate()
                self.assertFalse(self.path.exists())

    def test_untrusted_workflow_or_wrong_merge_cannot_authorize_calculation(self):
        for field, value in (
            ("event", "workflow_dispatch"), ("head_branch", "release/other"),
            ("head_sha", "f" * 40), ("path", ".github/workflows/ci.yml"),
        ):
            with self.subTest(field=field), patch.dict(self.client.runs[17], {field: value}):
                with self.assertRaisesRegex(ValueError, "source run"):
                    self.calculate()
                self.assertFalse(self.path.exists())

    def test_inputs_are_scoped_and_sandbox_legacy_global_version_directives(self):
        with patch("release_workflow.emit") as emit:
            release.inputs(self.root, self.client, self.path)
        outputs = emit.call_args.kwargs
        self.assertNotIn("needs_calculation", outputs)
        self.assertNotIn("frozen", outputs)
        self.assertEqual(outputs["version"], "")
        self.assertEqual(outputs["tag_prefix"], "dummy/v")
        self.assertEqual(json.loads(outputs["change_log_json"]), "- feat(action): do the thing")
        self.assertEqual(
            (self.root / ".semrelease" / "this_release").read_text(encoding="utf-8"),
            "- feat(action): do the thing\n",
        )
        self.assertIn("#!other", release.run(self.root, "git", "show", f"{self.merge}:.semrelease/this_release").decode())

    def test_existing_local_plan_is_never_reused_or_overwritten(self):
        plan = self.calculate()
        with self.assertRaisesRegex(ValueError, "job-local"):
            release.inputs(self.root, self.client, self.path)
        with self.assertRaisesRegex(ValueError, "job-local"):
            self.calculate()
        self.assertEqual(release.load(self.path, self.cycle), plan)

    def test_inputs_use_release_commit_without_advancing_default_branch(self):
        candidate = release.commit_file(
            self.root, self.merge, "dummy/main.sh", b"echo candidate\n",
            "2026-01-01T00:00:00+00:00", "Candidate merge\n",
        )
        release.push_branch(self.root, self.cycle.branch, self.merge, candidate)
        self.client.runs[17]["head_sha"] = candidate
        self.client.source_pr["merge_commit_sha"] = candidate
        fresh = self.parent / "release checkout"
        release.git(
            self.parent, "clone", "--quiet", "--no-local", "--single-branch",
            "--branch", self.cycle.branch, "--depth", "1", str(self.remote), str(fresh),
        )
        with patch.dict(os.environ, {"MERGE_SHA": candidate}), patch("release_workflow.emit") as emit:
            release.inputs(fresh, self.client, self.path)
        self.assertEqual(emit.call_args.kwargs["tag_prefix"], "dummy/v")
        self.assertEqual(release.git(fresh, "rev-parse", "HEAD"), candidate)
        self.assertEqual(release.remote_head(fresh, "main"), self.merge)

    def test_existing_release_is_rejected_before_notes_are_pushed(self):
        self.client.releases["dummy/v0.1.0"] = {
            "tag_name": "dummy/v0.1.0", "draft": False, "prerelease": False,
            "body": self.environment["RELEASE_NOTES"],
        }
        with self.assertRaisesRegex(ValueError, "already exists.*manually"):
            self.calculate()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.client.writes)
        self.assertEqual(release.remote_head(self.root, self.cycle.branch), self.merge)

    def test_plan_rejects_wrong_types_schemas_and_cycles(self):
        plan = self.calculate()
        for changes in (
            {"schema": 2}, {"ci_run_id": True}, {"ci_run_id": 18}, {"release_notes": None},
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
    def test_captures_pinned_merge_without_any_ci_reuse_outputs(self):
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        values = emit.call_args.kwargs
        self.assertTrue(values["eligible"])
        self.assertEqual(values["merge_sha"], self.merge)
        self.assertEqual(values["source_pr"], 7)
        self.assertEqual(set(values), {
            "eligible", "action", "branch", "merge_sha", "source_run_id", "source_pr",
        })

    def test_merged_pr_routing_errors_fail_before_ci_or_publication(self):
        for source in ("dev/other", "main"):
            self.client.source_pr["head"]["ref"] = source
            with (
                self.subTest(source=source),
                patch("release_workflow.emit") as emit,
                self.assertRaisesRegex(ValueError, "PR action mismatch|must use <prefix>/dummy"),
            ):
                release.context(self.root, self.client, self.path)
            emit.assert_not_called()
            self.assertEqual(self.client.writes, [])
            self.assertFalse(self.path.exists())
            self.assertEqual(release.remote_head(self.root, self.cycle.branch), self.merge)

    def test_manual_dispatch_is_not_a_release_entry_point(self):
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}):
            with self.assertRaisesRegex(ValueError, "Only release-branch pushes"):
                release.context(self.root, self.client, self.path)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.client.writes)

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

    def test_open_promotion_is_not_a_release_eligibility_gate(self):
        self.client.prs = [{"state": "open", "body": "Different cycle", "merged_at": None}]
        with patch("release_workflow.promotions", side_effect=AssertionError("Promotion is independent")):
            with patch("release_workflow.emit") as emit:
                release.context(self.root, self.client, self.path)
        self.assertTrue(emit.call_args.kwargs["eligible"])

    def test_completed_publication_without_a_promotion_allows_another_cycle(self):
        first = self.complete_release(promote=False)
        self.start_cycle(first.cleanup_sha)
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        self.assertTrue(emit.call_args.kwargs["eligible"])
        self.assertFalse(self.client.prs)
        self.assertEqual(release.remote_head(self.root, "main"), first.cycle.merge_sha)

    def test_completed_noop_notes_publication_allows_another_cycle(self):
        with patch.dict(os.environ, {"RELEASE_VERSION": "0.0.1", "RELEASE_NOTES": "Old notes."}):
            first = self.complete_release(promote=False)
        self.assertEqual(first.release_sha, first.cycle.merge_sha)
        self.start_cycle(first.cleanup_sha)
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        self.assertTrue(emit.call_args.kwargs["eligible"])

    def test_legacy_tag_without_a_managed_release_merge_does_not_require_cleanup(self):
        release.git(self.root, "tag", "dummy/v0.0.1", self.merge)
        release.git(self.root, "push", "--quiet", "origin", "refs/tags/dummy/v0.0.1")
        self.start_cycle(self.merge)
        self.client.source_prs.clear()
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        self.assertTrue(emit.call_args.kwargs["eligible"])

    def test_retry_after_notes_push_requires_manual_reconciliation(self):
        self.prepare()
        release.push_notes(self.root, self.client, self.path)
        with self.assertRaisesRegex(ValueError, "already changed.*manually"):
            release.context(self.root, self.client, self.path)
        fresh = self.parent / "fresh-plan.json"
        with self.assertRaisesRegex(ValueError, "already changed.*manually"):
            release.inputs(self.root, self.client, fresh)
        self.assertFalse(fresh.exists())

    def test_existing_tag_on_the_merge_stops_a_fresh_attempt(self):
        release.git(self.root, "tag", "dummy/v0.1.0", self.merge)
        release.git(self.root, "push", "--quiet", "origin", "refs/tags/dummy/v0.1.0")
        with self.assertRaisesRegex(ValueError, "already has a release tag"):
            release.context(self.root, self.client, self.path)

    def test_published_cycle_cannot_restart_even_if_branch_is_rewound(self):
        first = self.complete_release()
        release.push_branch(self.root, self.cycle.branch, first.cleanup_sha, first.cycle.merge_sha)
        self.client.prs[0]["body"] = None
        with self.assertRaisesRegex(ValueError, "already has a tagged release.*manually"):
            release.context(self.root, self.client, self.path)

    def test_prior_notes_without_cleanup_block_another_cycle_even_with_a_promotion_marker(self):
        first = self.freeze()
        self.client.prs = [{
            "state": "closed", "body": self.cycle.marker, "merged_at": "2026-01-01T00:00:00Z",
        }]
        self.start_cycle(first.release_sha)
        with self.assertRaisesRegex(ValueError, "still needs matching cleanup.*manually"):
            release.context(self.root, self.client, self.path)
        self.assertFalse(self.client.writes)

    def test_prior_publication_without_cleanup_blocks_another_cycle(self):
        first = self.freeze()
        release.publish(self.root, self.client, self.path)
        self.start_cycle(first.release_sha)
        with self.assertRaisesRegex(ValueError, "still needs matching cleanup.*manually"):
            release.context(self.root, self.client, self.path)
        self.assertEqual(len(self.client.writes), 1)

    def test_prior_noop_notes_publication_without_cleanup_blocks_another_cycle(self):
        with patch.dict(os.environ, {"RELEASE_VERSION": "0.0.1", "RELEASE_NOTES": "Old notes."}):
            first = self.freeze()
            release.publish(self.root, self.client, self.path)
        self.assertEqual(first.release_sha, first.cycle.merge_sha)
        self.start_cycle(first.release_sha)
        with self.assertRaisesRegex(ValueError, "still needs matching cleanup.*manually"):
            release.context(self.root, self.client, self.path)
        self.assertEqual(len(self.client.writes), 1)

    def test_prior_noop_notes_tags_without_a_release_block_another_cycle(self):
        with patch.dict(os.environ, {"RELEASE_VERSION": "0.0.1", "RELEASE_NOTES": "Old notes."}):
            first = self.freeze()
            error = subprocess.CalledProcessError(1, ["gh", "api"], stderr=b"Network reset")
            with patch.object(self.client, "api", side_effect=error):
                with self.assertRaises(subprocess.CalledProcessError):
                    release.publish(self.root, self.client, self.path)
        self.assertEqual(first.release_sha, first.cycle.merge_sha)
        self.start_cycle(first.release_sha)
        with self.assertRaisesRegex(ValueError, "still needs matching cleanup.*manually"):
            release.context(self.root, self.client, self.path)
        self.assertFalse(self.client.writes)

    def test_prior_cleanup_without_a_published_release_blocks_another_cycle(self):
        first = self.complete_release(promote=False)
        del self.client.releases[first.tag]
        self.start_cycle(first.cleanup_sha)
        with self.assertRaisesRegex(ValueError, "Publication.*must complete"):
            release.context(self.root, self.client, self.path)
        self.assertEqual(len(self.client.writes), 1)

    def test_prior_cleanup_with_a_missing_or_moved_full_tag_blocks_another_cycle(self):
        first = self.complete_release(promote=False)
        self.start_cycle(first.cleanup_sha)
        release.git(self.root, "push", "--quiet", "origin", f":refs/tags/{first.tag}")
        with self.assertRaisesRegex(ValueError, "immutable tag does not match"):
            release.context(self.root, self.client, self.path)
        release.git(
            self.root, "push", "--quiet", "origin", f"{first.cycle.merge_sha}:refs/tags/{first.tag}",
        )
        with self.assertRaisesRegex(ValueError, "immutable tag does not match"):
            release.context(self.root, self.client, self.path)
        self.assertEqual(len(self.client.writes), 1)

    def test_prior_draft_or_prerelease_is_not_completed_publication(self):
        first = self.complete_release(promote=False)
        self.start_cycle(first.cleanup_sha)
        for flag in ("draft", "prerelease"):
            with (
                self.subTest(flag=flag),
                patch.dict(self.client.releases[first.tag], {flag: True}),
                self.assertRaisesRegex(ValueError, "conflicts"),
            ):
                release.context(self.root, self.client, self.path)

    def test_merged_promotion_with_an_empty_body_does_not_block_another_cycle(self):
        first = self.complete_release()
        release.push_branch(self.root, "main", first.cycle.merge_sha, first.cleanup_sha)
        self.client.prs[0].update(state="closed", body=None, merged_at="2026-01-01T00:00:00Z")
        self.start_cycle(first.cleanup_sha)
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        self.assertTrue(emit.call_args.kwargs["eligible"])

    def test_other_action_bookkeeping_does_not_gate_this_action(self):
        staging = release.run(self.root, "git", "show", f"{self.merge}:.semrelease/this_release")
        other = release.commit_file(
            self.root, self.merge, ".semrelease/this_release", staging + b"\n# Other cleanup\n",
            "2026-01-01T00:00:00+00:00",
            "chore(action): clear released changelog entries\n\n"
            f"Action-Release-Cleanup: other@{'a' * 40}\nRelease: other/v1.0.0\n",
        )
        release.push_branch(self.root, self.cycle.branch, self.merge, other)
        self.start_cycle(other)
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        self.assertTrue(emit.call_args.kwargs["eligible"])

    def test_copied_bookkeeping_in_a_squash_summary_is_not_a_new_release(self):
        first = self.complete_release(promote=False)
        notes = release.git(self.root, "log", "-1", "--format=%B", first.release_sha)
        cleanup = release.git(self.root, "log", "-1", "--format=%B", first.cleanup_sha)
        squash = release.commit_file(
            self.root, first.cleanup_sha, "dummy/main.sh", b"echo integrated\n", first.timestamp,
            f"Promotion squash summary\n\n{notes}\n\n{cleanup}\n",
        )
        release.push_branch(self.root, self.cycle.branch, first.cleanup_sha, squash)
        self.start_cycle(squash)
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        self.assertTrue(emit.call_args.kwargs["eligible"])

    def test_manual_cleanup_can_follow_intervening_branch_changes(self):
        first = self.freeze()
        release.publish(self.root, self.client, self.path)
        self.start_cycle(first.release_sha)
        staging = release.run(self.root, "git", "show", f"{self.merge}:.semrelease/this_release")
        reconciled = release.commit_file(
            self.root, self.merge, ".semrelease/this_release", staging + b"\n# Manually reconciled\n",
            first.timestamp, "chore(action): clear released changelog entries\n\n"
            f"Action-Release-Cleanup: dummy@{first.cycle.merge_sha}\nRelease: {first.tag}\n"
            "Signed-off-by: Test <test@example.invalid>\n",
        )
        release.push_branch(self.root, self.cycle.branch, self.merge, reconciled)
        self.start_cycle(reconciled)
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        self.assertTrue(emit.call_args.kwargs["eligible"])


class ReleaseIntegrationTests(ReleaseFixture):
    def test_two_releases_before_main_promotion_reuse_one_cumulative_pr(self):
        first = self.complete_release()
        pr = self.client.prs[0]
        pr["body"] += "\nHuman review note: keep this description.\n"
        original_body = pr["body"]
        self.start_cycle(first.cleanup_sha)
        with patch("release_workflow.emit") as emit:
            release.context(self.root, self.client, self.path)
        self.assertTrue(emit.call_args.kwargs["eligible"])
        second = self.complete_release()
        self.assertEqual(len(self.client.prs), 1)
        self.assertEqual(self.client.prs[0]["number"], pr["number"])
        self.assertEqual(pr["title"], "Promote dummy to main")
        self.assertEqual(pr["head"]["sha"], second.cleanup_sha)
        self.assertTrue(pr["body"].startswith(original_body))
        for plan in (first, second):
            self.assertIn(plan.tag, pr["body"])
            self.assertIn(plan.release_sha, pr["body"])
            self.assertIn(plan.cleanup_sha, pr["body"])
            self.assertEqual(self.remote_tag(plan.tag), plan.release_sha)
        for alias in ("dummy/v0", "dummy/v0.2"):
            self.assertEqual(self.remote_tag(alias), second.release_sha)
        self.assertEqual(self.remote_tag("dummy/v0.1"), first.release_sha)
        self.assertEqual(release.remote_head(self.root, "main"), first.cycle.merge_sha)
        self.assertEqual(release.remote_head(self.root, "release/dummy"), second.cleanup_sha)
        history = release.run(self.root, "git", "show", f"{second.release_sha}:dummy/RELEASE-NOTES.md")
        self.assertLess(history.index(b"## v0.2.0"), history.index(b"## v0.1.0"))
        self.assertEqual(history.count(b"## v0.1.0\n"), 1)
        cleaned = release.run(self.root, "git", "show", f"{second.cleanup_sha}:.semrelease/this_release")
        self.assertNotIn(b"#!dummy", cleaned)
        self.assertIn(b"#!other/v2.0.0\n- fix(action): preserve this section\n", cleaned)
        self.assertEqual(
            [path.rsplit("/", 1)[-1] for path, _ in self.client.writes],
            ["releases", "pulls", "releases", str(pr["number"])],
        )

    def test_complete_release_preserves_code_other_sections_and_manual_merge(self):
        self.path = self.root / "release-state.json"
        release.inputs(self.root, self.client, self.path)
        (self.root / "dummy" / "main.sh").write_bytes(b"echo unrelated staged change\n")
        release.git(self.root, "add", "dummy/main.sh")
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
        self.assertEqual(release.git(self.root, "diff", "--cached", "--name-only"), "dummy/main.sh")
        self.assertEqual([path.rsplit("/", 1)[-1] for path, _ in self.client.writes], ["releases", "pulls"])

    def test_notes_push_network_failure_requires_manual_reconciliation(self):
        plan = self.prepare()
        original = release.push_branch

        def lost_response(*args):
            original(*args)
            raise subprocess.CalledProcessError(1, ["git", "push"], stderr=b"Network reset")

        with patch("release_workflow.push_branch", side_effect=lost_response):
            with self.assertRaises(subprocess.CalledProcessError):
                release.push_notes(self.root, self.client, self.path)
        self.assertEqual(release.remote_head(self.root, self.cycle.branch), plan.release_sha)
        with patch("release_workflow.push_branch") as push:
            with self.assertRaisesRegex(ValueError, "already changed.*manually"):
                release.push_notes(self.root, self.client, self.path)
            push.assert_not_called()
        content = release.run(self.root, "git", "show", f"{plan.release_sha}:dummy/RELEASE-NOTES.md")
        self.assertEqual(content.count(b"## v0.1.0\n"), 1)

    def test_preparation_cannot_replay_an_existing_local_plan(self):
        plan = self.prepare()
        with self.assertRaisesRegex(ValueError, "new calculation"):
            release.prepare(self.root, self.client, self.path)
        self.assertEqual(release.load(self.path, self.cycle), plan)
        self.assertEqual(release.remote_head(self.root, self.cycle.branch), self.merge)

    def test_release_creation_network_failure_does_not_resume_publication(self):
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
        with self.assertRaisesRegex(ValueError, "already exists.*manually"):
            release.publish(self.root, self.client, self.path)
        self.assertEqual(self.remote_tag(plan.tag), tag)
        self.assertEqual(len(self.client.writes), 1)

    def test_promotion_handoff_explains_ci_approval_and_manual_merge(self):
        plan = self.freeze()
        release.publish(self.root, self.client, self.path)
        release.cleanup(self.root, self.client, self.path)
        self.assertEqual(release.remote_head(self.root, self.cycle.branch), plan.cleanup_sha)
        summary = self.root / "promotion-summary.md"
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}),
            patch("sys.stdout", output),
        ):
            release.promote(self.root, self.client, self.path)
        self.assertEqual(len(self.client.prs), 1)
        self.assertEqual(len(self.client.writes), 2)
        for text in (
            self.client.prs[0]["body"], summary.read_text(encoding="utf-8"), output.getvalue(),
        ):
            with self.subTest(text=text):
                self.assertIn("If prompted", text)
                self.assertLess(text.index("Approve workflows to run"), text.index("Merge manually"))
        self.assertEqual(output.getvalue().count("Approve workflows to run"), 1)

    def test_tags_without_a_github_release_are_not_silently_resumed(self):
        plan = self.freeze()
        error = subprocess.CalledProcessError(1, ["gh", "api"], stderr=b"Network reset")
        with patch.object(self.client, "api", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                release.publish(self.root, self.client, self.path)
        self.assertEqual(self.remote_tag(plan.tag), plan.release_sha)
        self.assertNotIn(plan.tag, self.client.releases)
        with self.assertRaisesRegex(ValueError, "already exists.*manually"):
            release.publish(self.root, self.client, self.path)
        self.assertEqual(self.remote_tag(plan.tag), plan.release_sha)
        self.assertFalse(self.client.writes)

    def test_existing_exact_notes_entry_needs_no_preparation_commit(self):
        with patch.dict(os.environ, {"RELEASE_VERSION": "0.0.1", "RELEASE_NOTES": "Old notes."}):
            self.calculate()
        release.prepare(self.root, self.client, self.path)
        self.assertEqual(release.load(self.path, self.cycle).release_sha, self.merge)
        release.push_notes(self.root, self.client, self.path)
        release.freeze(self.root, self.client, self.path)
        release.publish(self.root, self.client, self.path)
        self.assertEqual(self.remote_tag("dummy/v0.0.1"), self.merge)
        release.cleanup(self.root, self.client, self.path)
        release.promote(self.root, self.client, self.path)
        self.assertEqual(len(self.client.prs), 1)

    def test_unexpected_branch_advance_stops_notes_publication_and_cleanup(self):
        plan = self.prepare()
        moved = release.commit_file(
            self.root, self.merge, "dummy/main.sh", b"echo different\n", plan.timestamp, "Other work\n",
        )
        release.push_branch(self.root, self.cycle.branch, self.merge, moved)
        with self.assertRaisesRegex(ValueError, "changed"):
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
        with self.assertRaisesRegex(ValueError, "already exists.*manually"):
            release.publish(self.root, self.client, self.path)
        self.assertIsNone(self.remote_tag(plan.tag))
        self.assertFalse(self.client.writes)

    def test_exact_tag_at_another_commit_is_never_moved(self):
        plan = self.freeze()
        release.git(self.root, "tag", plan.tag, self.merge)
        release.git(self.root, "push", "--quiet", "origin", f"refs/tags/{plan.tag}")
        with self.assertRaisesRegex(ValueError, "already exists.*manually"):
            release.publish(self.root, self.client, self.path)
        self.assertEqual(self.remote_tag(plan.tag), self.merge)
        self.assertIsNone(self.remote_tag("dummy/v0"))
        self.assertFalse(self.client.writes)

    def test_cleanup_push_network_failure_requires_manual_reconciliation(self):
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
            with self.assertRaisesRegex(ValueError, "reconcile cleanup manually"):
                release.cleanup(self.root, self.client, self.path)
            push.assert_not_called()
        self.assertEqual(release.remote_head(self.root, self.cycle.branch), plan.cleanup_sha)

    def test_existing_open_promotion_is_reused_without_duplicate_metadata_or_writes(self):
        self.complete_release()
        body = self.client.prs[0]["body"]
        release.promote(self.root, self.client, self.path)
        self.assertEqual(len(self.client.prs), 1)
        self.assertEqual(len(self.client.writes), 2)
        self.assertEqual(self.client.prs[0]["body"], body)
        self.assertEqual(body.count(self.cycle.marker), 1)

    def test_manual_open_promotion_keeps_human_description_without_a_cycle_marker(self):
        self.complete_release(promote=False)
        body = "Human promotion description.\n\nDo not remove these review instructions.\n"
        self.client.api(f"repos/{self.cycle.repository}/pulls", "POST", {
            "title": "Manual promotion", "head": self.cycle.branch,
            "base": self.cycle.default_branch, "body": body,
        })
        for description in (body, "", None):
            self.client.prs[0]["body"] = description
            with self.subTest(description=description):
                release.promote(self.root, self.client, self.path)
                self.assertEqual(len(self.client.prs), 1)
                self.assertTrue(self.client.prs[0]["body"].startswith(description or ""))
                self.assertIn(self.cycle.marker, self.client.prs[0]["body"])
                self.assertEqual(self.client.prs[0]["title"], "Promote dummy to main")

    def test_closed_or_merged_promotions_allow_a_new_pr_for_a_later_release(self):
        first = self.complete_release()
        self.client.prs[0].update(state="closed", body=None)
        self.start_cycle(first.cleanup_sha)
        second = self.complete_release()
        self.client.prs[1].update(state="closed", body=None, merged_at="2026-01-01T00:00:00Z")
        self.start_cycle(second.cleanup_sha, version="0.3.0")
        third = self.complete_release()
        self.assertEqual(len(self.client.prs), 3)
        self.assertEqual(sum(pr["state"] == "open" for pr in self.client.prs), 1)
        self.assertEqual(self.client.prs[-1]["head"]["sha"], third.cleanup_sha)
        self.assertEqual(self.client.prs[-1]["number"], 32)

    def test_multiple_open_promotions_fail_without_creating_or_updating_a_pr(self):
        self.complete_release()
        self.client.prs.append({**self.client.prs[0], "number": 31})
        with self.assertRaisesRegex(ValueError, "Multiple open promotion PRs"):
            release.promote(self.root, self.client, self.path)
        self.assertEqual(len(self.client.writes), 2)

    def test_branch_movement_during_promotion_update_fails_without_another_pr(self):
        first = self.complete_release()
        self.start_cycle(first.cleanup_sha)
        second = self.complete_release(promote=False)
        moved = release.commit_file(
            self.root, second.cleanup_sha, "dummy/main.sh", b"echo other work\n",
            second.timestamp, "Other work\n",
        )
        original = self.client.api

        def move_branch(path, method="GET", data=None):
            if method == "PATCH":
                release.push_branch(self.root, self.cycle.branch, second.cleanup_sha, moved)
            return original(path, method, data)

        with patch.object(self.client, "api", side_effect=move_branch):
            with self.assertRaisesRegex(ValueError, "changed during the handoff.*manually"):
                release.promote(self.root, self.client, self.path)
        self.assertEqual(len(self.client.prs), 1)
        self.assertEqual(release.remote_head(self.root, self.cycle.branch), moved)

    def test_promotion_creation_network_failure_surfaces_without_creating_a_duplicate(self):
        self.complete_release(promote=False)
        original = self.client.api

        def lost_response(path, method="GET", data=None):
            result = original(path, method, data)
            if method == "POST" and path.endswith("/pulls"):
                raise subprocess.CalledProcessError(1, ["gh", "api"], stderr=b"Network reset")
            return result

        with patch.object(self.client, "api", side_effect=lost_response):
            with self.assertRaises(subprocess.CalledProcessError):
                release.promote(self.root, self.client, self.path)
        release.promote(self.root, self.client, self.path)
        self.assertEqual(len(self.client.prs), 1)
        self.assertEqual(len(self.client.writes), 2)


class GitHubClientTests(ReleaseFixture):
    def test_ancestry_lookup_only_treats_exit_one_as_a_negative_result(self):
        with patch("release_workflow.git", return_value=""):
            self.assertTrue(release.is_ancestor(self.root, self.merge, self.merge))
        error = subprocess.CalledProcessError(1, ["git", "merge-base"])
        with patch("release_workflow.git", side_effect=error):
            self.assertFalse(release.is_ancestor(self.root, self.merge, self.merge))
        error = subprocess.CalledProcessError(128, ["git", "merge-base"], stderr=b"Invalid object")
        with patch("release_workflow.git", side_effect=error):
            with self.assertRaises(subprocess.CalledProcessError):
                release.is_ancestor(self.root, self.merge, self.merge)

    def test_only_explicit_http_404_is_treated_as_missing_release(self):
        client = release.GitHub(self.root)
        for status in (403, 429, 500):
            error = subprocess.CalledProcessError(1, ["gh"], stderr=f"gh: error (HTTP {status})".encode())
            with patch.object(client, "api", side_effect=error), self.assertRaises(subprocess.CalledProcessError):
                client.optional("repos/example/actions/releases/tags/dummy%2Fv1.0.0")
        error = subprocess.CalledProcessError(1, ["gh"], stderr=b"gh: Not Found (HTTP 404)")
        with patch.object(client, "api", side_effect=error):
            self.assertIsNone(client.optional("missing"))

    def test_pagination_keeps_all_ci_jobs(self):
        client = release.GitHub(self.root)
        with patch.object(client, "api", side_effect=[
            {"jobs": list(range(100))}, {"jobs": [100]},
        ]) as api:
            self.assertEqual(client.pages("jobs", "jobs"), list(range(101)))
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

    def test_restore_command_is_no_longer_available(self):
        with patch("sys.argv", ["release_workflow.py", "restore"]), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                release.main()
        self.assertEqual(result.exception.code, 2)


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self):
        from ruamel.yaml import YAML

        yaml = YAML(typ="safe")
        self.workflow_text = (monorepo.ROOT / release.WORKFLOW).read_text(encoding="utf-8")
        self.workflow = yaml.load(self.workflow_text)
        self.ci = yaml.load((monorepo.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        self.jobs = self.workflow["jobs"]
        self.steps = self.jobs["Release"]["steps"]
        self.named = {step.get("name"): step for step in self.steps}

    def test_python_workflows_use_utf8_for_console_messages(self):
        for workflow in (self.workflow, self.ci):
            with self.subTest(name=workflow["name"]):
                self.assertEqual(workflow["env"]["PYTHONIOENCODING"], "utf-8")

    def test_release_only_triggers_on_action_branch_pushes(self):
        self.assertEqual(self.workflow["on"], {"push": {"branches": ["release/**"]}})
        self.assertFalse(self.workflow["concurrency"]["cancel-in-progress"])
        self.assertEqual(self.workflow["concurrency"]["group"], "release-${{ github.ref_name }}")
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
        self.assertNotIn("skipped", self.jobs["Release"]["if"])
        self.assertEqual(ci["if"], "${{ needs.CaptureContext.outputs.eligible == 'true' }}")

    def test_release_checkouts_use_incoming_code_not_the_default_branch(self):
        for job, reference in (
            ("CaptureContext", "${{ github.sha }}"),
            ("Release", "${{ needs.CaptureContext.outputs.merge-sha }}"),
        ):
            with self.subTest(job=job):
                checkout = self.jobs[job]["steps"][0]
                self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
                self.assertEqual(checkout["with"]["ref"], reference)
                self.assertEqual(checkout["with"]["fetch-depth"], 0)
                self.assertFalse(checkout["with"]["persist-credentials"])
        self.assertEqual(
            self.jobs["CaptureContext"]["outputs"]["merge-sha"],
            "${{ steps.gate.outputs.merge-sha }}",
        )
        self.assertNotIn("trusted-sha", self.jobs["CaptureContext"]["outputs"])
        self.assertNotIn("outputs.trusted-sha", self.workflow_text)

    def test_reusable_ci_exposes_actual_target_and_preserves_normal_previews(self):
        call = self.ci["on"]["workflow_call"]
        self.assertFalse(call["inputs"]["skip-release-preview"]["default"])
        self.assertEqual(call["outputs"]["target-sha"]["value"], "${{ jobs.CaptureContext.outputs.target-sha }}")
        capture = self.ci["jobs"]["CaptureContext"]["steps"]
        self.assertEqual(capture[0]["with"]["ref"], "${{ inputs.target-sha || github.sha }}")
        self.assertIn("!inputs.skip-release-preview", self.ci["jobs"]["PreviewRelease"]["if"])

    def test_local_calculation_notes_freeze_and_publication_are_ordered(self):
        required = [
            "Select the pinned action section",
            "Calculate release version and notes", "Validate and record the calculation",
            "Prepare the notes-only and cleanup commits", "Commit the release-notes update",
            "Freeze release plan",
            "Publish immutable release and compatibility aliases",
            "Clean only the published changelog section", "Create or update the promotion PR for manual merge",
        ]
        positions = [self.steps.index(self.named[name]) for name in required]
        self.assertEqual(positions, sorted(positions))
        semrelease = self.named["Calculate release version and notes"]
        self.assertTrue(semrelease["with"]["dry-run"])
        self.assertEqual(semrelease["with"]["target-sha"], "${{ env.MERGE_SHA }}")
        self.assertEqual(semrelease["with"]["version"], "${{ steps.inputs.outputs.version }}")
        self.assertEqual(semrelease["with"]["change-log"], "${{ fromJSON(steps.inputs.outputs.change-log-json) }}")
        self.assertNotIn("if", semrelease)
        calculation = self.named["Validate and record the calculation"]
        self.assertNotIn("if", calculation)
        self.assertEqual(calculation["env"], {
            "RELEASE_RESULT": "${{ steps.semrelease.outputs.result }}",
            "RELEASE_VERSION": "${{ steps.semrelease.outputs.releaseVersion }}",
            "RELEASE_NOTES": "${{ steps.semrelease.outputs.releaseNotes }}",
            "CI_RUN_ID": "${{ github.run_id }}",
            "CI_RUN_ATTEMPT": "${{ github.run_attempt }}",
            "CI_TARGET_SHA": "${{ needs.CI.outputs.target-sha }}",
        })
        self.assertEqual(
            self.named["Freeze release plan"]["run"],
            "python .github/scripts/release_workflow.py freeze",
        )
        self.assertNotIn("if", self.named["Prepare the notes-only and cleanup commits"])
        self.assertFalse(any(step.get("continue-on-error") for step in self.steps))

    def test_only_release_job_can_write_with_github_token(self):
        self.assertEqual(self.workflow["permissions"], {
            "contents": "read", "actions": "read", "pull-requests": "read",
        })
        self.assertNotIn("permissions", self.jobs["CaptureContext"])
        self.assertEqual(self.jobs["CI"]["permissions"], {"contents": "read"})
        self.assertEqual(self.jobs["Release"]["permissions"], {
            "contents": "write", "actions": "read", "pull-requests": "write",
        })
        token = self.jobs["Release"]["env"]["GH_TOKEN"]
        self.assertEqual(token, "${{ github.token }}")
        for name in (
            "Commit the release-notes update", "Publish immutable release and compatibility aliases",
            "Clean only the published changelog section", "Create or update the promotion PR for manual merge",
        ):
            self.assertEqual(self.named[name].get("env", {}).get("GH_TOKEN", token), "${{ github.token }}")
        git_access = self.named["Configure release Git access"]
        self.assertEqual(git_access["run"], "gh auth setup-git")
        for step in self.steps:
            self.assertNotIn("${{", step.get("run", ""))
            if "uses" in step and not step["uses"].startswith("./"):
                self.assertRegex(step["uses"], r"@[0-9a-f]{40}$")

    def test_release_requires_no_app_or_personal_token_credentials(self):
        for value in ("RELEASE_APP_", "create-github-app-token@", "steps.app.", "secrets."):
            with self.subTest(value=value):
                self.assertNotIn(value, self.workflow_text)

    def test_artifact_actions_and_recovery_outputs_are_removed(self):
        for value in (
            "upload-artifact@", "download-artifact@", "RECOVERY_", "checkpoint",
            "steps.restore", "needs-calculation", "ci-run-attempt", "run-ci",
        ):
            with self.subTest(value=value):
                self.assertNotIn(value, self.workflow_text)
        for name in ("restore", "checkpoints", "select_checkpoint", "ci_evidence", "ARTIFACT"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(release, name))
        self.assertFalse(hasattr(release.GitHub, "download"))


if __name__ == "__main__":
    unittest.main()

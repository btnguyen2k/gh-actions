import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monorepo


class RepositoryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def add_action(self, name="example-action"):
        directory = self.root / name
        for filename in ("action.yml", "README.md", "tests/run.sh"):
            path = directory / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        return directory


class ActionLayoutTests(RepositoryTestCase):
    def test_empty_repository_has_no_actions(self):
        (self.root / ".github").mkdir()
        self.assertEqual(monorepo.actions(self.root), [])

    def test_discovers_independent_root_actions(self):
        second = self.add_action("second-action")
        first = self.add_action("first-action")
        self.assertEqual(monorepo.actions(self.root), [first, second])

    def test_requires_documentation_and_test_entrypoint(self):
        directory = self.add_action()
        for filename in ("README.md", "tests/run.sh"):
            with self.subTest(filename=filename):
                path = directory / filename
                path.unlink()
                with self.assertRaisesRegex(ValueError, "missing"):
                    monorepo.actions(self.root)
                path.write_text("", encoding="utf-8")

    def test_rejects_root_metadata_and_alternate_filename(self):
        for path in (self.root / "action.yml", self.root / "example" / "action.yaml"):
            with self.subTest(path=path):
                path.parent.mkdir(exist_ok=True)
                path.write_text("", encoding="utf-8")
                with self.assertRaises(ValueError):
                    monorepo.actions(self.root)
                path.unlink()

    def test_rejects_invalid_names_and_versions(self):
        for name in ("Example", "../example", "example_action", "example/other", "-example", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                monorepo.validate_name(name)
        for version in ("v1.2.3", "1.2", "01.2.3", "1.02.3", "1.2.03", "1.2.3-rc.1", "1.2.3+build", ""):
            with self.subTest(version=version), self.assertRaises(ValueError):
                monorepo.parse_version(version)
        self.assertEqual(monorepo.parse_version("0.10.2"), (0, 10, 2))

    @patch("monorepo.subprocess.run")
    def test_tests_run_from_the_action_directory(self, run):
        directory = self.add_action()
        monorepo.test_actions(self.root)
        run.assert_called_once_with(["bash", "tests/run.sh"], cwd=directory, check=True)

    @patch("monorepo.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["bash"]))
    def test_action_test_failures_propagate(self, run):
        self.add_action()
        with self.assertRaises(subprocess.CalledProcessError):
            monorepo.test_actions(self.root)

    @patch("monorepo.subprocess.run")
    def test_validates_workflows_dependabot_and_action_metadata(self, run):
        self.add_action()
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("", encoding="utf-8")
        monorepo.validate(self.root)
        schemas = [call.args[0][4] for call in run.call_args_list]
        self.assertEqual(schemas, [
            "vendor.github-workflows", "vendor.dependabot", "vendor.github-actions",
        ])

    def test_action_schema_accepts_supported_runtimes_and_rejects_missing_runs(self):
        directory = self.add_action()
        metadata = directory / "action.yml"
        base = "name: Example\ndescription: Test action\n"
        examples = {
            "composite": "runs:\n  using: composite\n  steps:\n    - run: echo test\n      shell: bash\n",
            "javascript": "runs:\n  using: node24\n  main: dist/index.js\n",
            "docker": "runs:\n  using: docker\n  image: Dockerfile\n",
            "invalid": "",
        }
        for name, configuration in examples.items():
            with self.subTest(runtime=name):
                metadata.write_text(base + configuration, encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, "-m", "check_jsonschema", "--builtin-schema",
                     "vendor.github-actions", str(metadata)],
                    cwd=self.root, capture_output=True, text=True,
                )
                if name == "invalid":
                    self.assertNotEqual(result.returncode, 0)
                else:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_release_rejects_missing_environment(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "Missing release environment"):
                monorepo.release(self.root)

    def test_release_rejects_nondefault_branch_before_external_commands(self):
        environment = dict.fromkeys((
            "ACTION_NAME", "ACTION_VERSION", "RELEASE_NOTES", "GITHUB_SHA",
            "GITHUB_REPOSITORY", "GITHUB_REF", "DEFAULT_BRANCH", "GH_TOKEN",
        ), "test")
        environment.update(GITHUB_REF="refs/heads/feature", DEFAULT_BRANCH="main")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "default branch"):
                monorepo.release(self.root)


class ReleaseCommandTests(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.add_action()
        self.environment = {
            "ACTION_NAME": "example-action",
            "ACTION_VERSION": "1.2.3",
            "RELEASE_NOTES": "Action-specific notes\n$(literal-text)",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_REPOSITORY": "example/actions",
            "GITHUB_REF": "refs/heads/main",
            "DEFAULT_BRANCH": "main",
            "GH_TOKEN": "not-a-real-token",
        }
        environment = patch.dict(os.environ, self.environment, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    @patch("monorepo.capture")
    def test_api_errors_are_not_treated_as_missing_releases(self, capture):
        capture.side_effect = [
            self.environment["GITHUB_SHA"],
            subprocess.CalledProcessError(1, ["gh", "api"], stderr="Network unavailable"),
        ]
        with self.assertRaises(subprocess.CalledProcessError):
            monorepo.release(self.root)
        self.assertEqual(capture.call_count, 2)

    @patch("monorepo.capture")
    def test_rejects_existing_github_release_before_mutation(self, capture):
        capture.side_effect = [
            self.environment["GITHUB_SHA"], "other/v1.2.3\nexample-action/v1.2.3",
        ]
        with self.assertRaisesRegex(ValueError, "already exists"):
            monorepo.release(self.root)
        self.assertEqual(capture.call_count, 2)

    @patch("monorepo.capture")
    def test_rejects_a_different_checkout(self, capture):
        capture.return_value = "b" * 40
        with self.assertRaisesRegex(ValueError, "does not match"):
            monorepo.release(self.root)
        capture.assert_called_once()

    @patch("monorepo.push_tags")
    @patch("monorepo.tag_state", return_value=({}, {}))
    @patch("monorepo.capture")
    def test_release_notes_are_stdin_and_release_is_not_repo_latest(self, capture, state, push):
        capture.side_effect = [
            self.environment["GITHUB_SHA"], "", "", "", "https://example.invalid/release",
        ]
        monorepo.release(self.root)
        push.assert_called_once()
        command = capture.call_args
        self.assertIn("--verify-tag", command.args)
        self.assertIn("--latest=false", command.args)
        self.assertEqual(command.kwargs["stdin"], self.environment["RELEASE_NOTES"])


class ReleasePlanTests(unittest.TestCase):
    def test_initial_release_creates_all_three_references(self):
        self.assertEqual(monorepo.release_targets("example", "1.2.3", "new", {}), {
            "example/v1.2.3": "new", "example/v1": "new", "example/v1.2": "new",
        })

    def test_maintenance_release_does_not_roll_back_major_alias(self):
        targets = monorepo.release_targets("example", "1.2.5", "maintenance", {
            "example/v1.3.0": "latest", "example/v1.2.4": "old",
        })
        self.assertEqual(targets["example/v1"], "latest")
        self.assertEqual(targets["example/v1.2"], "maintenance")

    def test_older_patch_does_not_roll_back_either_alias(self):
        targets = monorepo.release_targets("example", "1.2.3", "older", {
            "example/v1.2.10": "latest",
        })
        self.assertEqual(targets["example/v1"], "latest")
        self.assertEqual(targets["example/v1.2"], "latest")

    def test_version_order_is_numeric_and_action_specific(self):
        targets = monorepo.release_targets("example", "1.9.1", "new", {
            "example/v1.10.0": "latest", "other/v99.0.0": "other",
            "example/v2.0.0": "next-major", "example/v1.11.0-rc.1": "prerelease",
            "example/v1": "alias",
        })
        self.assertEqual(targets["example/v1"], "latest")
        self.assertEqual(targets["example/v1.9"], "new")
        self.assertEqual(len(targets), 3)

    def test_existing_version_may_only_be_retried_at_the_same_commit(self):
        tags = {"example/v1.2.3": "original"}
        monorepo.release_targets("example", "1.2.3", "original", tags)
        with self.assertRaisesRegex(ValueError, "different commit"):
            monorepo.release_targets("example", "1.2.3", "replacement", tags)


class GitReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        parent = Path(self.temp.name)
        self.remote = parent / "remote.git"
        self.root = parent / "work"
        monorepo.capture(parent, "git", "init", "--bare", "--quiet", str(self.remote))
        monorepo.capture(parent, "git", "init", "--quiet", "--initial-branch=main", str(self.root))
        for name, value in {
            "user.name": "Test",
            "user.email": "test@example.invalid",
            "commit.gpgsign": "false",
            "tag.gpgsign": "false",
            "core.autocrlf": "false",
            "core.hooksPath": str(parent / "no-hooks"),
        }.items():
            monorepo.capture(self.root, "git", "config", "--local", name, value)
        monorepo.capture(self.root, "git", "remote", "add", "origin", str(self.remote))
        self.first = self.commit("first")

    def commit(self, content):
        (self.root / "file.txt").write_text(content, encoding="utf-8")
        monorepo.capture(self.root, "git", "add", "file.txt")
        monorepo.capture(self.root, "git", "commit", "--quiet", "-m", content)
        monorepo.capture(self.root, "git", "push", "--quiet", "origin", "main")
        return monorepo.capture(self.root, "git", "rev-parse", "HEAD")

    def publish_tags(self, version, commit):
        monorepo.capture(self.root, "git", "fetch", "--tags", "--force", "origin")
        objects, commits = monorepo.tag_state(self.root, "example")
        targets = monorepo.release_targets("example", version, commit, commits)
        monorepo.push_tags(self.root, targets, objects, f"example/v{version}")

    def remote_object(self, tag):
        result = monorepo.capture(
            self.root, "git", "ls-remote", "--refs", "origin", f"refs/tags/{tag}"
        )
        return result.split()[0] if result else None

    def test_publishes_only_the_requested_action_references(self):
        self.publish_tags("1.2.3", self.first)
        for tag in ("example/v1.2.3", "example/v1", "example/v1.2"):
            self.assertEqual(self.remote_object(tag), self.first)
        refs = monorepo.capture(self.root, "git", "ls-remote", "--tags", "origin")
        self.assertEqual(len(refs.splitlines()), 3)

    def test_maintenance_release_preserves_newer_major_target(self):
        self.publish_tags("1.3.0", self.first)
        second = self.commit("maintenance")
        self.publish_tags("1.2.5", second)
        self.assertEqual(self.remote_object("example/v1"), self.first)
        self.assertEqual(self.remote_object("example/v1.2"), second)
        self.assertEqual(self.remote_object("example/v1.3.0"), self.first)

    def test_retry_preserves_an_annotated_full_version_tag(self):
        monorepo.capture(
            self.root, "git", "tag", "-a", "example/v1.2.3", "-m", "Release", self.first
        )
        monorepo.capture(self.root, "git", "push", "--quiet", "origin", "example/v1.2.3")
        original = self.remote_object("example/v1.2.3")
        self.publish_tags("1.2.3", self.first)
        self.publish_tags("1.2.3", self.first)
        self.assertEqual(self.remote_object("example/v1.2.3"), original)
        self.assertNotEqual(original, self.first)

    def test_stale_alias_lease_rejects_the_entire_atomic_push(self):
        self.publish_tags("1.2.3", self.first)
        second = self.commit("second")
        concurrent = self.commit("concurrent")
        monorepo.capture(self.root, "git", "fetch", "--tags", "--force", "origin")
        objects, commits = monorepo.tag_state(self.root, "example")
        targets = monorepo.release_targets("example", "1.2.4", second, commits)
        monorepo.capture(
            self.root, "git", "--git-dir", str(self.remote),
            "update-ref", "refs/tags/example/v1", concurrent,
        )
        with self.assertRaises(subprocess.CalledProcessError):
            monorepo.push_tags(self.root, targets, objects, "example/v1.2.4")
        self.assertIsNone(self.remote_object("example/v1.2.4"))
        self.assertEqual(self.remote_object("example/v1.2"), self.first)
        self.assertEqual(self.remote_object("example/v1"), concurrent)


if __name__ == "__main__":
    unittest.main()

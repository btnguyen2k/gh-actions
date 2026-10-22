import io
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

    @patch("monorepo.sys.platform", "linux")
    @patch("monorepo.subprocess.run")
    def test_tests_run_from_the_action_directory(self, run):
        directory = self.add_action()
        monorepo.test_actions(self.root)
        run.assert_called_once_with(["bash", "tests/run.sh"], cwd=directory, check=True)

    @patch("monorepo.sys.platform", "win32")
    @patch("monorepo.capture")
    @patch("monorepo.subprocess.run")
    def test_windows_resolves_git_bash_before_launching_action_tests(self, run, capture):
        directory = self.add_action()
        native = self.root / "Git for Windows" / "usr" / "bin" / "bash.exe"
        native.parent.mkdir(parents=True)
        native.touch()
        capture.return_value = str(native.with_name("sh.exe"))
        monorepo.test_actions(self.root)
        capture.assert_called_once_with(self.root, "git", "var", "GIT_SHELL_PATH")
        run.assert_called_once_with([str(native), "tests/run.sh"], cwd=directory, check=True)

    @patch("monorepo.sys.platform", "win32")
    @patch("monorepo.capture")
    @patch("monorepo.subprocess.run")
    def test_windows_rejects_missing_or_relative_git_bash_without_falling_back(self, run, capture):
        self.add_action()
        for shell in (str(self.root / "missing-git" / "sh.exe"), "sh.exe"):
            with self.subTest(shell=shell):
                capture.return_value = shell
                with self.assertRaisesRegex(FileNotFoundError, "Git for Windows Bash"):
                    monorepo.test_actions(self.root)
        run.assert_not_called()

    @patch("monorepo.sys.platform", "win32")
    @patch("monorepo.capture")
    @patch("monorepo.subprocess.run")
    def test_windows_git_shell_lookup_errors_propagate_without_falling_back(self, run, capture):
        self.add_action()
        failure = subprocess.CalledProcessError(1, ["git", "var", "GIT_SHELL_PATH"])
        capture.side_effect = failure
        with self.assertRaises(subprocess.CalledProcessError) as result:
            monorepo.test_actions(self.root)
        self.assertIs(result.exception, failure)
        run.assert_not_called()

    @patch("monorepo.sys.platform", "win32")
    @patch("monorepo.capture")
    @patch("monorepo.subprocess.run")
    def test_empty_repository_does_not_require_a_shell(self, run, capture):
        monorepo.test_actions(self.root)
        capture.assert_not_called()
        run.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Requires native Windows.")
    def test_windows_action_scripts_run_in_native_git_bash(self):
        directory = self.add_action()
        (directory / "tests" / "run.sh").write_text(
            "set -euo pipefail\nprintf '%s' \"$OSTYPE\" > native-platform.txt\n",
            encoding="utf-8", newline="\n",
        )
        monorepo.test_actions(self.root)
        self.assertIn(
            (directory / "native-platform.txt").read_text(encoding="utf-8"),
            ("msys", "cygwin"),
        )

    @patch("monorepo.sys.platform", "linux")
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


class ChangelogTests(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        self.add_action()
        self.add_action("second-action")
        self.known_actions = {"example-action", "second-action"}

    def parse(self, text, selected_action=None):
        return monorepo.parse_changelog(text, self.known_actions, selected_action)

    def write_changelog(self, text, encoding="utf-8"):
        path = self.root / ".semrelease" / "this_release"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding=encoding)

    def report(self, text, branch, encoding="utf-8"):
        self.write_changelog(text, encoding)
        output = io.StringIO()
        with patch.dict(os.environ, {"CHANGELOG_BRANCH": branch}), patch("sys.stdout", output):
            monorepo.changelog(self.root)
        return output.getvalue()

    def test_parses_versions_notes_and_source_lines_in_file_order(self):
        sections, warnings = self.parse(
            "# Instructions\n\n"
            "#!example-action/v0.1.0\n"
            "- feat(action): capture workflow metadata\n"
            "\n  Additional details.\n\n"
            "#! second-action/v2.3.4\n"
            "- fix(action): preserve values\n"
        )
        self.assertEqual(sections, [
            monorepo.ReleaseSection(
                "example-action", "0.1.0",
                "- feat(action): capture workflow metadata\n\n  Additional details.", 3,
            ),
            monorepo.ReleaseSection(
                "second-action", "2.3.4",
                "- fix(action): preserve values", 8,
            ),
        ])
        self.assertEqual(warnings, [])

    def test_accepts_optional_spaces_after_marker_and_crlf(self):
        for spaces in ("", " ", "    "):
            with self.subTest(spaces=spaces):
                sections, warnings = self.parse(
                    f"#!{spaces}example-action/v1.2.3  \r\n- First update\r\n"
                    "- Second update\r\n"
                )
                self.assertEqual(sections, [
                    monorepo.ReleaseSection(
                        "example-action", "1.2.3", "- First update\n- Second update", 1,
                    ),
                ])
                self.assertEqual(warnings, [])

    def test_ignores_comments_but_preserves_note_indentation(self):
        sections, warnings = self.parse(
            "# Preamble\n\n#!example-action/v1.2.3\n"
            "# Maintainer comment\n    Indented note\n\n    More detail\n# Footer\n"
        )
        self.assertEqual(sections[0].notes, "    Indented note\n\n    More detail")
        self.assertEqual(warnings, [])

    def test_warns_on_invalid_sections_without_losing_later_valid_sections(self):
        invalid = {
            "#!": "expected #!",
            "#!example-action/1.2.3": "expected #!",
            "#!\texample-action/v1.2.3": "expected #!",
            "  #!example-action/v1.2.3": "expected #!",
            "#!Example/v1.2.3": "lowercase kebab-case",
            "#!example_action/v1.2.3": "lowercase kebab-case",
            "#!../v1.2.3": "lowercase kebab-case",
            "#!example-action/v1.2": "MAJOR.MINOR.PATCH",
            "#!example-action/v01.2.3": "MAJOR.MINOR.PATCH",
            "#!example-action/v1.2.3-rc.1": "MAJOR.MINOR.PATCH",
            "#!example-action/v1.2.3+build": "MAJOR.MINOR.PATCH",
            "#!unknown-action/v1.2.3": "Unknown action",
        }
        for header, message in invalid.items():
            with self.subTest(header=header):
                sections, warnings = self.parse(
                    f"{header}\n- Invalid section\n"
                    "#!second-action/v2.3.4\n- Valid section\n"
                )
                self.assertEqual(sections, [
                    monorepo.ReleaseSection("second-action", "2.3.4", "- Valid section", 3),
                ])
                self.assertEqual(len(warnings), 1)
                self.assertEqual(warnings[0][0], 1)
                self.assertIn(message, warnings[0][1])

    def test_warns_on_content_before_any_release_header(self):
        sections, warnings = self.parse(
            "# Instructions\n- Unscoped update\n#!example-action/v1.2.3\n- Update\n"
        )
        self.assertEqual(len(sections), 1)
        self.assertEqual(warnings, [
            (2, "Changelog content outside a #! release section is ignored."),
        ])

    def test_empty_sections_do_not_consume_the_next_sections_notes(self):
        sections, warnings = self.parse(
            "#!example-action/v1.2.3\n\n# Comment\n"
            "#!second-action/v2.3.4\n- Valid section\n"
        )
        self.assertEqual(sections, [
            monorepo.ReleaseSection("second-action", "2.3.4", "- Valid section", 4),
        ])
        self.assertEqual(warnings, [
            (1, "Release section example-action/v1.2.3 has no changelog entries."),
        ])

    def test_rejects_duplicate_and_conflicting_versions_instead_of_picking_one(self):
        for version in ("1.2.3", "1.2.4"):
            with self.subTest(version=version):
                sections, warnings = self.parse(
                    "#!example-action/v1.2.3\n- First section\n"
                    f"#!example-action/v{version}\n- Second section\n"
                    "#!second-action/v2.3.4\n- Valid section\n"
                )
                self.assertEqual(sections, [
                    monorepo.ReleaseSection("second-action", "2.3.4", "- Valid section", 5),
                ])
                self.assertEqual([line for line, _ in warnings], [1, 3])
                self.assertTrue(all("Multiple release sections" in message for _, message in warnings))

    def test_action_scope_ignores_unrelated_notes_and_invalid_sections(self):
        sections, warnings = self.parse(
            "Unscoped content\n"
            "#!second-action/not-a-version\n- Other action\n"
            "#!unknown-action/v01.2.3\n- Unknown action\n"
            "#!example-action/v1.2.3\n- Selected action\n"
            "#!example-action-extra/v1.2.3\n- Similar name\n",
            "example-action",
        )
        self.assertEqual(sections, [
            monorepo.ReleaseSection("example-action", "1.2.3", "- Selected action", 6),
        ])
        self.assertEqual(warnings, [])

    def test_action_scope_still_warns_on_malformed_headers_for_that_action(self):
        sections, warnings = self.parse(
            "#!example-action/not-a-version\n- Invalid\n"
            "#!second-action/not-a-version\n- Unrelated\n",
            "example-action",
        )
        self.assertEqual(sections, [])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0][0], 1)
        self.assertIn("Invalid release section header", warnings[0][1])

    def test_empty_or_comment_only_common_changelog_is_not_a_warning(self):
        for text in ("", "\n# Cleaned up\n\n"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), ([], []))
                output = self.report(text, "main")
                self.assertIn("Pending releases: 0\n", output)
                self.assertNotIn("::warning", output)

    def test_common_branches_report_every_actions_version_and_changelog(self):
        for branch in ("main", "dev", "feature/changelog", ""):
            with self.subTest(branch=branch):
                output = self.report(
                    "#!example-action/v1.2.3\n- First notes\n"
                    "#!second-action/v2.3.4\n- Second notes\n",
                    branch,
                )
                self.assertIn("Release scope: all actions\nPending releases: 2\n", output)
                self.assertIn(
                    "Action: example-action\nVersion: 1.2.3\n"
                    "Tag: example-action/v1.2.3\nChangelog:\n- First notes\n",
                    output,
                )
                self.assertIn(
                    "Action: second-action\nVersion: 2.3.4\n"
                    "Tag: second-action/v2.3.4\nChangelog:\n- Second notes\n",
                    output,
                )
                self.assertNotIn("::warning", output)

    def test_release_branch_reports_only_its_action(self):
        output = self.report(
            "#!example-action/v1.2.3\n- First notes\n"
            "#!second-action/v2.3.4\n- Second notes\n",
            "release/second-action",
        )
        self.assertIn("Release scope: action 'second-action'\nPending releases: 1\n", output)
        self.assertIn("Action: second-action\n", output)
        self.assertIn("- Second notes\n", output)
        self.assertNotIn("example-action", output)
        self.assertNotIn("- First notes", output)
        self.assertNotIn("::warning", output)

    def test_release_branch_warns_for_missing_empty_invalid_or_ambiguous_sections(self):
        for text in (
            "",
            "# Cleaned up\n",
            "#!second-action/v2.3.4\n- Other action\n",
            "#!example-action/v1.2.3\n# No notes\n",
            "#!example-action/v01.2.3\n- Invalid version\n",
            "#!example-action/v1.2.3\n- First\n#!example-action/v1.2.4\n- Second\n",
        ):
            with self.subTest(text=text):
                output = self.report(text, "release/example-action")
                self.assertIn("Pending releases: 0\n", output)
                self.assertIn(
                    "::warning file=.semrelease/this_release,line=1::"
                    "No valid, non-empty release sections found for action 'example-action'",
                    output,
                )

    def test_malformed_release_branch_never_falls_back_to_all_actions(self):
        for branch in ("release/", "release/unknown-action", "release/example-action/nested"):
            with self.subTest(branch=branch):
                output = self.report("#!example-action/v1.2.3\n- Notes\n", branch)
                self.assertIn("Pending releases: 0\n", output)
                self.assertNotIn("Release scope: all actions", output)
                self.assertIn("No valid, non-empty release sections", output)

    def test_reads_utf8_bom_and_preserves_unicode_notes(self):
        output = self.report(
            "#!example-action/v1.2.3\n- Preserve caf\u00e9 values\n",
            "main", encoding="utf-8-sig",
        )
        self.assertIn("- Preserve caf\u00e9 values\n", output)
        self.assertNotIn("::warning", output)

    def test_prints_untrusted_notes_with_runner_commands_disabled(self):
        output = self.report(
            "#!example-action/v1.2.3\n::error::Literal note\n"
            "::warning::Another literal note\n::endgroup::\n"
            "#!bad%name/v1.2.3\n- Invalid section\n",
            "main",
        )
        token = output.splitlines()[0].removeprefix("::stop-commands::")
        self.assertEqual(len(token), 32)
        protected, annotations = output.split(f"::{token}::\n")
        self.assertIn("::error::Literal note\n", protected)
        self.assertIn("::warning::Another literal note\n", protected)
        self.assertNotIn("::error::", annotations)
        self.assertIn("::warning file=.semrelease/this_release,line=5::", annotations)
        self.assertIn("bad%25name", annotations)

    def test_runner_command_resume_token_cannot_collide_with_notes(self):
        collision = monorepo.uuid.UUID(int=0)
        safe = monorepo.uuid.UUID(int=1)
        with patch("monorepo.uuid.uuid4", side_effect=[collision, safe]):
            output = self.report(
                f"#!example-action/v1.2.3\n::{collision.hex}::\n", "main",
            )
        self.assertTrue(output.startswith(f"::stop-commands::{safe.hex}\n"))
        self.assertIn(f"\n::{collision.hex}::\n", output)
        self.assertTrue(output.endswith(f"\n::{safe.hex}::\n"))

    def test_changelog_command_warns_without_failing(self):
        self.write_changelog("#!example-action/v01.2.3\n- Invalid version\n")
        output = io.StringIO()
        with (
            patch("monorepo.ROOT", self.root),
            patch("sys.argv", ["monorepo.py", "changelog"]),
            patch.dict(os.environ, {"CHANGELOG_BRANCH": "main"}),
            patch("sys.stdout", output),
        ):
            self.assertIsNone(monorepo.main())
        self.assertIn("::warning file=.semrelease/this_release,line=1::", output.getvalue())

    def test_changelog_command_fails_explicitly_when_file_is_missing(self):
        error = io.StringIO()
        with (
            patch("monorepo.ROOT", self.root),
            patch("sys.argv", ["monorepo.py", "changelog"]),
            patch("sys.stderr", error),
            self.assertRaises(SystemExit) as result,
        ):
            monorepo.main()
        self.assertEqual(result.exception.code, 1)
        self.assertIn("error:", error.getvalue())
        self.assertIn("this_release", error.getvalue())


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

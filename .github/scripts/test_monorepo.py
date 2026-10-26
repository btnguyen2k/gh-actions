import io
import json
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
        for filename in ("action.yml", "README.md", "RELEASE-NOTES.md", "tests/run.sh"):
            path = directory / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        return directory


class ConsoleOutputTests(RepositoryTestCase):
    def test_configures_utf8_without_changing_stream_error_handling(self):
        stdout_bytes, stderr_bytes = io.BytesIO(), io.BytesIO()
        stdout = io.TextIOWrapper(stdout_bytes, encoding="ascii", errors="strict", newline="\n")
        stderr = io.TextIOWrapper(
            stderr_bytes, encoding="ascii", errors="backslashreplace", newline="\n",
        )
        self.addCleanup(stdout.close)
        self.addCleanup(stderr.close)
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            monorepo.configure_output()
            print("ℹ️ Information")
            print("❌ Error", file=sys.stderr)
        stdout.flush()
        stderr.flush()
        self.assertEqual(stdout_bytes.getvalue(), "ℹ️ Information\n".encode("utf-8"))
        self.assertEqual(stderr_bytes.getvalue(), "❌ Error\n".encode("utf-8"))
        self.assertEqual(stdout.errors, "strict")
        self.assertEqual(stderr.errors, "backslashreplace")

    def test_supports_in_memory_output_capture(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            monorepo.configure_output()
            print("✅ Success")
        self.assertEqual(stdout.getvalue(), "✅ Success\n")

    def test_warning_icon_preserves_annotation_metadata_and_escaping(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            monorepo.print_changelog_warnings([(7, "Invalid%\nsection\r")])
        self.assertEqual(
            output.getvalue(),
            "::warning file=.semrelease/this_release,line=7::⚠️ Invalid%25%0Asection%0D\n",
        )

    def test_cli_emits_utf8_messages_with_ascii_stdio_defaults(self):
        self.add_action()
        changelog = self.root / ".semrelease" / "this_release"
        changelog.parent.mkdir()
        changelog.write_text(
            "#!example-action\n- feat(action): test output\n#!bad%name\n- Ignored\n",
            encoding="utf-8",
        )
        command = [
            sys.executable, "-c",
            "import sys; from pathlib import Path; import monorepo; "
            "monorepo.ROOT = Path(sys.argv[1]); "
            "sys.argv = ['monorepo.py', 'validate-changelog']; monorepo.main()",
            str(self.root),
        ]
        environment = {**os.environ, "PYTHONIOENCODING": "ascii", "PYTHONUTF8": "0"}
        result = subprocess.run(
            command, cwd=Path(monorepo.__file__).parent, env=environment,
            capture_output=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        output = result.stdout.decode("utf-8")
        self.assertIn("::warning file=.semrelease/this_release,line=3::⚠️ ", output)
        self.assertIn("✅ Changelog validation passed", output)
        changelog.unlink()
        result = subprocess.run(
            command, cwd=Path(monorepo.__file__).parent, env=environment,
            capture_output=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.decode("utf-8").startswith("❌ error:"))


class ReleaseBranchTests(RepositoryTestCase):
    def test_accepts_matching_action_with_any_nonempty_prefix(self):
        for prefix in ("dev", "feature", "hotfix", "release", "custom_prefix.123"):
            with self.subTest(prefix=prefix):
                self.assertTrue(monorepo.validate_release_pr_branches(
                    f"{prefix}/example-action", "release/example-action",
                ))

    def test_non_release_targets_including_promotion_remain_unrestricted(self):
        for source, target in (
            ("release/example-action", "main"),
            ("main", "dev/example-action"),
            ("feature/general-cleanup", "main"),
            ("dependabot/github_actions/checkout", "main"),
            ("dev/example-action", "dev/second-action"),
            ("dev/example-action", "release"),
        ):
            with self.subTest(source=source, target=target):
                self.assertFalse(monorepo.validate_release_pr_branches(source, target))

    def test_rejects_mismatched_action_names(self):
        with self.assertRaisesRegex(ValueError, "PR action mismatch") as error:
            monorepo.validate_release_pr_branches(
                "dev/example-action", "release/second-action",
            )
        self.assertIn("dev/example-action", str(error.exception))
        self.assertIn("release/second-action", str(error.exception))

    def test_rejects_unscoped_or_malformed_release_sources(self):
        for source in (
            "main", "example-action", "/example-action", "dev/",
            "feature/nested/example-action", "dev/Example-action", "dev/example_action",
        ):
            with self.subTest(source=source), self.assertRaisesRegex(
                ValueError, "must use <prefix>/example-action",
            ):
                monorepo.validate_release_pr_branches(source, "release/example-action")

    def test_rejects_malformed_release_targets(self):
        for target in (
            "release/", "release/Example-action", "release/example_action",
            "release/example-action/nested",
        ):
            with self.subTest(target=target), self.assertRaisesRegex(
                ValueError, "Invalid release PR target branch",
            ):
                monorepo.validate_release_pr_branches("dev/example-action", target)

    def test_rejects_missing_or_nontext_branch_metadata(self):
        for source, target in (
            ("", "release/example-action"), ("dev/example-action", ""),
            (None, "release/example-action"), ("dev/example-action", None),
            (True, "release/example-action"), ("dev/example-action", 1),
        ):
            with self.subTest(source=source, target=target), self.assertRaisesRegex(
                ValueError, "nonempty strings",
            ):
                monorepo.validate_release_pr_branches(source, target)

    def test_cli_reports_matching_release_branches_and_non_release_targets(self):
        for source, target, message in (
            ("feature/example-action", "release/example-action", "✅ Release PR branches match"),
            ("release/example-action", "main", "ℹ️ PR target 'main' is not a release branch"),
        ):
            output = io.StringIO()
            with (
                self.subTest(source=source, target=target),
                patch.dict(os.environ, {"PR_HEAD_REF": source, "PR_BASE_REF": target}),
                patch("sys.argv", ["monorepo.py", "validate-pr-branches"]),
                patch("sys.stdout", output),
            ):
                self.assertIsNone(monorepo.main())
            self.assertIn(message, output.getvalue())

    def test_cli_rejects_mismatch_before_exporting_any_plan_even_with_both_sections(self):
        self.add_action()
        self.add_action("second-action")
        changelog = self.root / ".semrelease" / "this_release"
        changelog.parent.mkdir()
        content = "#!example-action\n- First update\n#!second-action\n- Second update\n"
        changelog.write_text(content, encoding="utf-8", newline="\n")
        github_output = self.root / "outputs.txt"
        github_output.write_text("existing=value\n", encoding="utf-8", newline="\n")
        output, error = io.StringIO(), io.StringIO()
        with (
            patch("monorepo.ROOT", self.root),
            patch.dict(os.environ, {
                "PR_HEAD_REF": "dev/example-action", "PR_BASE_REF": "release/second-action",
                "GITHUB_OUTPUT": str(github_output),
            }),
            patch("sys.argv", ["monorepo.py", "validate-pr-branches"]),
            patch("sys.stdout", output), patch("sys.stderr", error),
            self.assertRaises(SystemExit) as result,
        ):
            monorepo.main()
        self.assertEqual(result.exception.code, 1)
        self.assertIn("❌ error: PR action mismatch", error.getvalue())
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(changelog.read_text(encoding="utf-8"), content)
        self.assertEqual(github_output.read_text(encoding="utf-8"), "existing=value\n")

    def test_cli_requires_explicit_pr_branch_metadata(self):
        for environment in ({}, {"PR_HEAD_REF": "dev/example-action"}, {"PR_BASE_REF": "main"}):
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, environment, clear=True),
                self.assertRaisesRegex(ValueError, "Missing PR branch environment variables"),
            ):
                monorepo.validate_pr_branches(self.root)


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
        for filename in ("README.md", "RELEASE-NOTES.md", "tests/run.sh"):
            with self.subTest(filename=filename):
                path = directory / filename
                path.unlink()
                with self.assertRaises(ValueError) as error:
                    monorepo.actions(self.root)
                self.assertEqual(str(error.exception), f"example-action: missing {filename}.")
                path.write_text("", encoding="utf-8")

    def test_rejects_directory_in_place_of_release_notes(self):
        path = self.add_action() / "RELEASE-NOTES.md"
        path.unlink()
        path.mkdir()
        with self.assertRaises(ValueError) as error:
            monorepo.actions(self.root)
        self.assertEqual(str(error.exception), "example-action: missing RELEASE-NOTES.md.")

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
        self.add_action("second-action")
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("", encoding="utf-8")
        (workflows / "release.yaml").write_text("", encoding="utf-8")
        output = io.StringIO()
        log_snapshots = []
        run.side_effect = lambda *args, **kwargs: log_snapshots.append(output.getvalue())
        with patch("sys.stdout", output):
            monorepo.validate(self.root)
        schemas = [call.args[0][4] for call in run.call_args_list]
        self.assertEqual(schemas, [
            "vendor.github-workflows", "vendor.dependabot", "vendor.github-actions",
        ])
        groups = [
            "ℹ️ Validating github-workflows:\n"
            "  - .github/workflows/ci.yml\n"
            "  - .github/workflows/release.yaml\n",
            "ℹ️ Validating dependabot:\n"
            "  - .github/dependabot.yml\n",
            "ℹ️ Validating github-actions:\n"
            "  - example-action/action.yml\n"
            "  - second-action/action.yml\n",
        ]
        self.assertEqual(output.getvalue(), "".join(groups))
        self.assertEqual(
            log_snapshots, ["".join(groups[:end]) for end in range(1, len(groups) + 1)],
        )

    @patch("monorepo.subprocess.run")
    def test_validation_logs_failing_group_and_preserves_errors(self, run):
        self.add_action()
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("", encoding="utf-8")
        failure = subprocess.CalledProcessError(1, ["check-jsonschema"])
        run.side_effect = failure
        output = io.StringIO()
        with patch("sys.stdout", output), self.assertRaises(subprocess.CalledProcessError) as result:
            monorepo.validate(self.root)
        self.assertIs(result.exception, failure)
        run.assert_called_once()
        self.assertEqual(
            output.getvalue(), "ℹ️ Validating github-workflows:\n  - .github/workflows/ci.yml\n",
        )

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

    def report(self, text, branch, encoding="utf-8", github_output=None):
        self.write_changelog(text, encoding)
        output = io.StringIO()
        environment = {
            "CHANGELOG_BRANCH": branch,
            "GITHUB_OUTPUT": str(github_output) if github_output is not None else "",
        }
        with patch.dict(os.environ, environment), patch("sys.stdout", output):
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

    def test_versionless_headers_request_automatic_versions(self):
        for spaces in ("", " ", "    "):
            with self.subTest(spaces=spaces):
                sections, warnings = self.parse(
                    f"#!{spaces}example-action  \r\n- feat(action): add capability\r\n"
                )
                self.assertEqual(sections, [
                    monorepo.ReleaseSection(
                        "example-action", "", "- feat(action): add capability", 1,
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
            "#!example-action/v": "expected #!",
            "#!\texample-action/v1.2.3": "expected #!",
            "  #!example-action/v1.2.3": "expected #!",
            "#!Example/v1.2.3": "lowercase kebab-case",
            "#!example_action/v1.2.3": "lowercase kebab-case",
            "#!*&^dummy": "lowercase kebab-case",
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
        for first, second in (
            ("#!example-action/v1.2.3", "#!example-action/v1.2.3"),
            ("#!example-action/v1.2.3", "#!example-action/v1.2.4"),
            ("#!example-action/v1.2.3", "#!example-action"),
            ("#!example-action", "#!example-action"),
        ):
            with self.subTest(first=first, second=second):
                with self.assertRaisesRegex(monorepo.ChangelogError, (
                    "Multiple release sections for action 'example-action' at lines 1, 3"
                )):
                    self.parse(
                        f"{first}\n- First section\n{second}\n- Second section\n"
                        "#!second-action/v2.3.4\n- Valid section\n"
                    )

    def test_duplicate_headers_fail_even_when_sections_are_empty(self):
        for first_notes, second_notes in (
            ("", "- Second update\n"),
            ("- First update\n", ""),
            ("# Comment only\n", "# Comment only\n"),
        ):
            with self.subTest(first_notes=first_notes, second_notes=second_notes):
                with self.assertRaisesRegex(monorepo.ChangelogError, "Multiple release sections"):
                    self.parse(
                        f"#!example-action/v1.2.3\n{first_notes}"
                        f"#!example-action/v1.2.4\n{second_notes}"
                    )

    def test_reports_every_action_with_duplicate_sections(self):
        with self.assertRaises(monorepo.ChangelogError) as error:
            self.parse(
                "#!example-action\n- First\n#!second-action\n- First\n"
                "#!example-action\n- Second\n#!second-action\n- Second\n"
            )
        self.assertIn("action 'example-action' at lines 1, 5", str(error.exception))
        self.assertIn("action 'second-action' at lines 3, 7", str(error.exception))

    def test_invalid_headers_do_not_count_as_duplicate_sections(self):
        sections, warnings = self.parse(
            "#!*&^dummy\n- Invalid name\n"
            "#!example-action/vnot-semver\n- Invalid version\n"
            "#!example-action\n- Valid update\n"
        )
        self.assertEqual(sections, [
            monorepo.ReleaseSection("example-action", "", "- Valid update", 5),
        ])
        self.assertEqual([line for line, _ in warnings], [1, 3])

    def test_validation_warns_and_ignores_invalid_sections_regardless_of_branch_scope(self):
        self.write_changelog(
            "#!example-action\n- First update\n#!*&^dummy\n- Invalid section\n"
            "#!second-action/v1.2.3\n- Second update\n",
            encoding="utf-8-sig",
        )
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"CHANGELOG_BRANCH": "feature/second-action"}),
            patch("sys.stdout", output),
        ):
            monorepo.validate_changelog(self.root)
        self.assertIn("::warning file=.semrelease/this_release,line=3::", output.getvalue())
        self.assertIn("#!*&^dummy", output.getvalue())
        self.assertIn("Changelog validation passed (2 non-empty sections).", output.getvalue())
        self.assertNotIn("- Invalid section", output.getvalue())

    def test_validation_accepts_an_empty_or_cleaned_up_changelog(self):
        for text in ("", "# Cleaned up\n"):
            with self.subTest(text=text):
                self.write_changelog(text)
                output = io.StringIO()
                with patch("sys.stdout", output):
                    monorepo.validate_changelog(self.root)
                self.assertEqual(
                    output.getvalue(), "✅ Changelog validation passed (0 non-empty sections).\n",
                )

    def test_validation_command_reports_warnings_and_fails_duplicates_outside_branch_scope(self):
        self.write_changelog(
            "#!*&^dummy\n- Invalid section\n"
            "#!example-action/v1.2.3\n- First\n#!example-action/v1.2.4\n- Second\n"
        )
        output, error = io.StringIO(), io.StringIO()
        with (
            patch("monorepo.ROOT", self.root),
            patch("sys.argv", ["monorepo.py", "validate-changelog"]),
            patch.dict(os.environ, {"CHANGELOG_BRANCH": "feature/second-action"}),
            patch("sys.stdout", output),
            patch("sys.stderr", error),
            self.assertRaises(SystemExit) as result,
        ):
            monorepo.main()
        self.assertEqual(result.exception.code, 1)
        self.assertIn("::warning file=.semrelease/this_release,line=1::", output.getvalue())
        self.assertIn("#!*&^dummy", output.getvalue())
        self.assertNotIn("validation passed", output.getvalue())
        self.assertIn("action 'example-action' at lines 3, 5", error.getvalue())

    def test_duplicate_sections_cannot_export_a_release_matrix(self):
        for branch in ("main", "feature/example-action"):
            with self.subTest(branch=branch):
                github_output = self.root / "outputs.txt"
                github_output.write_text("existing=value\n", encoding="utf-8")
                with self.assertRaises(monorepo.ChangelogError):
                    self.report(
                        "#!example-action/v1.2.3\n- First\n"
                        "#!example-action/v1.2.4\n- Second\n",
                        branch, github_output=github_output,
                    )
                self.assertEqual(
                    github_output.read_text(encoding="utf-8"), "existing=value\n",
                )

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

    def test_unscoped_branches_report_every_actions_version_and_changelog(self):
        for branch in ("main", "dev", "release", "feature", "/second-action", ""):
            with self.subTest(branch=branch):
                output = self.report(
                    "#!example-action/v1.2.3\n- First notes\n"
                    "#!second-action/v2.3.4\n- Second notes\n",
                    branch,
                )
                self.assertIn("Release scope: all actions\nPending releases: 2\n", output)
                self.assertIn(
                    "Action: example-action\nVersion: 1.2.3\n"
                    "Tag prefix: example-action/v\nChangelog:\n- First notes\n",
                    output,
                )
                self.assertIn(
                    "Action: second-action\nVersion: 2.3.4\n"
                    "Tag prefix: second-action/v\nChangelog:\n- Second notes\n",
                    output,
                )
                self.assertNotIn("::warning", output)

    def test_action_branches_report_only_their_action(self):
        for prefix in ("release", "dev", "feature", "custom_prefix.123"):
            with self.subTest(prefix=prefix):
                output = self.report(
                    "#!example-action/v1.2.3\n- First notes\n"
                    "#!second-action/v2.3.4\n- Second notes\n",
                    f"{prefix}/second-action",
                )
                self.assertIn("Release scope: action 'second-action'\nPending releases: 1\n", output)
                self.assertIn("Action: second-action\n", output)
                self.assertIn("- Second notes\n", output)
                self.assertNotIn("example-action", output)
                self.assertNotIn("- First notes", output)
                self.assertNotIn("::warning", output)

    def test_action_branches_warn_for_missing_empty_or_invalid_sections(self):
        for text in (
            "",
            "# Cleaned up\n",
            "#!second-action/v2.3.4\n- Other action\n",
            "#!example-action/v1.2.3\n# No notes\n",
            "#!example-action\n# No notes\n",
            "#!example-action/v01.2.3\n- Invalid version\n",
        ):
            for prefix in ("release", "dev", "feature", "custom_prefix.123"):
                with self.subTest(text=text, prefix=prefix):
                    output = self.report(text, f"{prefix}/example-action")
                    self.assertIn("Pending releases: 0\n", output)
                    self.assertIn(
                        "::warning file=.semrelease/this_release,line=1::⚠️ "
                        "No valid, non-empty release sections found for action 'example-action'",
                        output,
                    )

    def test_malformed_action_branch_never_falls_back_to_all_actions(self):
        for prefix in ("release", "dev", "feature", "custom_prefix.123"):
            for suffix in ("", "unknown-action", "changelog", "example-action/nested"):
                branch = f"{prefix}/{suffix}"
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
            patch.dict(os.environ, {"CHANGELOG_BRANCH": "main", "GITHUB_OUTPUT": ""}),
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

    def test_exports_fixed_and_automatic_versions_with_multiline_changelog(self):
        github_output = self.root / "outputs.txt"
        github_output.write_text("existing=value\n", encoding="utf-8")
        notes = '- feat(action): preserve "quotes"\n- fix(action): handle caf\u00e9\n::warning::literal'
        output = self.report(
            f"#!example-action/v1.2.3\n{notes}\n"
            "#!second-action\n- feat(action): add capability\n",
            "main", github_output=github_output,
        )
        lines = github_output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        values = dict(line.split("=", 1) for line in lines)
        self.assertEqual(values["existing"], "value")
        self.assertEqual(values["has-releases"], "true")
        self.assertEqual(json.loads(values["matrix"]), {"include": [
            {
                "action": "example-action", "version": "1.2.3",
                "tag-prefix": "example-action/v", "change-log": notes,
            },
            {
                "action": "second-action", "version": "",
                "tag-prefix": "second-action/v", "change-log": "- feat(action): add capability",
            },
        ]})
        self.assertIn("Version: (automatic)\nTag prefix: second-action/v\n", output)

    def test_exported_matrix_respects_any_nonempty_action_branch_prefix(self):
        for prefix in ("release", "dev", "feature", "custom_prefix.123"):
            with self.subTest(prefix=prefix):
                github_output = self.root / "outputs.txt"
                github_output.write_text("", encoding="utf-8")
                self.report(
                    "#!example-action/v1.2.3\n- First notes\n"
                    "#!second-action\n- Second notes\n",
                    f"{prefix}/second-action", github_output=github_output,
                )
                values = dict(
                    line.split("=", 1)
                    for line in github_output.read_text(encoding="utf-8").splitlines()
                )
                self.assertEqual(json.loads(values["matrix"]), {"include": [{
                    "action": "second-action", "version": "",
                    "tag-prefix": "second-action/v", "change-log": "- Second notes",
                }]})
                self.assertEqual(values["has-releases"], "true")

    def test_empty_or_invalid_plan_exports_a_skippable_empty_matrix(self):
        for text, branch in (
            ("# Cleaned up\n", "main"),
            ("#!example-action/v01.2.3\n- Invalid\n", "main"),
            ("#!example-action\n- Valid\n", "dev/second-action"),
            ("#!example-action\n- Valid\n", "feature/second-action"),
        ):
            with self.subTest(text=text, branch=branch):
                github_output = self.root / "outputs.txt"
                github_output.write_text("", encoding="utf-8")
                self.report(text, branch, github_output=github_output)
                values = dict(
                    line.split("=", 1)
                    for line in github_output.read_text(encoding="utf-8").splitlines()
                )
                self.assertEqual(json.loads(values["matrix"]), {"include": []})
                self.assertEqual(values["has-releases"], "false")


class ReleasePreviewTests(RepositoryTestCase):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {
            "ACTION_NAME": "example-action",
            "RELEASE_RESULT": "SUCCESS",
            "RELEASE_VERSION": "1.2.3",
            "RELEASE_NOTES": "## Features\n\n- feat(action): add capability\n",
            "GITHUB_STEP_SUMMARY": "",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def test_reports_all_upstream_outputs_and_appends_a_job_summary(self):
        summary = self.root / "summary.md"
        summary.write_text("Existing summary\n\n", encoding="utf-8")
        os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
        output = io.StringIO()
        with patch("sys.stdout", output):
            monorepo.release_preview(self.root)
        self.assertIn(
            "✅ Action: example-action\nresult: SUCCESS\nreleaseVersion: 1.2.3\n"
            "releaseNotes:\n## Features\n\n- feat(action): add capability\n",
            output.getvalue(),
        )
        self.assertEqual(summary.read_text(encoding="utf-8"), (
            "Existing summary\n\n## ✅ Release preview: `example-action`\n\n"
            "- result: `SUCCESS`\n- releaseVersion: `1.2.3`\n\n"
            "### releaseNotes\n\n## Features\n\n- feat(action): add capability\n\n\n"
        ))

    def test_skipped_result_preserves_empty_outputs(self):
        os.environ.update(RELEASE_RESULT="SKIPPED", RELEASE_VERSION="", RELEASE_NOTES="")
        output = io.StringIO()
        with patch("sys.stdout", output):
            monorepo.release_preview(self.root)
        self.assertIn("ℹ️ Action: example-action\n", output.getvalue())
        self.assertIn("result: SKIPPED\nreleaseVersion: \nreleaseNotes:\n", output.getvalue())

    def test_failed_result_is_reported_and_remains_a_failure(self):
        os.environ.update(RELEASE_RESULT="FAILED", RELEASE_VERSION="", RELEASE_NOTES="")
        output = io.StringIO()
        with patch("sys.stdout", output), self.assertRaisesRegex(ValueError, "preview.*failed"):
            monorepo.release_preview(self.root)
        self.assertIn("❌ Action: example-action\n", output.getvalue())
        self.assertIn("result: FAILED\n", output.getvalue())

    def test_rejects_missing_unknown_or_incomplete_outputs(self):
        for overrides in (
            {"RELEASE_RESULT": ""},
            {"RELEASE_RESULT": "UNKNOWN"},
            {"RELEASE_VERSION": ""},
            {"RELEASE_NOTES": "   "},
            {"ACTION_NAME": "../example-action"},
        ):
            with self.subTest(overrides=overrides), patch.dict(os.environ, overrides):
                with self.assertRaises(ValueError):
                    monorepo.release_preview(self.root)
        with patch.dict(os.environ):
            del os.environ["RELEASE_RESULT"]
            with self.assertRaisesRegex(ValueError, "Missing release preview"):
                monorepo.release_preview(self.root)

    def test_release_notes_cannot_execute_runner_commands(self):
        os.environ["RELEASE_NOTES"] = "::error::literal\n::warning::literal\n"
        output = io.StringIO()
        with patch("sys.stdout", output):
            monorepo.release_preview(self.root)
        token = output.getvalue().splitlines()[0].removeprefix("::stop-commands::")
        protected, remaining = output.getvalue().split(f"::{token}::\n")
        self.assertIn("::error::literal\n", protected)
        self.assertEqual(remaining, "")


class DependabotAutomergeWorkflowTests(unittest.TestCase):
    def setUp(self):
        from ruamel.yaml import YAML

        self.workflow = YAML(typ="safe").load(
            (monorepo.ROOT / ".github" / "workflows" / "automerge-dependabot.yaml")
            .read_text(encoding="utf-8")
        )
        self.job = self.workflow["jobs"]["automerge"]
        self.step = self.job["steps"][0]
        self.head = "a" * 40
        self.request = {"owner": "example", "repo": "actions", "pull_number": 17}
        self.pr = {
            "user": {"login": "dependabot[bot]"},
            "base": {"ref": "main"},
            "state": "open",
            "draft": False,
            "head": {"sha": self.head},
        }
        self.context = {
            "repo": {"owner": "example", "repo": "actions"},
            "payload": {
                "repository": {"default_branch": "main"},
                "workflow_run": {
                    "event": "pull_request",
                    "conclusion": "success",
                    "actor": {"login": "human-rerunning-ci"},
                    "head_sha": self.head,
                    "pull_requests": [{"number": 17, "head": {"sha": self.head}}],
                },
            },
        }

    def run_script(self, *, pr=None, merge=None, error=None):
        fixture = {
            "script": self.step["with"]["script"],
            "context": self.context,
            "pr": self.pr if pr is None else pr,
            "merge": {"merged": True} if merge is None else merge,
            "error": error,
        }
        runner = """
            import fs from "node:fs";
            const fixture = JSON.parse(fs.readFileSync(0, "utf8"));
            const calls = [];
            const messages = [];
            const respond = (method, data) => async (request) => {
                calls.push({method, ...request});
                if (fixture.error?.method === method) {
                    throw new Error(fixture.error.message);
                }
                return {data};
            };
            const github = {rest: {pulls: {
                get: respond("get", fixture.pr),
                merge: respond("merge", fixture.merge),
            }}};
            const logger = {log: (message) => messages.push(message)};
            const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
            let error = null;
            try {
                await new AsyncFunction("github", "context", "console", fixture.script)(
                    github, fixture.context, logger,
                );
            } catch (failure) {
                error = failure.message;
            }
            process.stdout.write(JSON.stringify({calls, messages, error}));
        """
        result = subprocess.run(
            ["node", "--input-type=module", "-e", runner],
            input=json.dumps(fixture), capture_output=True, encoding="utf-8",
            check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_keeps_one_job_gated_on_successful_pr_ci(self):
        self.assertEqual(self.workflow["on"], {
            "workflow_run": {"workflows": ["CI"], "types": ["completed"]},
        })
        self.assertEqual(list(self.workflow["jobs"]), ["automerge"])
        self.assertEqual(" ".join(self.job["if"].split()), (
            "github.event.workflow_run.event == 'pull_request' && "
            "github.event.workflow_run.conclusion == 'success'"
        ))
        self.assertNotIn("strategy", self.job)
        self.assertNotIn("concurrency", self.job)
        self.assertNotIn("continue-on-error", self.job)

    def test_skips_missing_pr_associations_before_any_api_call(self):
        for associations in ([], None, [{}]):
            with self.subTest(associations=associations):
                self.context["payload"]["workflow_run"]["pull_requests"] = associations
                result = self.run_script()
                self.assertIsNone(result["error"])
                self.assertEqual(result["calls"], [])
                self.assertEqual(result["messages"], ["ℹ️ Skipped: no associated PR."])

    def test_scopes_write_permissions_and_pins_the_only_step(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertEqual(self.job["permissions"], {
            "contents": "write", "pull-requests": "write",
        })
        self.assertEqual(len(self.job["steps"]), 1)
        self.assertRegex(
            self.step["uses"], r"^actions/github-script@[0-9a-f]{40}$",
        )
        self.assertEqual(self.step["with"]["github-token"], "${{ secrets.GITHUB_TOKEN }}")
        self.assertNotIn("continue-on-error", self.step)

    def test_merges_the_ci_head_even_when_a_human_reruns_ci(self):
        result = self.run_script()
        self.assertIsNone(result["error"])
        self.assertEqual(result["calls"], [
            {"method": "get", **self.request},
            {"method": "merge", **self.request, "sha": self.head},
        ])
        self.assertEqual(result["messages"], ["✅ Merged PR #17."])

    def test_skips_ineligible_prs_without_merging(self):
        for changes in (
            {"user": {"login": "octocat"}},
            {"user": {"login": "github-actions[bot]"}},
            {"user": None},
            {"base": {"ref": "release/dummy"}},
            {"state": "closed"},
            {"state": "closed", "merged": True},
            {"draft": True},
        ):
            with self.subTest(changes=changes):
                result = self.run_script(pr={**self.pr, **changes})
                self.assertIsNone(result["error"])
                self.assertEqual(result["calls"], [{"method": "get", **self.request}])
                self.assertEqual(result["messages"], [
                    "ℹ️ Skipped PR #17: not an open, non-draft Dependabot PR "
                    "into the default branch.",
                ])

    def test_uses_the_repository_default_branch_instead_of_hardcoded_main(self):
        self.context["payload"]["repository"]["default_branch"] = "trunk"
        result = self.run_script()
        self.assertEqual(result["calls"], [{"method": "get", **self.request}])
        self.pr["base"]["ref"] = "trunk"
        result = self.run_script()
        self.assertIsNone(result["error"])
        self.assertEqual(result["calls"][-1], {
            "method": "merge", **self.request, "sha": self.head,
        })

    def test_skips_stale_ci_even_if_associated_pr_metadata_has_advanced(self):
        latest = "b" * 40
        self.context["payload"]["workflow_run"]["pull_requests"][0]["head"]["sha"] = latest
        self.pr["head"]["sha"] = latest
        result = self.run_script()
        self.assertIsNone(result["error"])
        self.assertEqual(result["calls"], [{"method": "get", **self.request}])
        self.assertEqual(result["messages"], [
            f"ℹ️ Skipped PR #17: head {latest} does not match successful CI {self.head}.",
        ])

    def test_unsuccessful_merge_response_is_an_error_not_a_success_message(self):
        result = self.run_script(merge={
            "merged": False, "message": "Required status checks have not passed",
        })
        self.assertEqual(
            result["error"],
            "❌ PR #17 was not merged: Required status checks have not passed",
        )
        self.assertEqual(result["messages"], [])

    def test_api_errors_including_concurrent_updates_are_not_hidden_or_retried(self):
        for method, message in (
            ("get", "403 Forbidden"),
            ("merge", "405 Required status checks have not passed"),
            ("merge", "409 Head branch was modified"),
            ("merge", "500 Internal Server Error"),
        ):
            with self.subTest(method=method, message=message):
                result = self.run_script(error={"method": method, "message": message})
                self.assertEqual(result["error"], message)
                self.assertEqual(result["messages"], [])
                self.assertEqual(
                    [call["method"] for call in result["calls"]],
                    ["get"] if method == "get" else ["get", "merge"],
                )


class DependabotAutomergeActionTests(unittest.TestCase):
    def setUp(self):
        from ruamel.yaml import YAML

        self.directory = monorepo.ROOT / "automerge-dependabot"
        self.action = YAML(typ="safe").load(
            (self.directory / "action.yml").read_text(encoding="utf-8")
        )
        self.step = self.action["runs"]["steps"][0]

    def run_script(self, *, token="test-token", associated=True, merge_result=None):
        with tempfile.TemporaryDirectory(prefix="automerge action ") as temporary:
            directory = Path(temporary)
            installed = directory / "installed action"
            installed.mkdir()
            (installed / "merge.cjs").write_bytes(
                (self.directory / "merge.cjs").read_bytes()
            )
            caller = directory / "unrelated caller"
            caller.mkdir()
            fixture = {
                "script": self.step["with"]["script"],
                "action_path": str(installed),
                "token": token,
                "associated": associated,
                "merge_result": {"merged": True} if merge_result is None else merge_result,
            }
            runner = """
                import fs from "node:fs";
                import {createRequire} from "node:module";
                const fixture = JSON.parse(fs.readFileSync(0, "utf8"));
                process.env.ACTION_PATH = fixture.action_path;
                process.env.ACTION_TOKEN = fixture.token;
                const head = "a".repeat(40);
                const calls = [];
                const outputs = {};
                const respond = (method, data) => async (request) => {
                    calls.push({method, ...request});
                    return {data};
                };
                const github = {rest: {pulls: {
                    get: respond("get", {
                        user: {login: "dependabot[bot]"}, base: {ref: "main"},
                        state: "open", draft: false, head: {sha: head},
                    }),
                    merge: respond("merge", fixture.merge_result),
                }}};
                const context = {
                    eventName: "workflow_run",
                    repo: {owner: "consumer", repo: "project"},
                    payload: {
                        action: "completed",
                        repository: {default_branch: "main"},
                        workflow_run: {
                            event: "pull_request", conclusion: "success", head_sha: head,
                            pull_requests: fixture.associated ? [{number: 17}] : [],
                        },
                    },
                };
                const core = {setOutput: (name, value) => {outputs[name] = String(value);}};
                const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
                let error = null;
                try {
                    await new AsyncFunction("github", "context", "core", "require", fixture.script)(
                        github, context, core, createRequire(import.meta.url),
                    );
                } catch (failure) {
                    error = failure.message;
                }
                process.stdout.write(JSON.stringify({calls, outputs, error}));
            """
            result = subprocess.run(
                ["node", "--input-type=module", "-e", runner],
                input=json.dumps(fixture), cwd=caller, capture_output=True,
                encoding="utf-8", check=False, timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_composite_pins_github_script_and_maps_inputs_and_outputs(self):
        self.assertEqual(self.action["runs"]["using"], "composite")
        self.assertEqual(len(self.action["runs"]["steps"]), 1)
        self.assertEqual(self.step["id"], "merge")
        self.assertRegex(
            self.step["uses"], r"^actions/github-script@[0-9a-f]{40}$",
        )
        self.assertEqual(list(self.action["inputs"]), ["github-token"])
        self.assertEqual(
            self.action["inputs"]["github-token"]["default"], "${{ github.token }}",
        )
        self.assertEqual(self.step["with"]["github-token"], "${{ inputs.github-token }}")
        self.assertEqual(self.step["env"], {
            "ACTION_PATH": "${{ github.action_path }}",
            "ACTION_TOKEN": "${{ inputs.github-token }}",
        })
        self.assertEqual(set(self.action["outputs"]), {"status", "reason", "pr-number"})
        for name, output in self.action["outputs"].items():
            self.assertEqual(output["value"], "${{ steps.merge.outputs." + name + " }}")
        self.assertNotIn("${{", self.step["with"]["script"])
        self.assertNotIn("continue-on-error", self.step)

    def test_dependabot_monitors_the_nested_external_action_pin(self):
        from ruamel.yaml import YAML

        dependabot = YAML(typ="safe").load(
            (monorepo.ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        )
        updates = next(
            update for update in dependabot["updates"]
            if update["package-ecosystem"] == "github-actions"
        )
        self.assertIn("/", updates["directories"])
        self.assertIn("/automerge-dependabot", updates["directories"])

    def test_bundled_script_works_from_another_repository_and_path_with_spaces(self):
        result = self.run_script()
        self.assertIsNone(result["error"])
        self.assertEqual(result["outputs"], {
            "status": "merged", "reason": "", "pr-number": "17",
        })
        request = {"owner": "consumer", "repo": "project", "pull_number": 17}
        self.assertEqual(result["calls"], [
            {"method": "get", **request},
            {"method": "merge", **request, "sha": "a" * 40},
        ])

    def test_expected_skips_are_exposed_as_outputs(self):
        result = self.run_script(associated=False)
        self.assertIsNone(result["error"])
        self.assertEqual(result["calls"], [])
        self.assertEqual(result["outputs"], {
            "status": "skipped", "reason": "no-associated-pr", "pr-number": "",
        })

    def test_empty_tokens_fail_without_api_calls_or_success_outputs(self):
        for token in ("", " \t\n"):
            with self.subTest(token=token):
                result = self.run_script(token=token)
                self.assertEqual(result["error"], "github-token must be a nonempty token.")
                self.assertEqual(result["calls"], [])
                self.assertEqual(result["outputs"], {})

    def test_merge_failure_does_not_emit_success_outputs(self):
        result = self.run_script(merge_result={
            "merged": False, "message": "Required status checks have not passed",
        })
        self.assertEqual(
            result["error"], "PR #17 was not merged: Required status checks have not passed",
        )
        self.assertEqual(result["outputs"], {})


class ReleasePreviewWorkflowTests(unittest.TestCase):
    def setUp(self):
        from ruamel.yaml import YAML

        self.workflow = YAML(typ="safe").load(
            (monorepo.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        self.plan = self.workflow["jobs"]["ParseChangelog"]
        self.preview = self.workflow["jobs"]["PreviewRelease"]

    def test_dependabot_uses_pr_ci_without_duplicate_push_checks(self):
        self.assertEqual(self.workflow["on"]["push"], {
            "branches": ["**", "!release/**", "!dependabot/**"],
        })
        self.assertEqual(self.workflow["on"]["pull_request"], {
            "types": ["opened", "synchronize", "reopened", "edited"],
        })
        self.assertEqual(set(self.workflow["on"]), {
            "push", "pull_request", "workflow_dispatch", "workflow_call",
        })

    def test_pr_retargeting_reruns_ci_and_pairing_is_checked_before_changelog_planning(self):
        self.assertEqual(
            self.workflow["on"]["pull_request"]["types"],
            ["opened", "synchronize", "reopened", "edited"],
        )
        steps = self.plan["steps"]
        setup = next(step for step in steps if step.get("name") == "Set up Python")
        pairing = next(
            step for step in steps
            if step.get("run") == "python .github/scripts/monorepo.py validate-pr-branches"
        )
        validator = next(
            step for step in steps
            if step.get("run") == "python .github/scripts/monorepo.py validate-changelog"
        )
        self.assertLess(steps.index(setup), steps.index(pairing))
        self.assertLess(steps.index(pairing), steps.index(validator))
        self.assertEqual(pairing["if"], "${{ github.event_name == 'pull_request' }}")
        self.assertEqual(pairing["env"], {
            "PR_HEAD_REF": "${{ github.head_ref }}",
            "PR_BASE_REF": "${{ github.base_ref }}",
        })
        self.assertNotIn("continue-on-error", pairing)

    def test_validates_the_whole_changelog_before_branch_scoped_release_planning(self):
        steps = self.plan["steps"]
        validator = next(
            step for step in steps
            if step.get("run") == "python .github/scripts/monorepo.py validate-changelog"
        )
        parser = next(step for step in steps if step.get("id") == "changelog")
        self.assertLess(steps.index(validator), steps.index(parser))
        self.assertNotIn("if", validator)
        self.assertNotIn("continue-on-error", validator)

    def test_connects_branch_scoped_matrix_and_skips_empty_or_deleted_refs(self):
        self.assertEqual(self.plan["outputs"], {
            "matrix": "${{ steps.changelog.outputs.matrix }}",
            "has-releases": "${{ steps.changelog.outputs.has-releases }}",
        })
        parser = next(step for step in self.plan["steps"] if step.get("id") == "changelog")
        self.assertEqual(parser["run"], "python .github/scripts/monorepo.py changelog")
        self.assertEqual(parser["env"]["CHANGELOG_BRANCH"], (
            "${{ inputs.target-branch || github.base_ref || "
            "needs.CaptureContext.outputs.action-target-branch }}"
        ))
        self.assertEqual(self.preview["needs"], ["CaptureContext", "ParseChangelog"])
        self.assertEqual(self.preview["if"], (
            "${{ !inputs.skip-release-preview && "
            "needs.ParseChangelog.outputs.has-releases == 'true' "
            "&& github.event.deleted != true }}"
        ))
        self.assertEqual(self.preview["strategy"], {
            "fail-fast": False,
            "matrix": "${{ fromJSON(needs.ParseChangelog.outputs.matrix) }}",
        })

    def test_passes_each_sections_inputs_to_pinned_read_only_dry_run(self):
        semrelease = next(
            step for step in self.preview["steps"] if step.get("id") == "semrelease"
        )
        self.assertEqual(semrelease["uses"], (
            "btnguyen2k/action-semrelease@37fe196d36496098bd291e9519c86772ea2e0c0d"
        ))
        self.assertEqual(semrelease["with"], {
            "github-token": "${{ github.token }}",
            "dry-run": True,
            "target-sha": "${{ needs.CaptureContext.outputs.target-sha }}",
            "branches": "${{ github.ref }}",
            "tag-prefix": "${{ matrix.tag-prefix }}",
            "version": "${{ matrix.version }}",
            "change-log": "${{ matrix.change-log }}",
        })
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        self.assertNotIn("permissions", self.preview)
        self.assertNotIn("continue-on-error", semrelease)

    def test_captures_outputs_per_action_and_reports_failures_without_hiding_them(self):
        report = self.preview["steps"][-1]
        self.assertEqual(report["run"], "python .github/scripts/monorepo.py release-preview")
        self.assertEqual(report["env"], {
            "ACTION_NAME": "${{ matrix.action }}",
            "RELEASE_RESULT": "${{ steps.semrelease.outputs.result }}",
            "RELEASE_VERSION": "${{ steps.semrelease.outputs.releaseVersion }}",
            "RELEASE_NOTES": "${{ steps.semrelease.outputs.releaseNotes }}",
        })
        self.assertEqual(report["if"], (
            "${{ !cancelled() && (steps.semrelease.outcome == 'success' "
            "|| steps.semrelease.outcome == 'failure') }}"
        ))
        self.assertNotIn("outputs", self.preview)
        self.assertNotIn("continue-on-error", self.preview)


class CIWorkflowTestCase(unittest.TestCase):
    def setUp(self):
        from ruamel.yaml import YAML

        workflow = YAML(typ="safe").load(
            (monorepo.ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        self.jobs = workflow["jobs"]
        self.bash = "bash"
        if sys.platform == "win32":
            native = Path(monorepo.capture(
                monorepo.ROOT, "git", "var", "GIT_SHELL_PATH",
            )).with_name("bash.exe")
            self.assertTrue(native.is_absolute() and native.is_file())
            self.bash = str(native)

    def run_step(self, step, environment):
        return subprocess.run(
            [
                self.bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
                step["run"],
            ],
            cwd=monorepo.ROOT, env={**os.environ, **environment},
            capture_output=True, encoding="utf-8", check=False, timeout=10,
        )


class CIContextWorkflowTests(CIWorkflowTestCase):
    def setUp(self):
        super().setUp()
        self.step = next(
            step for step in self.jobs["CaptureContext"]["steps"]
            if step.get("name") == "Print workflow context"
        )

    def test_binds_context_fields_without_changing_outputs_or_release_scope(self):
        self.assertEqual(self.step["shell"], "bash")
        self.assertNotIn("${{", self.step["run"])
        self.assertEqual(self.jobs["CaptureContext"]["outputs"], {
            "target-sha": "${{ steps.context.outputs.target-sha }}",
            "action-target-branch": "${{ inputs.target-branch || steps.context.outputs.action-target-branch }}",
        })
        planner = next(
            step for step in self.jobs["ParseChangelog"]["steps"]
            if step.get("id") == "changelog"
        )
        self.assertEqual(self.step["env"], {
            "EVENT_NAME": "${{ steps.context.outputs.event-name }}",
            "VALIDATION_SHA": "${{ steps.context.outputs.target-sha }}",
            "WORKFLOW_REF": "${{ steps.context.outputs.ref }}",
            "BRANCH": "${{ steps.context.outputs.action-target-branch }}",
            "TAG": "${{ steps.context.outputs.tag }}",
            "IS_PR": "${{ steps.context.outputs.is-pr }}",
            "PR_NUMBER": "${{ steps.context.outputs.pr-number }}",
            "PR_SOURCE_BRANCH": "${{ steps.context.outputs.pr-head-ref }}",
            "PR_SOURCE_SHA": "${{ steps.context.outputs.pr-head-sha }}",
            "PR_TARGET_BRANCH": "${{ steps.context.outputs.pr-base-ref }}",
            "RELEASE_SCOPE_BRANCH": planner["env"]["CHANGELOG_BRANCH"].replace(
                "needs.CaptureContext.outputs.", "steps.context.outputs.",
            ),
        })

    def test_prints_context_appropriate_branches_refs_and_shas(self):
        pr = {
            "EVENT_NAME": "pull_request", "WORKFLOW_REF": "refs/pull/12/merge",
            "BRANCH": "dev/dummy", "IS_PR": "true", "PR_NUMBER": "12",
            "PR_SOURCE_BRANCH": "dev/dummy", "PR_SOURCE_SHA": "b" * 40,
            "PR_TARGET_BRANCH": "release/dummy", "RELEASE_SCOPE_BRANCH": "release/dummy",
        }
        pr_lines = [
            "Workflow ref: refs/pull/12/merge", "PR number: 12",
            "PR source branch: dev/dummy", f"PR source SHA: {'b' * 40}",
            "PR target branch: release/dummy",
        ]
        cases = (
            ("pull request", pr, [*pr_lines, "Release scope branch: release/dummy"]),
            ("branch push", {
                "EVENT_NAME": "push", "WORKFLOW_REF": "refs/heads/dev/dummy",
                "BRANCH": "dev/dummy", "RELEASE_SCOPE_BRANCH": "dev/dummy",
            }, [
                "Workflow ref: refs/heads/dev/dummy", "Branch: dev/dummy",
                "Release scope branch: dev/dummy",
            ]),
            ("manual branch run", {
                "EVENT_NAME": "workflow_dispatch", "WORKFLOW_REF": "refs/heads/main",
                "BRANCH": "main", "RELEASE_SCOPE_BRANCH": "main",
            }, [
                "Workflow ref: refs/heads/main", "Branch: main", "Release scope branch: main",
            ]),
            ("tag ref", {
                "EVENT_NAME": "push", "WORKFLOW_REF": "refs/tags/dummy/v1.2.3",
                "TAG": "dummy/v1.2.3",
            }, [
                "Workflow ref: refs/tags/dummy/v1.2.3", "Tag: dummy/v1.2.3",
            ]),
            ("explicit validation and scope overrides", {
                **pr, "VALIDATION_SHA": "b" * 40, "RELEASE_SCOPE_BRANCH": "testing/dummy",
            }, [*pr_lines, "Release scope branch: testing/dummy"]),
            ("partial PR details", {
                "EVENT_NAME": "issue_comment", "IS_PR": "true", "PR_NUMBER": "12",
                "WORKFLOW_REF": "refs/heads/main",
            }, [
                "Workflow ref: refs/heads/main", "PR number: 12",
                "PR source branch: unknown", "PR source SHA: unknown",
                "PR target branch: unknown",
            ]),
            ("branch names are literal data", {
                **pr, "PR_SOURCE_BRANCH": "dev/$(printf not-executed)",
            }, [
                "Workflow ref: refs/pull/12/merge", "PR number: 12",
                "PR source branch: dev/$(printf not-executed)", f"PR source SHA: {'b' * 40}",
                "PR target branch: release/dummy", "Release scope branch: release/dummy",
            ]),
        )
        for name, overrides, expected in cases:
            environment = {
                **dict.fromkeys(self.step["env"], ""),
                "IS_PR": "false", "VALIDATION_SHA": "a" * 40, **overrides,
            }
            with self.subTest(name=name):
                result = self.run_step(self.step, environment)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stdout.splitlines(), [
                    f"ℹ️ Event: {environment['EVENT_NAME']}",
                    f"ℹ️ Validation SHA: {environment['VALIDATION_SHA']}",
                    *(f"ℹ️ {line}" for line in expected),
                ])
                self.assertEqual(result.stderr, "")


class CIMarkerWorkflowTests(CIWorkflowTestCase):
    def run_marker(self, name, environment):
        return self.run_step(self.jobs[name]["steps"][0], environment)

    def test_marker_jobs_have_stable_names_and_run_after_failed_dependencies(self):
        for name, needs in (
            ("ValidatePass", "Validate"),
            ("PreviewReleasePass", ["ParseChangelog", "PreviewRelease"]),
        ):
            with self.subTest(name=name):
                marker = self.jobs[name]
                self.assertEqual(marker["name"], name)
                self.assertEqual(marker["needs"], needs)
                self.assertEqual(marker["if"], "${{ always() }}")
                self.assertEqual(marker["runs-on"], "ubuntu-latest")
                self.assertEqual(marker["timeout-minutes"], 5)
                self.assertNotIn("strategy", marker)
                self.assertNotIn("continue-on-error", marker)
                self.assertEqual(len(marker["steps"]), 1)
                self.assertEqual(marker["steps"][0]["shell"], "bash")
                self.assertNotIn("continue-on-error", marker["steps"][0])

    def test_marker_inputs_distinguish_intentional_skips_from_upstream_errors(self):
        self.assertEqual(self.jobs["ValidatePass"]["steps"][0]["env"], {
            "JOB_RESULT": "${{ needs.Validate.result }}",
        })
        self.assertEqual(self.jobs["PreviewReleasePass"]["steps"][0]["env"], {
            "CHANGELOG_RESULT": "${{ needs.ParseChangelog.result }}",
            "PREVIEW_RESULT": "${{ needs.PreviewRelease.result }}",
            "SKIP_EXPECTED": (
                "${{ inputs.skip-release-preview "
                "|| needs.ParseChangelog.outputs.has-releases == 'false' "
                "|| github.event.deleted == true }}"
            ),
        })

    def test_validation_marker_requires_success(self):
        for status in ("success", "failure", "cancelled", "skipped", ""):
            with self.subTest(status=status):
                result = self.run_marker("ValidatePass", {"JOB_RESULT": status})
                self.assertEqual(
                    result.returncode == 0, status == "success", result.stdout + result.stderr,
                )
                if status != "success":
                    self.assertIn("::error::❌ Validate", result.stdout)
                else:
                    self.assertIn("✅ All validation jobs passed.", result.stdout)

    def test_preview_marker_requires_successful_planning_and_success_or_intentional_skip(self):
        statuses = ("success", "failure", "cancelled", "skipped", "")
        for changelog in statuses:
            for preview in statuses:
                for skip in ("true", "false"):
                    with self.subTest(changelog=changelog, preview=preview, skip=skip):
                        result = self.run_marker("PreviewReleasePass", {
                            "CHANGELOG_RESULT": changelog,
                            "PREVIEW_RESULT": preview,
                            "SKIP_EXPECTED": skip,
                        })
                        expected = changelog == "success" and (
                            preview == "success" or preview == "skipped" and skip == "true"
                        )
                        self.assertEqual(
                            result.returncode == 0, expected, result.stdout + result.stderr,
                        )
                        if not expected:
                            self.assertIn("::error::❌ ", result.stdout)
                        elif preview == "skipped":
                            self.assertIn("ℹ️ Release preview was intentionally skipped.", result.stdout)
                        else:
                            self.assertIn("✅ All release previews passed.", result.stdout)


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

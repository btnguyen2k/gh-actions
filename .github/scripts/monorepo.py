import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTION_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def validate_name(name: str) -> None:
    if not ACTION_NAME.fullmatch(name):
        raise ValueError("Action names must use lowercase kebab-case.")


def parse_version(value: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(value)
    if not match:
        raise ValueError("Version must be MAJOR.MINOR.PATCH without v or leading zeroes.")
    return int(match[1]), int(match[2]), int(match[3])


def actions(root: Path) -> list[Path]:
    if any((root / name).exists() for name in ("action.yml", "action.yaml")):
        raise ValueError("Public actions belong in separate root-level directories.")
    result = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.name.startswith("."):
            continue
        if (directory / "action.yaml").exists():
            raise ValueError(f"{directory.name}: use action.yml, not action.yaml.")
        if not (directory / "action.yml").exists():
            continue
        validate_name(directory.name)
        for filename in ("action.yml", "README.md", "RELEASE-NOTES.md", "tests/run.sh"):
            if not (directory / filename).is_file():
                raise ValueError(f"{directory.name}: missing {filename}.")
        result.append(directory)
    return result


def validate(root: Path) -> None:
    action_files = [directory / "action.yml" for directory in actions(root)]
    workflow_directory = root / ".github" / "workflows"
    workflows = sorted([
        *workflow_directory.glob("*.yml"),
        *workflow_directory.glob("*.yaml"),
    ])
    if not workflows:
        raise ValueError("No GitHub workflow files found.")
    groups = [
        ("github-workflows", workflows),
        ("dependabot", [root / ".github" / "dependabot.yml"]),
    ]
    if action_files:
        groups.append(("github-actions", action_files))
    else:
        print("No actions found; action metadata validation is not yet applicable.")
    for schema, files in groups:
        print(f"Validating {schema}:", flush=True)
        for path in files:
            print(f"  - {path.relative_to(root).as_posix()}", flush=True)
        subprocess.run(
            [sys.executable, "-m", "check_jsonschema", "--builtin-schema",
             f"vendor.{schema}", *map(str, files)],
            cwd=root,
            check=True,
        )


def test_actions(root: Path) -> None:
    directories = actions(root)
    if not directories:
        print("No actions found; action tests will run when the first action is added.")
        return
    bash = "bash"
    if sys.platform == "win32":
        # Windows can resolve bare bash to WSL even inside a Git Bash step.
        native = Path(capture(root, "git", "var", "GIT_SHELL_PATH")).with_name("bash.exe")
        if not native.is_absolute() or not native.is_file():
            raise FileNotFoundError(
                f"Git for Windows Bash not found at {native}. "
                "Install Git for Windows; WSL is not supported."
            )
        bash = str(native)
    for directory in directories:
        print(f"Testing {directory.name}", flush=True)
        subprocess.run([bash, "tests/run.sh"], cwd=directory, check=True)


@dataclass(frozen=True)
class ReleaseSection:
    action: str
    version: str
    notes: str
    line: int


class ChangelogError(ValueError):
    def __init__(self, message: str, warnings: list[tuple[int, str]]) -> None:
        super().__init__(message)
        self.warnings = warnings


def parse_changelog(
    text: str, known_actions: set[str], selected_action: str | None = None
) -> tuple[list[ReleaseSection], list[tuple[int, str]]]:
    lines = text.splitlines()
    headers = [
        index for index, line in enumerate(lines) if line.lstrip().startswith("#!")
    ]
    sections = []
    warnings = []
    header_lines: dict[str, list[int]] = {}
    if selected_action is None:
        for index in range(headers[0] if headers else len(lines)):
            if lines[index].strip() and not lines[index].lstrip().startswith("#"):
                warnings.append((
                    index + 1, "Changelog content outside a #! release section is ignored.",
                ))
                break
    for position, index in enumerate(headers):
        header = lines[index]
        candidate = re.match(r"\s*#!\s*([^/\s]+)", header)
        if selected_action is not None and (
            candidate is None or candidate[1] != selected_action
        ):
            continue
        match = re.fullmatch(r"#! *([^/\s]+)(?:/v(\S+))?", header.rstrip())
        if not match:
            warnings.append((
                index + 1,
                f"Invalid release section header {header!r}; "
                "expected #!<action-name> or #!<action-name>/v<version-number>.",
            ))
            continue
        action = match[1]
        version = match[2] or ""
        try:
            validate_name(action)
            if version:
                parse_version(version)
        except ValueError as error:
            warnings.append((index + 1, f"Invalid release section {header!r}: {error}"))
            continue
        if action not in known_actions:
            warnings.append((index + 1, f"Unknown action {action!r}: no action.yml found."))
            continue
        header_lines.setdefault(action, []).append(index + 1)
        end = headers[position + 1] if position + 1 < len(headers) else len(lines)
        notes = "\n".join(
            line for line in lines[index + 1:end] if not line.lstrip().startswith("#")
        ).strip("\n")
        if not notes.strip():
            label = f"{action}/v{version}" if version else action
            warnings.append((index + 1, f"Release section {label} has no changelog entries."))
            continue
        sections.append(ReleaseSection(action, version, notes, index + 1))
    duplicates = [
        f"Multiple release sections for action {action!r} at lines "
        f"{', '.join(map(str, positions))}; keep exactly one section per action."
        for action, positions in header_lines.items() if len(positions) > 1
    ]
    warnings.sort()
    if duplicates:
        raise ChangelogError("\n".join(duplicates), warnings)
    return sections, warnings


def print_changelog_warnings(warnings: list[tuple[int, str]]) -> None:
    for line, message in warnings:
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::warning file=.semrelease/this_release,line={line}::{escaped}", flush=True)


def load_changelog(
    root: Path, selected_action: str | None = None
) -> tuple[list[ReleaseSection], list[tuple[int, str]]]:
    try:
        return parse_changelog(
            (root / ".semrelease" / "this_release").read_text(encoding="utf-8-sig"),
            {directory.name for directory in actions(root)},
            selected_action,
        )
    except ChangelogError as error:
        print_changelog_warnings(error.warnings)
        raise


def validate_changelog(root: Path) -> None:
    sections, warnings = load_changelog(root)
    print_changelog_warnings(warnings)
    print(f"Changelog validation passed ({len(sections)} non-empty sections).", flush=True)


def changelog(root: Path) -> None:
    branch = os.environ.get("CHANGELOG_BRANCH", "")
    prefix, separator, action = branch.partition("/")
    selected_action = action if separator and prefix else None
    sections, warnings = load_changelog(root, selected_action)
    if selected_action is not None and not sections:
        warnings.append((
            1,
            f"No valid, non-empty release sections found for action {selected_action!r} "
            f"on branch {branch!r}.",
        ))
    scope = f"action {selected_action!r}" if selected_action is not None else "all actions"
    report = [
        f"Branch: {branch or '(not a branch)'}",
        f"Release scope: {scope}",
        f"Pending releases: {len(sections)}",
    ]
    for section in sections:
        report.extend((
            "",
            f"Action: {section.action}",
            f"Version: {section.version or '(automatic)'}",
            f"Tag prefix: {section.action}/v",
            "Changelog:",
            section.notes,
        ))
    print_workflow_data("\n".join(report))
    print_changelog_warnings(warnings)
    if output_path := os.environ.get("GITHUB_OUTPUT"):
        matrix = {"include": [
            {
                "action": section.action,
                "version": section.version,
                "tag-prefix": f"{section.action}/v",
                "change-log": section.notes,
            }
            for section in sections
        ]}
        with Path(output_path).open("a", encoding="utf-8", newline="\n") as output:
            output.write(f"matrix={json.dumps(matrix, ensure_ascii=True)}\n")
            output.write(f"has-releases={'true' if sections else 'false'}\n")


def print_workflow_data(output: str) -> None:
    token = uuid.uuid4().hex
    while token in output:
        token = uuid.uuid4().hex
    # Reports may contain untrusted messages, not runner commands.
    print(f"::stop-commands::{token}", flush=True)
    print(output, flush=True)
    print(f"::{token}::", flush=True)


def release_preview(_root: Path) -> None:
    required = ("ACTION_NAME", "RELEASE_RESULT", "RELEASE_VERSION", "RELEASE_NOTES")
    missing = [name for name in required if name not in os.environ]
    if missing:
        raise ValueError(f"Missing release preview environment variables: {', '.join(missing)}.")
    action = os.environ["ACTION_NAME"]
    validate_name(action)
    result = os.environ["RELEASE_RESULT"]
    version = os.environ["RELEASE_VERSION"]
    notes = os.environ["RELEASE_NOTES"]
    if result not in ("SUCCESS", "SKIPPED", "FAILED"):
        raise ValueError(f"Invalid release preview result: {result!r}.")
    if result == "SUCCESS" and (not version.strip() or not notes.strip()):
        raise ValueError("A successful release preview must include releaseVersion and releaseNotes.")
    print_workflow_data("\n".join((
        f"Action: {action}",
        f"result: {result}",
        f"releaseVersion: {version}",
        "releaseNotes:",
        notes,
    )))
    if summary_path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary_path).open("a", encoding="utf-8", newline="\n") as summary:
            summary.write(
                f"## Release preview: `{action}`\n\n"
                f"- result: `{result}`\n"
                f"- releaseVersion: `{version}`\n\n"
                f"### releaseNotes\n\n{notes}\n\n"
            )
    if result == "FAILED":
        raise ValueError(f"Release preview for {action} failed; see action-semrelease logs.")


def capture(root: Path, *command: str, stdin: str | None = None) -> str:
    return subprocess.run(
        command, cwd=root, input=stdin, text=True, capture_output=True, check=True
    ).stdout.strip()


def tag_state(root: Path, action: str) -> tuple[dict[str, str], dict[str, str]]:
    objects = {}
    commits = {}
    prefix = f"{action}/v"
    lines = capture(
        root, "git", "for-each-ref", "--format=%(refname:strip=2) %(objectname)",
        f"refs/tags/{action}/",
    ).splitlines()
    for line in lines:
        name, object_id = line.split()
        objects[name] = object_id
        if name.startswith(prefix) and VERSION.fullmatch(name[len(prefix):]):
            commits[name] = capture(
                root, "git", "rev-parse", "--verify", f"refs/tags/{name}^{{commit}}"
            )
    return objects, commits


def release_targets(
    action: str, version: str, commit: str, tags: dict[str, str]
) -> dict[str, str]:
    validate_name(action)
    numbers = parse_version(version)
    exact = f"{action}/v{version}"
    if exact in tags and tags[exact] != commit:
        raise ValueError(f"{exact} already exists at a different commit; use a new version.")
    prefix = f"{action}/v"
    releases = {
        parse_version(name[len(prefix):]): target
        for name, target in tags.items()
        if name.startswith(prefix) and VERSION.fullmatch(name[len(prefix):])
    }
    releases[numbers] = commit
    major = max(value for value in releases if value[0] == numbers[0])
    minor = max(value for value in releases if value[:2] == numbers[:2])
    return {
        exact: commit,
        f"{action}/v{numbers[0]}": releases[major],
        f"{action}/v{numbers[0]}.{numbers[1]}": releases[minor],
    }


def push_tags(
    root: Path, targets: dict[str, str], objects: dict[str, str], exact: str
) -> None:
    # Preserve existing full-version tag objects, including annotated tags.
    updates = {
        name: commit for name, commit in targets.items()
        if (name != exact or name not in objects) and objects.get(name) != commit
    }
    if not updates:
        print("Release tags already match; completing the missing GitHub release.")
        return
    leases = [
        f"--force-with-lease=refs/tags/{name}:{objects.get(name, '')}"
        for name in updates
    ]
    refspecs = [f"{commit}:refs/tags/{name}" for name, commit in updates.items()]
    capture(root, "git", "push", "--atomic", *leases, "origin", *refspecs)


def release(root: Path) -> None:
    required = (
        "ACTION_NAME", "ACTION_VERSION", "RELEASE_NOTES", "GITHUB_SHA",
        "GITHUB_REPOSITORY", "GITHUB_REF", "DEFAULT_BRANCH", "GH_TOKEN",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ValueError(f"Missing release environment variables: {', '.join(missing)}.")
    if os.environ["GITHUB_REF"] != f"refs/heads/{os.environ['DEFAULT_BRANCH']}":
        raise ValueError("Releases must run from the repository's default branch.")
    action = os.environ["ACTION_NAME"]
    version = os.environ["ACTION_VERSION"]
    validate_name(action)
    parse_version(version)
    if action not in {directory.name for directory in actions(root)}:
        raise ValueError(f"No action.yml found for action {action}.")
    commit = capture(root, "git", "rev-parse", "--verify", "HEAD^{commit}")
    if commit != os.environ["GITHUB_SHA"]:
        raise ValueError("Checkout does not match the commit validated by the workflow.")
    repository = os.environ["GITHUB_REPOSITORY"]
    exact = f"{action}/v{version}"
    existing = capture(
        root, "gh", "api", "--paginate", f"repos/{repository}/releases",
        "--jq", ".[].tag_name",
    ).splitlines()
    if exact in existing:
        raise ValueError(f"GitHub release {exact} already exists.")
    capture(root, "gh", "auth", "setup-git", "--hostname", "github.com")
    capture(root, "git", "fetch", "--tags", "--force", "origin")
    objects, commits = tag_state(root, action)
    targets = release_targets(action, version, commit, commits)
    for name, target in targets.items():
        print(f"{name} -> {target}", flush=True)
    push_tags(root, targets, objects, exact)
    print(capture(
        root, "gh", "release", "create", exact, "--repo", repository,
        "--verify-tag", "--title", exact, "--notes-file", "-", "--latest=false",
        stdin=os.environ["RELEASE_NOTES"],
    ))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate, test, plan or preview releases, or release this action monorepo."
    )
    parser.add_argument(
        "command", choices=(
            "validate", "test", "validate-changelog", "changelog", "release-preview", "release",
        ),
    )
    args = parser.parse_args()
    try:
        {
            "validate": validate, "test": test_actions,
            "validate-changelog": validate_changelog, "changelog": changelog,
            "release-preview": release_preview, "release": release,
        }[args.command](ROOT)
    except (ValueError, FileNotFoundError) as error:
        parser.exit(1, f"error: {error}\n")
    except subprocess.CalledProcessError as error:
        parser.exit(1, f"error: {error.stderr or str(error)}\n")


if __name__ == "__main__":
    main()

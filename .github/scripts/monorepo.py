import argparse
import os
import re
import subprocess
import sys
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
        for filename in ("action.yml", "README.md", "tests/run.sh"):
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
    parser = argparse.ArgumentParser(description="Validate, test, or release this action monorepo.")
    parser.add_argument("command", choices=("validate", "test", "release"))
    args = parser.parse_args()
    try:
        {"validate": validate, "test": test_actions, "release": release}[args.command](ROOT)
    except (ValueError, FileNotFoundError) as error:
        parser.exit(1, f"error: {error}\n")
    except subprocess.CalledProcessError as error:
        parser.exit(1, f"error: {error.stderr or str(error)}\n")


if __name__ == "__main__":
    main()

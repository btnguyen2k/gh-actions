import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import monorepo


SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
WORKFLOW = ".github/workflows/release.yml"
CI_JOBS = (
    "Parse release changelog",
    "Validate and test (ubuntu-latest)",
    "Validate and test (windows-latest)",
    "Validate and test (macos-latest)",
)
BOT = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


def run(root: Path, *command: str, stdin: bytes | None = None, env=None) -> bytes:
    return subprocess.run(
        command, cwd=root, input=stdin, env=env, capture_output=True, check=True
    ).stdout


def git(root: Path, *arguments: str, stdin: bytes | None = None, env=None) -> str:
    return run(root, "git", *arguments, stdin=stdin, env=env).decode("utf-8").strip()


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def positive_integer(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("Run IDs, attempts, and PR numbers must be positive integers.")
    return int(value)


def emit(**values) -> None:
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8", newline="\n") as stream:
            for name, value in values.items():
                text = str(value).lower() if isinstance(value, bool) else str(value)
                if "\n" in text or "\r" in text:
                    raise ValueError("Workflow outputs must be single-line or JSON-encoded.")
                stream.write(f"{name.replace('_', '-')}={text}\n")


@dataclass(frozen=True)
class Cycle:
    repository: str
    action: str
    branch: str
    merge_sha: str
    source_run_id: int
    source_pr: int
    default_branch: str

    def validate(self) -> None:
        for name in ("repository", "action", "branch", "merge_sha", "default_branch"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"Invalid release cycle field: {name}.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("Invalid release repository.")
        monorepo.validate_name(self.action)
        if self.branch != f"release/{self.action}" or not SHA.fullmatch(self.merge_sha):
            raise ValueError("Release cycles require release/<action> and a pinned merge SHA.")
        if not self.default_branch or any(c in self.default_branch for c in "\r\n\0"):
            raise ValueError("Invalid default branch.")
        for value in (self.source_run_id, self.source_pr):
            if type(value) is not int or value < 1:
                raise ValueError("Invalid source run or PR number.")

    @classmethod
    def environment(cls):
        cycle = cls(
            os.environ["GITHUB_REPOSITORY"], os.environ["ACTION_NAME"],
            os.environ["RELEASE_BRANCH"], os.environ["MERGE_SHA"],
            positive_integer(os.environ["SOURCE_RUN_ID"]),
            positive_integer(os.environ["SOURCE_PR"]), os.environ["DEFAULT_BRANCH"],
        )
        cycle.validate()
        return cycle

    @property
    def marker(self) -> str:
        return f"<!-- action-release:{self.action}:{self.merge_sha} -->"


@dataclass(frozen=True)
class Plan:
    cycle: Cycle
    stage: str
    version: str
    requested_version: str
    change_log: str
    release_notes: str
    timestamp: str
    changelog_digest: str
    section_digest: str
    ci_run_id: int
    ci_run_attempt: int
    ci_target_sha: str
    release_sha: str = ""
    history_digest: str = ""
    cleanup_sha: str = ""
    schema: int = 1

    def validate(self) -> None:
        self.cycle.validate()
        if type(self.schema) is not int or self.schema != 1:
            raise ValueError("Unsupported release plan schema.")
        for name in (
            "stage", "version", "requested_version", "change_log", "release_notes",
            "timestamp", "changelog_digest", "section_digest", "ci_target_sha",
            "release_sha", "history_digest", "cleanup_sha",
        ):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"Invalid release plan field: {name}.")
        if self.stage not in ("calculated", "prepared", "frozen"):
            raise ValueError("Invalid release plan stage.")
        monorepo.parse_version(self.version)
        if self.requested_version:
            monorepo.parse_version(self.requested_version)
            if self.requested_version != self.version:
                raise ValueError("Calculated version does not match the fixed section version.")
        if not self.change_log.strip() or not self.release_notes.strip():
            raise ValueError("Release changelog and generated notes must not be empty.")
        date = datetime.fromisoformat(self.timestamp)
        if date.tzinfo is None or date.utcoffset().total_seconds() != 0:
            raise ValueError("Release timestamps must use UTC.")
        for value in (self.changelog_digest, self.section_digest):
            if not DIGEST.fullmatch(value):
                raise ValueError("Invalid changelog digest.")
        for value in (self.ci_run_id, self.ci_run_attempt):
            if type(value) is not int or value < 1:
                raise ValueError("Invalid CI evidence.")
        if self.ci_run_id != self.cycle.source_run_id:
            raise ValueError("CI must belong to this release workflow run.")
        if self.ci_target_sha != self.cycle.merge_sha:
            raise ValueError("CI did not select the pinned release merge SHA.")
        if self.stage != "calculated":
            if not SHA.fullmatch(self.release_sha) or not SHA.fullmatch(self.cleanup_sha):
                raise ValueError("Prepared plans require release and cleanup SHAs.")
            if not DIGEST.fullmatch(self.history_digest):
                raise ValueError("Prepared plans require a release-notes digest.")
        elif self.release_sha or self.cleanup_sha or self.history_digest:
            raise ValueError("Calculated plans must not claim prepared commits.")

    @classmethod
    def decode(cls, content: bytes):
        try:
            payload = json.loads(content)
            payload["cycle"] = Cycle(**payload["cycle"])
            plan = cls(**payload)
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError("Malformed release plan.") from error
        plan.validate()
        return plan

    @property
    def tag(self) -> str:
        return f"{self.cycle.action}/v{self.version}"


def save(path: Path, plan: Plan) -> None:
    plan.validate()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(plan), stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load(path: Path, cycle: Cycle) -> Plan:
    plan = Plan.decode(path.read_bytes())
    if plan.cycle != cycle:
        raise ValueError("Release state belongs to a different cycle.")
    return plan


class GitHub:
    def __init__(self, root: Path):
        self.root = root
        self.repository = os.environ["GITHUB_REPOSITORY"]
        self.host = urlparse(os.environ.get("GITHUB_SERVER_URL", "https://github.com")).hostname
        self.runs = {}

    def api(self, path: str, method: str = "GET", data=None):
        command = ["gh", "api", "--hostname", self.host, "--method", method, path]
        content = None
        if data is not None:
            command.extend(("--input", "-"))
            content = json.dumps(data, ensure_ascii=True).encode("utf-8")
        return json.loads(run(self.root, *command, stdin=content))

    def optional(self, path: str):
        try:
            return self.api(path)
        except subprocess.CalledProcessError as error:
            detail = error.stderr or b""
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
            if "(HTTP 404)" not in detail:
                raise
            return None

    def pages(self, path: str, key: str | None = None) -> list:
        values = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            response = self.api(f"{path}{separator}per_page=100&page={page}")
            batch = response[key] if key else response
            if not isinstance(batch, list):
                raise ValueError("GitHub returned an invalid paginated response.")
            values.extend(batch)
            if len(batch) < 100:
                return values
            page += 1

    def workflow_run(self, run_id: int):
        if run_id not in self.runs:
            self.runs[run_id] = self.api(
                f"repos/{self.repository}/actions/runs/{run_id}"
            )
        return self.runs[run_id]


def matching_merge(pr, cycle: Cycle) -> bool:
    return (
        pr["merged_at"] is not None and pr["merge_commit_sha"] == cycle.merge_sha
        and pr["base"]["ref"] == cycle.branch
        and pr["base"]["repo"]["full_name"] == cycle.repository
    )


def verify_source(client: GitHub, cycle: Cycle) -> None:
    if cycle.source_run_id != positive_integer(os.environ["GITHUB_RUN_ID"]):
        raise ValueError("Release must use the current workflow run; reconcile interrupted releases manually.")
    source = client.workflow_run(cycle.source_run_id)
    if (
        source["repository"]["full_name"] != cycle.repository
        or source["path"].split("@")[0] != WORKFLOW
        or source["event"] != "push" or source["head_sha"] != cycle.merge_sha
        or source["head_branch"] != cycle.branch
    ):
        raise ValueError("The source run does not identify the original release push.")
    pr = client.api(f"repos/{cycle.repository}/pulls/{cycle.source_pr}")
    if not matching_merge(pr, cycle):
        raise ValueError("The pinned release SHA is not the verified PR merge.")
    monorepo.validate_release_pr_branches(pr["head"]["ref"], pr["base"]["ref"])


def verify_ci(client: GitHub, plan: Plan) -> None:
    verify_source(client, plan.cycle)
    if (
        plan.ci_run_id != plan.cycle.source_run_id
        or plan.ci_run_attempt != positive_integer(os.environ["GITHUB_RUN_ATTEMPT"])
    ):
        raise ValueError("CI evidence must identify the current release run and attempt.")
    jobs = client.pages(
        f"repos/{client.repository}/actions/runs/{plan.ci_run_id}"
        f"/attempts/{plan.ci_run_attempt}/jobs", "jobs",
    )
    for name in CI_JOBS:
        matches = [
            job for job in jobs
            if job["name"] == name or job["name"].endswith(f" / {name}")
        ]
        if not matches or any(
            job["run_attempt"] != plan.ci_run_attempt
            or job["status"] != "completed" or job["conclusion"] != "success"
            for job in matches
        ):
            raise ValueError(
                "Current-attempt pinned-SHA CI evidence is missing or not successful. "
                "Rerun all jobs only if no release writes occurred; otherwise reconcile manually."
            )


def promotions(client: GitHub, action: str, default_branch: str):
    owner = client.repository.split("/")[0]
    query = urlencode({
        "state": "open", "head": f"{owner}:release/{action}", "base": default_branch,
    })
    return client.pages(f"repos/{client.repository}/pulls?{query}")


def fetch(root: Path, commit: str) -> None:
    if not SHA.fullmatch(commit):
        raise ValueError("Expected an immutable commit SHA.")
    git(root, "fetch", "--quiet", "--no-tags", "origin", commit)


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    try:
        git(root, "merge-base", "--is-ancestor", ancestor, descendant)
        return True
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            return False
        raise


def require_unstarted(root: Path, cycle: Cycle) -> dict[str, str]:
    if remote_head(root, cycle.branch) != cycle.merge_sha:
        raise ValueError("Release branch already changed; reconcile the interrupted release manually.")
    tags = git(root, "ls-remote", "--tags", "origin", f"refs/tags/{cycle.action}/v*")
    objects = dict(line.split()[::-1] for line in tags.splitlines())
    prefix = f"refs/tags/{cycle.action}/v"
    versions = {
        name.removeprefix("refs/tags/"): objects.get(name + "^{}", target)
        for name, target in objects.items() if name.startswith(prefix)
        and monorepo.VERSION.fullmatch(name[len(prefix):])
    }
    if cycle.merge_sha in versions.values():
        raise ValueError("This merge already has a release tag; reconcile it manually.")
    return versions


def verify_previous_releases(root: Path, client: GitHub, cycle: Cycle, tags: dict[str, str]) -> None:
    notes: dict[str, tuple[str, str]] = {}
    cleanups: dict[str, tuple[str, str]] = {}
    messages = git(
        root, "log", "--full-history", "--format=%H%x00%B%x00", cycle.merge_sha, "--",
        f"{cycle.action}/RELEASE-NOTES.md", ".semrelease/this_release",
    ).split("\0")
    for commit, message in zip(messages[::2], messages[1::2]):
        for label, subject, records in (
            ("Action-Release", "docs(action): record release notes", notes),
            ("Action-Release-Cleanup", "chore(action): clear released changelog entries", cleanups),
        ):
            # Squash/merge summaries can copy earlier bookkeeping trailers.
            if message.partition("\n")[0] != subject:
                continue
            matches = list(re.finditer(
                rf"^{label}: (?P<action>{monorepo.ACTION_NAME.pattern})@"
                rf"(?P<merge>{SHA.pattern})\nRelease: "
                rf"(?P<tag>(?P=action)/v{monorepo.VERSION.pattern})$",
                message, re.MULTILINE,
            ))
            if len(matches) != 1:
                raise ValueError("Ambiguous release bookkeeping; reconcile previous releases manually.")
            match = matches[0]
            if match["action"] != cycle.action:
                continue
            if match["merge"] in records:
                raise ValueError("Ambiguous release bookkeeping; reconcile previous releases manually.")
            records[match["merge"]] = (commit.strip(), match["tag"])
    if tags:
        git(root, "fetch", "--quiet", "--tags", "--force", "origin")
        published = git(root, "log", "--no-walk", "--format=%B%x00", *sorted(set(tags.values())))
        if re.search(
            rf"^Action-Release: {re.escape(cycle.action)}@{cycle.merge_sha}$", published, re.MULTILINE,
        ):
            raise ValueError("This merge already has a tagged release; reconcile it manually.")
    for merge, (_, tag) in notes.items():
        if merge not in cleanups or cleanups[merge][1] != tag:
            raise ValueError(f"Previous release {tag} still needs matching cleanup; reconcile it manually.")
    cleaned_tags = {tag for _, tag in cleanups.values()}
    unmatched = {
        tag: commit for tag, commit in tags.items()
        if tag not in cleaned_tags and is_ancestor(root, commit, cycle.merge_sha)
    }
    if unmatched:
        # PR history remains available even after a no-op notes release reaches main.
        query = urlencode({"state": "closed", "base": cycle.branch})
        prs = client.pages(f"repos/{client.repository}/pulls?{query}")
        for tag, commit in unmatched.items():
            previous = replace(cycle, merge_sha=commit)
            if any(matching_merge(pr, previous) for pr in prs):
                raise ValueError(f"Previous release {tag} still needs matching cleanup; reconcile it manually.")
    for merge, (cleanup_sha, tag) in cleanups.items():
        release_sha = notes[merge][0] if merge in notes else merge
        if not is_ancestor(root, release_sha, cleanup_sha):
            raise ValueError(
                f"Previous release {tag} has unrelated cleanup; reconcile it manually."
            )
        require_published(root, client, tag, release_sha)


def context(root: Path, client: GitHub, _path: Path) -> None:
    default = os.environ["DEFAULT_BRANCH"]
    current_run = positive_integer(os.environ["GITHUB_RUN_ID"])
    event = os.environ["GITHUB_EVENT_NAME"]
    if event != "push":
        raise ValueError("Only release-branch pushes are supported; reconcile interrupted releases manually.")
    payload = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
    branch = os.environ["CAPTURED_BRANCH"]
    if any(payload.get(name) is True for name in ("created", "deleted", "forced")):
        print("ℹ️ Not a release cycle: branch creation, deletion, or forced push.")
        emit(eligible=False)
        return
    action = branch.removeprefix("release/")
    if branch != f"release/{action}" or not monorepo.ACTION_NAME.fullmatch(action):
        print("ℹ️ Not a release cycle: expected release/<action>.")
        emit(eligible=False)
        return
    merge_sha = os.environ["CAPTURED_SHA"]
    if not SHA.fullmatch(merge_sha):
        raise ValueError("Context capture did not return an immutable merge SHA.")
    candidates = client.pages(f"repos/{client.repository}/commits/{merge_sha}/pulls")
    matching = [
        pr for pr in candidates
        if matching_merge(pr, Cycle(client.repository, action, branch, merge_sha, current_run, 1, default))
    ]
    if not matching:
        print("ℹ️ Not a release cycle: this push is not a verified PR merge.")
        emit(eligible=False)
        return
    if len(matching) != 1:
        raise ValueError("More than one merged PR matches this release push.")
    cycle = Cycle(
        client.repository, action, branch, merge_sha, current_run, matching[0]["number"], default,
    )
    cycle.validate()
    verify_source(client, cycle)
    tags = require_unstarted(root, cycle)
    fetch(root, cycle.merge_sha)
    verify_previous_releases(root, client, cycle, tags)
    emit(
        eligible=True, action=action, branch=cycle.branch, merge_sha=cycle.merge_sha,
        source_run_id=cycle.source_run_id, source_pr=cycle.source_pr,
    )


def source_section(root: Path, cycle: Cycle):
    paths = git(root, "ls-tree", "-r", "--name-only", cycle.merge_sha).splitlines()
    known = {
        path.split("/")[0] for path in paths
        if path.count("/") == 1 and path.endswith("/action.yml")
    }
    content = run(root, "git", "show", f"{cycle.merge_sha}:.semrelease/this_release")
    text = content.decode("utf-8-sig")
    sections, warnings = monorepo.parse_changelog(text, known)
    monorepo.print_changelog_warnings(warnings)
    selected = [section for section in sections if section.action == cycle.action]
    if len(selected) != 1:
        raise ValueError("Release requires exactly one nonempty section for its action.")
    section = selected[0]
    lines = text.splitlines(keepends=True)
    start = section.line - 1
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].lstrip().startswith("#!")),
        len(lines),
    )
    section_digest = digest("".join(lines[start:end]).encode("utf-8"))
    cleaned = "".join(lines[:start] + lines[end:]).encode("utf-8")
    if content.startswith(b"\xef\xbb\xbf"):
        cleaned = b"\xef\xbb\xbf" + cleaned
    return section, digest(content), section_digest, cleaned


def check_source(root: Path, plan: Plan) -> bytes:
    section, changelog_digest, section_digest, cleaned = source_section(root, plan.cycle)
    if (
        section.version != plan.requested_version or section.notes != plan.change_log
        or changelog_digest != plan.changelog_digest or section_digest != plan.section_digest
    ):
        raise ValueError("Pinned changelog does not match the recorded calculation.")
    return cleaned


def inputs(root: Path, client: GitHub, path: Path) -> None:
    cycle = Cycle.environment()
    if path.exists():
        raise ValueError("A job-local release plan already exists; reconcile it manually.")
    verify_source(client, cycle)
    require_unstarted(root, cycle)
    fetch(root, cycle.merge_sha)
    section, _, _, _ = source_section(root, cycle)
    # action-semrelease still reads legacy directives when version is empty.
    staging = root / ".semrelease" / "this_release"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_text(section.notes + "\n", encoding="utf-8", newline="\n")
    emit(
        version=section.version, tag_prefix=f"{cycle.action}/v",
        change_log_json=json.dumps(section.notes, ensure_ascii=True),
    )


def calculate(root: Path, client: GitHub, path: Path) -> None:
    cycle = Cycle.environment()
    if path.exists():
        raise ValueError("A job-local calculation already exists; reconcile it manually.")
    if os.environ["RELEASE_RESULT"] != "SUCCESS":
        raise ValueError("Release calculation must return SUCCESS; FAILED/SKIPPED cannot publish.")
    section, changelog_digest, section_digest, _ = source_section(root, cycle)
    plan = Plan(
        cycle=cycle, stage="calculated", version=os.environ["RELEASE_VERSION"],
        requested_version=section.version, change_log=section.notes,
        release_notes=os.environ["RELEASE_NOTES"],
        timestamp=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        changelog_digest=changelog_digest, section_digest=section_digest,
        ci_run_id=positive_integer(os.environ["CI_RUN_ID"]),
        ci_run_attempt=positive_integer(os.environ["CI_RUN_ATTEMPT"]),
        ci_target_sha=os.environ["CI_TARGET_SHA"],
    )
    plan.validate()
    verify_ci(client, plan)
    require_unstarted(root, cycle)
    require_new_release(root, client, plan)
    save(path, plan)
    monorepo.release_preview(root)


def history_headings(lines):
    headings = []
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = (token[0], len(token))
            elif token[0] == fence[0] and len(token) >= fence[1] and not line[marker.end():].strip():
                fence = None
            continue
        match = re.fullmatch(r"## v(" + monorepo.VERSION.pattern + r")", line.rstrip("\r\n"))
        if fence is None and match:
            headings.append((index, match[1]))
    return headings


def prepend_history(content: bytes, version: str, notes: str) -> bytes:
    monorepo.parse_version(version)
    text = content.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].strip():
        raise ValueError("RELEASE-NOTES.md must reserve its first line for a nonempty title.")
    if not notes.strip() or history_headings(notes.splitlines(keepends=True)):
        raise ValueError("Generated notes must be nonempty and must not contain history headings.")
    headings = history_headings(lines)
    positions = [index for index, value in headings if value == version]
    if len(positions) > 1:
        raise ValueError("RELEASE-NOTES.md has duplicate headings for the releasing version.")
    if positions:
        start = positions[0]
        end = next((index for index, _ in headings if index > start), len(lines))
        if "".join(lines[start + 1:end]).strip("\r\n") != notes.strip("\r\n"):
            raise ValueError("Existing version heading has conflicting release notes.")
        return content
    title = lines[0] if lines[0].endswith("\n") else lines[0] + "\n"
    older = "".join(lines[1:])
    if older and not older.startswith(("\r", "\n")):
        older = "\n" + older
    updated = (title + f"\n## v{version}\n\n" + notes.rstrip("\r\n") + "\n" + older).encode("utf-8")
    return b"\xef\xbb\xbf" + updated if content.startswith(b"\xef\xbb\xbf") else updated


def commit_file(root: Path, parent: str, filename: str, content: bytes, timestamp: str, message: str) -> str:
    with tempfile.TemporaryDirectory(prefix="release-index-") as temporary:
        env = {
            **os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index"),
            "GIT_AUTHOR_NAME": BOT, "GIT_AUTHOR_EMAIL": BOT_EMAIL,
            "GIT_COMMITTER_NAME": BOT, "GIT_COMMITTER_EMAIL": BOT_EMAIL,
            "GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp,
        }
        git(root, "read-tree", parent, env=env)
        blob = git(root, "hash-object", "-w", "--stdin", stdin=content, env=env)
        git(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{filename}", env=env)
        tree = git(root, "write-tree", env=env)
        if tree == git(root, "rev-parse", f"{parent}^{{tree}}"):
            return parent
        return git(
            root, "-c", "commit.gpgsign=false", "-c", "i18n.commitEncoding=UTF-8",
            "commit-tree", tree, "-p", parent,
            stdin=message.encode("utf-8"), env=env,
        )


def prepare(root: Path, _client: GitHub, path: Path) -> None:
    plan = load(path, Cycle.environment())
    if plan.stage != "calculated":
        raise ValueError("Preparation requires a new calculation; reconcile repeated preparation manually.")
    require_unstarted(root, plan.cycle)
    cleaned = check_source(root, plan)
    filename = f"{plan.cycle.action}/RELEASE-NOTES.md"
    entry = git(root, "ls-tree", plan.cycle.merge_sha, "--", filename).split()
    if not entry or entry[0] != "100644":
        raise ValueError("RELEASE-NOTES.md must be a regular, non-executable tracked file.")
    original = run(root, "git", "show", f"{plan.cycle.merge_sha}:{filename}")
    updated = prepend_history(original, plan.version, plan.release_notes)
    release_sha = commit_file(
        root, plan.cycle.merge_sha, filename, updated, plan.timestamp,
        f"docs(action): record release notes\n\n"
        f"Action-Release: {plan.cycle.action}@{plan.cycle.merge_sha}\nRelease: {plan.tag}\n",
    )
    cleanup_sha = commit_file(
        root, release_sha, ".semrelease/this_release", cleaned, plan.timestamp,
        f"chore(action): clear released changelog entries\n\n"
        f"Action-Release-Cleanup: {plan.cycle.action}@{plan.cycle.merge_sha}\nRelease: {plan.tag}\n",
    )
    prepared = replace(
        plan, stage="prepared", release_sha=release_sha,
        history_digest=digest(updated), cleanup_sha=cleanup_sha,
    )
    save(path, prepared)
    print(f"✅ Prepared {plan.tag}: merge {plan.cycle.merge_sha}, release {release_sha}.")


def remote_head(root: Path, branch: str) -> str:
    reference = f"refs/heads/{branch}"
    result = git(root, "ls-remote", "--refs", "origin", reference).split()
    if len(result) != 2 or result[1] != reference or not SHA.fullmatch(result[0]):
        raise ValueError("Release branch is missing or has an unexpected remote ref.")
    return result[0]


def release_record(client: GitHub, tag: str, notes: str | None = None):
    record = client.optional(f"repos/{client.repository}/releases/tags/{quote(tag, safe='')}")
    if record is not None and (
        record["tag_name"] != tag or record["draft"] or record["prerelease"]
        or (
            notes is not None and (record["body"] or "").replace("\r\n", "\n").strip("\n")
            != notes.replace("\r\n", "\n").strip("\n")
        )
    ):
        raise ValueError("An existing GitHub Release conflicts with the expected stable release.")
    return record


def require_new_release(root: Path, client: GitHub, plan: Plan) -> None:
    reference = f"refs/tags/{plan.tag}"
    if (
        release_record(client, plan.tag, plan.release_notes) is not None
        or git(root, "ls-remote", "--refs", "origin", reference)
    ):
        raise ValueError(f"{plan.tag} already exists; reconcile the release manually instead of retrying.")


def require_published(
    root: Path, client: GitHub, tag: str, release_sha: str, notes: str | None = None,
) -> None:
    if release_record(client, tag, notes) is None:
        raise ValueError(f"Publication of {tag} must complete before cleanup, promotion, or another cycle.")
    reference = f"refs/tags/{tag}"
    refs = git(root, "ls-remote", "--tags", "origin", reference, reference + "^{}")
    objects = dict(line.split()[::-1] for line in refs.splitlines())
    if objects.get(reference + "^{}", objects.get(reference)) != release_sha:
        raise ValueError("The published immutable tag does not match release N.")


def push_branch(root: Path, branch: str, expected: str, target: str) -> None:
    git(
        root, "push", "--quiet", f"--force-with-lease=refs/heads/{branch}:{expected}",
        "origin", f"{target}:refs/heads/{branch}",
    )


def push_notes(root: Path, client: GitHub, path: Path) -> None:
    plan = load(path, Cycle.environment())
    if plan.stage != "prepared":
        raise ValueError("Release notes must be prepared before pushing.")
    require_unstarted(root, plan.cycle)
    require_new_release(root, client, plan)
    if plan.release_sha != plan.cycle.merge_sha:
        push_branch(root, plan.cycle.branch, plan.cycle.merge_sha, plan.release_sha)


def freeze(root: Path, client: GitHub, path: Path) -> None:
    plan = load(path, Cycle.environment())
    if plan.stage != "prepared":
        raise ValueError("Cannot freeze before release-notes preparation.")
    verify_ci(client, plan)
    fetch(root, plan.release_sha)
    content = run(root, "git", "show", f"{plan.release_sha}:{plan.cycle.action}/RELEASE-NOTES.md")
    if digest(content) != plan.history_digest:
        raise ValueError("Committed release notes do not match the prepared plan.")
    if remote_head(root, plan.cycle.branch) != plan.release_sha:
        raise ValueError("Release branch advanced before the plan could be frozen.")
    save(path, replace(plan, stage="frozen"))


def frozen(path: Path) -> Plan:
    plan = load(path, Cycle.environment())
    if plan.stage != "frozen":
        raise ValueError("Publication requires a frozen release plan.")
    return plan


def publish(root: Path, client: GitHub, path: Path) -> None:
    plan = frozen(path)
    if remote_head(root, plan.cycle.branch) != plan.release_sha:
        raise ValueError("Release branch advanced before publication; no tags were pushed.")
    require_new_release(root, client, plan)
    git(root, "fetch", "--quiet", "--tags", "--force", "origin")
    objects, commits = monorepo.tag_state(root, plan.cycle.action)
    if plan.tag in objects:
        raise ValueError("The full-version tag already exists; reconcile it manually.")
    targets = monorepo.release_targets(plan.cycle.action, plan.version, plan.release_sha, commits)
    monorepo.push_tags(root, targets, objects, plan.tag)
    existing = client.api(f"repos/{plan.cycle.repository}/releases", "POST", {
        "tag_name": plan.tag, "target_commitish": plan.release_sha, "name": plan.tag,
        "body": plan.release_notes, "draft": False, "prerelease": False,
        "make_latest": "false" if plan.cycle.action == "dummy" else "true",
    })
    print(f"✅ Release: {existing['html_url']}")


def cleanup(root: Path, client: GitHub, path: Path) -> None:
    plan = frozen(path)
    require_published(root, client, plan.tag, plan.release_sha, plan.release_notes)
    head = remote_head(root, plan.cycle.branch)
    if head != plan.release_sha:
        raise ValueError("Release branch changed; reconcile cleanup manually.")
    push_branch(root, plan.cycle.branch, head, plan.cleanup_sha)


def promote(root: Path, client: GitHub, path: Path) -> None:
    plan = frozen(path)
    require_published(root, client, plan.tag, plan.release_sha, plan.release_notes)
    next_steps = (
        "If prompted, select 'Approve workflows to run' on the promotion PR to start CI. "
        "Merge manually after the required checks and reviews pass."
    )
    if remote_head(root, plan.cycle.branch) != plan.cleanup_sha:
        raise ValueError("Promotion requires the recorded cleanup commit.")
    prs = promotions(client, plan.cycle.action, plan.cycle.default_branch)
    if len(prs) > 1:
        raise ValueError("Multiple open promotion PRs match this action; reconcile them manually.")
    pr = prs[0] if prs else None
    if pr is not None and pr["head"]["sha"] != plan.cleanup_sha:
        raise ValueError("The open promotion PR does not point to the recorded cleanup commit.")
    title = f"Promote {plan.cycle.branch} to {plan.cycle.default_branch}"
    body = (pr["body"] or "") if pr is not None else (
        f"Promotes all unmerged changes from `{plan.cycle.branch}` "
        f"to `{plan.cycle.default_branch}`, including the releases below."
    )
    if plan.cycle.marker not in body:
        body = body + ("\n\n" if body else "") + (
            f"{plan.cycle.marker}\n\n## Release `{plan.tag}`\n\n"
            f"- Source PR: #{plan.cycle.source_pr}\n"
            f"- CI-tested merge: `{plan.cycle.merge_sha}`\n"
            f"- Release commit: `{plan.release_sha}`\n"
            f"- Changelog cleanup: `{plan.cleanup_sha}`\n\n"
            f"{next_steps}\n"
        )
    if pr is None:
        pr = client.api(f"repos/{plan.cycle.repository}/pulls", "POST", {
            "title": title, "head": plan.cycle.branch, "base": plan.cycle.default_branch, "body": body,
        })
    elif pr["title"] != title or (pr["body"] or "") != body:
        pr = client.api(f"repos/{plan.cycle.repository}/pulls/{pr['number']}", "PATCH", {
            "title": title, "body": body,
        })
    if (
        pr["head"]["sha"] != plan.cleanup_sha or pr["state"] != "open"
        or pr["merged_at"] is not None
    ):
        raise ValueError("Promotion PR changed during the handoff; reconcile it manually.")
    print(f"ℹ️ Promotion PR: {pr['html_url']}")
    print(f"ℹ️ {next_steps}")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                f"## ✅ Release `{plan.tag}`\n\n"
                f"CI-tested SHA: `{plan.cycle.merge_sha}`  \n"
                f"Release SHA: `{plan.release_sha}`\n\n"
                f"Promotion: {pr['html_url']}\n\n"
                f"{next_steps} No automatic merge is requested.\n"
            )


def main() -> None:
    monorepo.configure_output()
    commands = {
        "context": context, "inputs": inputs, "calculate": calculate,
        "prepare": prepare, "push-notes": push_notes, "freeze": freeze, "publish": publish,
        "cleanup": cleanup, "promote": promote,
    }
    parser = argparse.ArgumentParser(description="Run a pinned per-action release; reconcile failures manually.")
    parser.add_argument("command", choices=commands)
    args = parser.parse_args()
    try:
        commands[args.command](
            monorepo.ROOT, GitHub(monorepo.ROOT),
            Path(os.environ.get("RELEASE_STATE_FILE", "release-state.json")),
        )
    except (ValueError, OSError, KeyError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            detail = error.stderr or str(error)
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", errors="replace")
        else:
            detail = str(error)
        monorepo.print_workflow_data(f"❌ Release workflow failed: {detail}")
        parser.exit(1)


if __name__ == "__main__":
    main()

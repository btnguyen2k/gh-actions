# gh-actions

A collection of reusable GitHub actions for common development tasks.

Each action lives in its own directory at the repository root and is versioned
independently using **action-prefixed tags**. There are no published actions yet.

## Actions

| Action | Purpose | Status |
| --- | --- | --- |
| [workflow-context](workflow-context/README.md) | Capture event, target SHA/branch, PR details, and workflow-run metadata as outputs. | Unreleased |

## Repository layout

The layout for each new action is:

```text
<action-name>/
  action.yml
  README.md
  tests/
    run.sh
  ...implementation and action-specific dependencies...
```

Use lowercase kebab-case directory names. The root README is the action catalog;
each action's README documents its inputs, outputs, examples, required permissions,
prerequisites, supported runners, and compatibility guarantees.

Repository-wide automation lives under `.github/`. Contributor and coding-agent
instructions are in [AGENTS.md](AGENTS.md).

## Using an action

Replace `<action-name>` with an action's directory name:

| Reference | Example | Behavior |
| --- | --- | --- |
| Full version | `btnguyen2k/gh-actions/<action-name>@<action-name>/v1.2.3` | Fixed release; never moved or reused |
| Major alias | `btnguyen2k/gh-actions/<action-name>@<action-name>/v1` | Latest stable release in major version 1 |
| Minor alias | `btnguyen2k/gh-actions/<action-name>@<action-name>/v1.2` | Latest stable release in minor version 1.2 |
| Commit SHA | `btnguyen2k/gh-actions/<action-name>@<full-commit-sha>` | Exact repository snapshot; strongest pinning |

For production workflows, prefer the full commit SHA of the selected action's
release, with a comment identifying its version. A tag always points to a snapshot
of the **whole repository**: another action's release tag is not a release of the
action you intend to use. There are no repository-wide `v1` or `v1.2.3` tags.

Subdirectory actions can be consumed directly without a Marketplace listing.
GitHub Marketplace requires root-level action metadata, so this layout does not
provide a separate Marketplace listing for each action. See
[GitHub's publishing requirements](https://docs.github.com/en/actions/how-tos/create-and-publish-actions/publish-in-github-marketplace).

## Development and CI

Repository tooling uses Python 3.12 or newer. To set up a local environment in
PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --requirement .github\requirements.txt
python .github\scripts\monorepo.py validate
python -m unittest discover --start-directory .github\scripts --pattern "test_*.py"
python .github\scripts\monorepo.py test
```

The last command additionally requires Bash and any toolchains documented by the
actions being tested.

CI runs on branch pushes and pull requests. It validates workflow, Dependabot, and
action metadata, tests the repository tooling, and runs every action's
`tests/run.sh` with that action's directory as the working directory. With no
actions present, it explicitly reports that action tests are not yet applicable.
Changes to shared tooling therefore exercise all actions, not just changed paths.

Each action owns its implementation language, dependency manifests, lockfiles,
and tests. Its test entrypoint must install its development dependencies, fail on
test errors, and check that any committed runtime bundles are up to date. CI also
rejects changes to tracked files left by tests. CI currently runs on Ubuntu,
Windows, and macOS, with Python 3.12 and Node.js 24 available for repository and
action tests.
Pull-request tests must not require secrets or elevated repository permissions.

## Releasing an action

After the workflows are merged into `main`, run the **Release action** workflow
from the repository's default branch. Supply:

| Input | Meaning |
| --- | --- |
| `action` | Existing root-level action directory name |
| `version` | Stable SemVer without `v`, for example `1.2.3` |
| `notes` | Release notes for this action, including breaking changes and migration guidance |

The workflow validates and tests the selected commit before publishing. It then:

1. Creates `<action>/vX.Y.Z` without replacing an existing version tag.
2. Advances `<action>/vX` and `<action>/vX.Y` to their highest stable versions.
3. Publishes a GitHub release titled `<action>/vX.Y.Z` with the supplied notes.

Tags are pushed atomically, with leases to reject conflicting concurrent alias changes.
Releasing `1.2.5` after `1.3.0` updates `v1.2`, but does not roll `v1` back from
`1.3.0`. Other actions' tags are untouched. Prereleases and build metadata are not
supported by the initial release workflow.

Releases are not marked as the repository-wide "latest" release, because versions
belong to individual actions. If tag publication succeeds but GitHub release
creation fails, re-run the failed workflow on the same commit and version. It can
complete a missing release without moving the full-version tag; a different commit
for an existing version, or an already-created GitHub release, is rejected.

Use SemVer independently for each action. Changes to input/output contracts,
behavior, defaults, required permissions, or supported runners can be breaking
changes. Changes to shared runtime code must be assessed for every consuming
action; do not bump unrelated actions just because the repository changed.

Before the first public release, configure repository rulesets to prevent
full-version tag updates/deletions while allowing the release workflow to maintain
major/minor aliases. Consider enabling immutable GitHub releases for additional
protection; never attach GitHub releases to moving aliases. These repository
settings are not enabled by the files in this repository.

## License

[MIT](LICENSE.md).

# Repository instructions

## Project model

- This repository is a collection of reusable GitHub actions, not one root action.
  Each public action occupies a lowercase kebab-case directory directly under the
  repository root.
- Each action must contain `action.yml`, its own `README.md`, `RELEASE-NOTES.md`,
  and `tests/run.sh`. Keep implementation files, dependency manifests, lockfiles,
  and runtime assets within that action's directory. Choose the runtime per action;
  do not impose a repository-wide application framework or package version.
- Keep the root README concise and focused on business value, the action catalog,
  and consumer-facing usage. Keep internal development, CI, and release procedures
  in these instructions, not the README.
- Do not add placeholder actions or describe examples as published functionality.
- `dummy` is an internal release-flow test action, not a public catalog entry.
  Keep it out of the root README and do not present it as production functionality.

## Implementation and compatibility

- Treat inputs, outputs, defaults, failure modes, permissions, and supported
  runners as public API. Document them in the action README and assess breaking
  changes using that action's SemVer.
- Validate required and malformed inputs explicitly; `required: true` in action
  metadata does not itself enforce input validation.
- An action must work when invoked from another repository. Resolve bundled files
  relative to the action directory, not the caller's working directory. In
  composite actions, quote `$GITHUB_ACTION_PATH` when locating scripts.
- Ship everything needed at runtime in the tagged commit. For JavaScript actions,
  include the generated entrypoints and bundled dependencies; do not expect the
  caller to install development dependencies.
- Avoid runtime dependencies on sibling action directories. If shared source is
  introduced, bundle it into each consumer where appropriate and test/release
  every affected action independently.
- Never interpolate untrusted contexts or inputs directly into shell code. Pass
  values through environment variables or arguments and quote them. Do not log
  secrets; use least-privilege tokens and explicit, actionable failures.
- Write outputs through GitHub's supported output mechanism, such as
  `$GITHUB_OUTPUT`, and preserve the documented output format.

## Validation

- Commands below use PowerShell path separators. Use `/` separators in POSIX shells.
- Repository tooling requires Python 3.12+. Install its dependencies from
  `.github/requirements.txt`, preferably in a virtual environment.
- Validate repository metadata with
  `python .github\scripts\monorepo.py validate`. The log lists each schema group
  and the files being checked.
- Test repository tooling with
  `python -m unittest discover --start-directory .github\scripts --pattern "test_*.py"`.
- Run action tests with `python .github\scripts\monorepo.py test`. Each
  `tests/run.sh` runs from its own action directory through Bash, owns its
  dependency setup, and must return a nonzero exit code on failure. CI currently
  covers Ubuntu, Windows, and macOS with the latest Node.js LTS. Keep shell
  scripts LF-only, as required by `.gitattributes`, including when editing on
  Windows.
- Windows tests must run natively, not in WSL. Resolve Git for Windows Bash to an
  absolute executable path; a bare `bash` command can launch the WSL shim even
  inside a Git Bash workflow step.
- Add tests for invalid inputs, expected outputs, error paths, and compatibility.
  Verify committed generated files are reproducible. Add runner-specific CI
  coverage before claiming Windows or macOS support.
- Keep pull-request tests safe for forks: no secrets, write tokens, or execution
  of untrusted code through `pull_request_target`.
- CI currently validates and tests all actions. Preserve this coverage for shared
  changes if introducing path-based optimization later.
- CI first runs the local `workflow-context` action and passes its `target-sha`
  output to downstream checkout steps. Reusable CI accepts `target-sha` and
  `target-branch`; use the explicit SHA rather than a moving branch.
- CI's `ParseChangelog` job reports pending action releases and exports their
  inputs as a matrix. First validate the whole changelog with
  `python .github\scripts\monorepo.py validate-changelog`, regardless of branch
  scope. Then, on `<prefix>/<action>` branches with any non-empty prefix, inspect
  only that action; branches without such a prefix inspect all actions. Pull
  requests use their target/base branch for this scope.
- CI's `PreviewRelease` job invokes pinned `btnguyen2k/action-semrelease` in
  read-only dry-run mode for each selected section. Capture `result`,
  `releaseVersion`, and `releaseNotes` in each matrix job's logs and summary, not
  shared matrix job outputs that could overwrite one another. Validate the
  captured target SHA against the full workflow ref, including PR merge refs.
  The release workflow calls CI with `skip-release-preview: true`, then calculates
  only after all native validation jobs and changelog validation pass.
- CI exposes stable matrix marker checks `ValidatePass` and `PreviewReleasePass`.
  Both run even after dependency failures. `ValidatePass` requires the entire
  validation matrix to succeed. `PreviewReleasePass` requires successful
  changelog planning and successful previews, or an intentional preview skip
  (no selected releases, explicit preview disable, or a deleted branch).
  Failures, cancellations, and unexpected skips fail the corresponding marker.
- Standalone push CI excludes `release/**`; the release workflow owns those
  pushes. Ordinary branch pushes, pull requests (including those into release
  branches), and manual CI keep their existing coverage and previews.
- Run the same report locally with `python .github\scripts\monorepo.py changelog`;
  set `CHANGELOG_BRANCH` to select a branch scope. If unset, all actions are shown.
  This command prepares release inputs; version calculation and final release
  notes are delegated to action-semrelease in CI, not implemented in Python.

## Commit messages

- For action changes, use `type(action): <description>` for every release,
  including a brand-new action's first release. Choose the appropriate commit
  type and keep the description short, concise, and focused on the actual change.
- The changelog section header supplies the action name and optional release version.
  Do not repeat them as add-action announcements or release-version prefixes;
  use the literal scope `action`, not an action-specific scope.
- If an action version includes multiple updates, generate a separate
  commit message for each update rather than combining them into one message.

## Releases and workflows

- Stage release notes in `.semrelease/this_release` under
  `#!<action-name>/v<version-number>` for a fixed version or `#!<action-name>` for
  automatic versioning, allowing zero or more spaces after `#!`. Pass
  `<action-name>/v` as `tag-prefix`, the fixed version or an empty string as
  `version`, and the section's multiline messages as `change-log`. Do not use
  repository-wide `#!VERSION` directives. Blank lines and ordinary `#` comment
  lines do not count as changelog entries.
- Changelog reporting warns and excludes malformed headers, invalid action names
  or stable versions, unknown actions, and empty sections. An action-scoped branch
  with no valid matching section also warns. These warnings do not fail validation.
- Each known action may have at most one valid section header, including when its
  body is empty. Duplicate headers fail changelog validation with the action name
  and source line numbers, whether their versions match, differ, or are omitted.
  Missing or unreadable changelog files also fail. An empty plan skips release
  previews; action-semrelease failures still fail CI, and `SKIPPED` remains distinct
  from `SUCCESS`.
- Actions are versioned independently. Full release tags are
  `<action-name>/vX.Y.Z`; they are immutable and must never be moved or reused.
  Moving `<action-name>/vX` and `<action-name>/vX.Y` aliases track the highest stable
  release in their respective lines, never the most recently published by date.
- Do not create unprefixed repository-wide version tags or bump unaffected
  actions. A tag selects a whole-repository snapshot but releases only the named
  action.
- `.github/workflows/release.yml` publishes stable releases after a verified PR
  merge into `release/<action>`. Branch creation/deletion, forced pushes, ordinary
  direct pushes, and generated notes/cleanup pushes do not start release cycles.
  Verify the merged PR's base branch and merge SHA; do not infer a merge from its
  commit message.
- Capture context with `workflow-context`, pin the original merge SHA R, and run
  native CI on R before calculating version/notes. Require `SUCCESS`, a bare
  stable version matching any fixed header, nonempty generated notes, and actual
  successful CI evidence for R. No prerelease support is implied.
- Prepend `## v<version>` and generated notes to `<action>/RELEASE-NOTES.md`,
  preserving the title on line 1 and all older entries. An identical existing
  entry is a no-op; conflicting notes or duplicate version headings fail.
  The controlled writer changes only this text file to create N. Do not rerun
  CI or add a separate Git diff-check stage for N. Publish/tag N, not cleanup C.
- Freeze the validated plan before publication. Immutable calculation,
  preparation, and frozen-plan artifacts are named
  `release-state-<action>-<merge-sha>-<attempt>-<stage>`. Verify their digest,
  workflow/ref provenance, original PR merge, and CI evidence before recovery.
  Never recalculate a recorded automatic version or substitute the dispatch SHA
  or current branch head. Generated commits use recorded timestamps and isolated
  Git indexes so retries reconstruct the same N and C without staging other work.
- After publication succeeds, remove only that action's staged section and push
  cleanup C onto the same release branch. Preserve other sections and the committed
  release-notes history. Push notes and cleanup with expected-head leases; stop
  on unexpected branch movement rather than overwriting it.
- Create/reuse the promotion PR into the default branch for **manual merge** after
  required checks and reviews. Never request auto-merge or push directly to the
  default branch. Only one active cycle per action is allowed until promotion is
  merged; release branches should start their next cycle from the latest default
  branch.
- Release helpers and the context action run from the captured default-branch
  automation checkout, not candidate code with a write token. Candidate code runs
  only in read-only CI. The release calculator is SHA-pinned and read-only.
  Keep release notes scoped to their action and explain migrations for breaking
  changes.
- Never publish, push tags, or create GitHub releases without an explicit user
  request. Adding or updating release automation is not permission to run it.
- If a release is interrupted, rerun its failed jobs. Alternatively, dispatch
  `release.yml` from the default branch with `action` and `run-id` identifying the
  original push run, or a recovery run with saved state. There are no manual
  version/notes overrides. Resume matching tags/releases, notes commits, cleanup,
  and promotion rather than duplicating them. Missing/expired state or conflicting
  external state requires manual reconciliation, never a guessed new version.
- Configure repository rulesets to prevent full-version tag updates/deletions
  while allowing major/minor alias updates. Consider immutable GitHub releases;
  never attach GitHub releases to moving aliases or mark an action release as
  the repository-wide "latest" release.
- Keep workflow permissions read-only by default. The release workflow's
  `GITHUB_TOKEN` has contents, actions, and pull-request read permissions; supply
  the repository-scoped App token only to notes/publishing/cleanup/promotion
  steps. Pin external actions to full commit SHAs, with version comments. Supply
  dispatch inputs to scripts via environment variables, not shell interpolation.
- Update the relevant action README and root catalog when adding an action or
  changing its API. See the root README for usage and Marketplace limitations.

### Release automation setup

- Merge this automation into the default branch before creating/updating release
  branches; privileged jobs load their helpers from that trusted checkout.
- Install a GitHub App on this repository with Contents: read/write and Pull
  requests: read/write. Set repository variable `RELEASE_APP_CLIENT_ID` and secret
  `RELEASE_APP_PRIVATE_KEY`. The workflow mints a short-lived token restricted to
  this repository and these permissions; do not fall back to a personal token or
  `GITHUB_TOKEN` for generated promotion PRs.
- Require reviewed PRs and prevent force-pushes/deletion on release branches.
  Allow the App to bypass release-branch PR/check requirements only for its
  controlled notes/cleanup writes. Do not give it a bypass of default-branch
  protections. Protect immutable full-version tags while allowing compatibility
  alias updates.
- Keep required native CI checks/reviews on the default branch. Promotion is
  manual; enabling repository auto-merge is not required.
- Keep release-state artifacts for the full release/recovery cycle using the
  repository's artifact-retention settings. Do not delete them mid-cycle.
- Automatic version baselines follow action-semrelease's prefix-filtered API
  ordering, not necessarily highest numeric SemVer. Use fixed-version headers
  for maintenance/backport releases; the publisher still keeps aliases on the
  highest stable numeric versions.

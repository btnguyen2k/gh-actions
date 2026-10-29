# Repository instructions

## Project model

- This repository is a collection of reusable GitHub actions, not one root action.
  Each public action occupies a lowercase kebab-case directory directly under the
  repository root.
- Each action must contain `action.yml`, its own `README.md`, `RELEASE-NOTES.md`,
  and `tests/run.sh`. Keep implementation files, dependency manifests, lockfiles,
  and runtime assets within that action's directory. Choose the runtime per action;
  do not impose a repository-wide application framework or package version.
- Every action or workflow README must include a dedicated `## License` section
  that explicitly names the MIT License and uses the absolute license URL
  `https://github.com/btnguyen2k/gh-actions/blob/main/LICENSE.md`.
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
- Repository workflows invoke in-repository actions via `uses: ./<action>`,
  not published self-references. Check out the intended source commit first.
  Privileged `workflow_run` jobs use their trusted workflow SHA, never the
  upstream PR head.
- Include composite action directories in the `github-actions` update entry in
  `.github/dependabot.yml` when they reference external actions. The root `/`
  scan does not cover nested action metadata.
- Never interpolate untrusted contexts or inputs directly into shell code. Pass
  values through environment variables or arguments and quote them. Do not log
  secrets; use least-privilege tokens and explicit, actionable failures.
- Write outputs through GitHub's supported output mechanism, such as
  `$GITHUB_OUTPUT`, and preserve the documented output format.
- Keep reusable-action logs minimal and preferably plain text; avoid emojis
  whenever possible. Prefer documented, machine-readable outputs such as
  `status` or `result` for expected, non-fatal outcomes so caller workflows decide
  how to react and present messages.
- Fatal errors must fail the action explicitly, for example by throwing an
  exception or exiting nonzero. Do not merely log an error or turn a fatal
  failure into a successful status-only response.
- Prefix human-readable messages in workflows and repository automation scripts
  with ℹ️ for information, ✅ for success, ⚠️ for warnings, and ❌ for errors.
  Keep annotation commands, required check names, machine-readable outputs, and
  release-note content unchanged.
  Python CLIs configure UTF-8 output, and Python workflows set
  `PYTHONIOENCODING: "utf-8"`; decode captured log output as UTF-8 on Windows too.

## Validation

- Commands below use PowerShell path separators. Use `/` separators in POSIX shells.
- Repository tooling requires Python 3.12+. Install its dependencies from
  `.github/requirements.txt`, preferably in a virtual environment.
- Validate repository metadata with
  `python .github\scripts\monorepo.py validate`. The log lists each schema group
  and the files being checked.
- Test repository tooling with
  `python -m unittest discover --start-directory .github\scripts --pattern "test_*.py"`.
  Workflow script tests also require Node.js.
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
- Context logs distinguish the validation SHA from the PR source SHA and label
  PR source/head and target/base branches separately. Non-PR runs show the
  workflow branch or tag; report the release-scoping branch separately.
- PRs targeting `release/<action>` must originate from `<prefix>/<same-action>`,
  using any non-empty first path component as the prefix. Reject mismatched
  actions, unscoped sources such as `main`, and nested or malformed action suffixes.
  Other PR targets, including promotion into `main`, are unaffected.
  `ParseChangelog` runs `python .github\scripts\monorepo.py validate-pr-branches`
  with `PR_HEAD_REF` and `PR_BASE_REF` before changelog validation/planning.
  PR `edited` events rerun CI so retargeting cannot reuse the previous routing check.
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
- Standalone push CI excludes `release/**`, owned by the release workflow, and
  `dependabot/**`, covered by pull-request CI. Avoid duplicate push/PR runs with
  the same required check names on Dependabot heads: automerge attempts merging
  after successful PR CI without waiting or retrying. Other branch pushes,
  pull requests (including those into release branches), and manual/reusable CI
  keep their existing coverage and previews.
- Run the same report locally with `python .github\scripts\monorepo.py changelog`;
  set `CHANGELOG_BRANCH` to select a branch scope. If unset, all actions are shown.
  This command prepares release inputs; version calculation and final release
  notes are delegated to action-semrelease in CI, not implemented in Python.

## Pre-release action review

- Use `.github\agents\action-release-reviewer.agent.md` for a review-only check of
  repository conventions and the selected action's own documented contract.
- Select `/agent action-release-reviewer`, then request
  `Review <action-name> before release.` Include a candidate ref or comparison
  baseline when reviewing something other than the current checkout.
- The reviewer reports evidence-backed readiness, blockers, and incomplete
  checks. It does not fix findings, change release artifacts, or publish.

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
- New actions start with `# Version: 0.0.0` at the top of `action.yml` and a
  `0.0.0` version entry in the root README's Actions table. All action READMEs,
  including `dummy`, use a near-top `**Latest version:**` line linking `vX.Y.Z`
  to the action's GitHub Release, followed by ``(tag: `<action>/vX.Y.Z`).``.
  Keep this format for `0.0.0`, even if the tag or release does not exist.
  Use `0.0.0` when no published stable GitHub Release exists; it is a normal
  stable version, not an unreleased state. Keep internal `dummy` out of the root
  catalog. Never add an unsupported top-level `version` metadata key.
- Release preparation updates the selected action's version comment, README
  version/release link and applicable full-version tag examples, and its version
  link in the root README's Actions table to the calculated `<action>/vX.Y.Z`
  release. Update `dummy`'s own files too, but never add it to the root catalog.
  Require the version markers and public catalog entry to exist in the incoming
  source; new-action development must add them before release preparation.
- Do not create unprefixed repository-wide version tags or bump unaffected
  actions. A tag selects a whole-repository snapshot but releases only the named
  action.
- `.github/workflows/release.yml` publishes stable releases after a verified PR
  merge into `release/<action>`. Branch creation/deletion, forced pushes, ordinary
  direct pushes, and generated notes/cleanup pushes do not start release cycles.
  Verify the merged PR's base branch and merge SHA; do not infer a merge from its
  commit message. Notes/cleanup pushes made with `GITHUB_TOKEN` do not trigger
  additional push workflows.
- Release verification rechecks the same source/target action pairing
  from the merged PR's API metadata before CI and publication. A mismatch is an
  explicit error before CI/publication, not an ignored direct push.
- Capture context with `workflow-context`, pin the original merge SHA R, and run
  native CI on R before calculating version/notes. Require `SUCCESS`, a bare
  stable version matching any fixed header, nonempty generated notes, and actual
  successful CI evidence for R. No prerelease support is implied.
- Prepend `## v<version>` and generated notes to `<action>/RELEASE-NOTES.md`,
  preserving the title on line 1 and all older entries. An identical existing
  entry is a no-op; conflicting notes or duplicate version headings fail.
  The controlled writer creates N from R, changing only this file, the selected
  action's version comment in `action.yml`, its README version/tag references,
  and its root README catalog version (except for `dummy`). Never change runtime
  implementation files in N. Do not rerun CI or add a separate Git diff-check
  stage for N. Publish/tag N, not cleanup C.
- Freeze the validated job-local `release-state.json` plan before publication.
  Keep the version, notes, original merge SHA, current-run/attempt CI evidence,
  commit SHAs, and content digests in that local file only; do not upload/download
  release-state artifacts or restore plans from earlier runs. Generated commits
  use the plan's timestamp and isolated Git indexes without staging other work.
- After publication succeeds, remove only that action's staged section and push
  cleanup C onto the same release branch. Preserve other sections and the committed
  release-notes history. Push notes and cleanup with expected-head leases; stop
  on unexpected branch movement rather than overwriting it.
- Promotion into the default branch is independent of publication. Another
  `dev/<action>` (or other valid source) PR may merge into `release/<action>`
  while promotion is pending, provided earlier publication and cleanup completed.
  Verify prior notes/cleanup bookkeeping against the immutable full-version tag
  and published stable GitHub Release, not promotion PR bodies or main ancestry.
  Track generated version-metadata commits even when notes are unchanged. When
  notes and version metadata are all unchanged, use the tag's verified release-PR
  merge to detect missing cleanup even though there is no generated commit.
  Once matching cleanup is recorded, missing historical GitHub Releases or
  immutable tags are warnings, including for `dummy`. Conflicting records/tag
  targets and incomplete or unrelated cleanup remain fatal and require manual
  reconciliation. Current-cycle publication checks remain mandatory before
  cleanup or promotion.
- Keep one open promotion PR per action into the default branch. Create it when
  none is open; otherwise reuse it, update its title to
  `Promote release/<action> to <default-branch>`, and append the new release's
  information without replacing existing description text. The PR's source branch
  and cumulative diff advance with each release. Closed or merged PRs do not
  block a new one; ambiguous multiple open matches fail explicitly.
- Promotion remains a **manual merge** after the latest release finishes and
  required checks/reviews pass. Never request auto-merge or push directly to the
  default branch. Publications remain serialized per action. Base subsequent
  development on the latest release branch, including its release notes and
  cleanup, so previously released staging entries are not restored.
- Promotion PRs created/updated with `GITHUB_TOKEN` start their opened/synchronize/
  reopened CI runs in an approval-required state. A user with write access selects
  **Approve workflows to run**, waits for CI and reviews, then merges manually.
  Workflow approval only starts CI; it does not approve the code or merge the PR.
- Release helpers and the context action come from the incoming release commit R,
  not a default-branch automation checkout. CaptureContext checks out the event
  SHA; CI and Release reuse the captured merge SHA. Action tests run in read-only
  CI; only Release runs the incoming helpers with write permissions after CI
  succeeds. Review workflow/helper changes as privileged automation code. The
  release calculator is SHA-pinned and runs in dry-run mode. Keep release notes
  scoped to their action and explain migrations for breaking changes.
- Never publish, push tags, or create GitHub releases without an explicit user
  request. Adding or updating release automation is not permission to run it.
- Interrupted publication/cleanup requires **manual reconciliation**, not automatic resume.
  There is no recovery dispatch, artifact checkpoint, or historical CI reuse.
  Retry with **Re-run all jobs** only if no release writes occurred; every attempt
  needs its own successful pinned-SHA CI. Failed-jobs-only reruns cannot borrow
  native CI successes from an earlier attempt.
- Do not replay an interrupted cycle, recalculate another version after partial
  writes, or overwrite full-version tags. Manual cleanup must retain the generated
  `chore(action): clear released changelog entries` message and its
  `Action-Release-Cleanup: <action>@<original-merge-sha>` / `Release: <action>/vX.Y.Z`
  trailers, and descend from the published notes commit (or R when notes were a no-op).
  A failed or pending promotion does not invalidate completed publication/cleanup;
  a later fresh release may create/update that PR. Informational cycle markers in
  promotion descriptions are optional and never determine release eligibility.
- Configure repository rulesets to prevent full-version tag updates/deletions
  while allowing major/minor alias updates. Consider immutable GitHub releases;
  never attach GitHub releases to moving aliases. Publish internal `dummy`
  releases with `make_latest: "false"` and all other action releases with
  `make_latest: "true"`. The Latest designation is repository-wide, not per action.
- Keep workflow permissions read-only by default. Only `Release` and the
  Dependabot auto-merge job grant `GITHUB_TOKEN` contents/pull-request write.
  `Release` additionally retains actions read access for current-attempt CI
  verification and supplies the token as `GH_TOKEN` for notes, publishing,
  cleanup, and promotion; context capture and candidate CI stay read-only. Do not add GitHub
  App or personal-token dependencies. Pin external actions to full commit SHAs,
  with version comments. Supply dispatch inputs to scripts via environment
  variables, not shell interpolation.
- Keep `Automerge-Dependabot` as one post-CI job and one script. Run only after
  successful pull-request CI; the script skips missing PR associations before
  any API call. Merge only open, non-draft `dependabot[bot]` PRs into the
  repository's default branch. Fetch current PR
  metadata, compare its head with `workflow_run.head_sha`, and pass the same SHA
  to the merge API. Skip stale/ineligible PRs and surface API/merge failures.
  Never bypass branch rules or auto-merge release-promotion PRs.
- The reusable `automerge-dependabot` action independently enforces these event
  and merge guards, returning `status`, `reason`, and `pr-number` for caller-owned
  presentation. Keep the action itself checkout-free and self-contained.
- Update the relevant action README and root catalog when adding an action or
  changing its API. See the root README for usage and Marketplace limitations.

### Release automation setup

- Include the release workflow, helper scripts, and local `workflow-context`
  action in release branches. Automation does not need to reach the default
  branch before a release; protect release branches and review automation changes.
- Use the built-in `GITHUB_TOKEN`; no App credentials or personal token are
  required. Under **Settings > Actions > General > Workflow permissions**, enable
  **Allow GitHub Actions to create and approve pull requests** so the workflow can
  create promotion PRs. The workflow never approves or merges them automatically.
- Require checks/reviews for incoming release PRs and prevent force-pushes/deletion
  on release branches. Branch/tag rules must also permit the controlled notes/
  cleanup pushes and tag writes: required checks on every new commit without an
  applicable exception will block N/C, which intentionally receive no separate CI.
  Token write permissions do not bypass protections. Keep default-branch
  protections intact and protect immutable full-version tags while allowing
  compatibility alias updates.
- Keep required native CI checks/reviews on the default branch. Promotion is
  manual; enabling repository auto-merge is not required.
- Automatic version baselines follow action-semrelease's prefix-filtered API
  ordering, not necessarily highest numeric SemVer. Use fixed-version headers
  for maintenance/backport releases; the publisher still keeps aliases on the
  highest stable numeric versions.

# Repository instructions

## Project model

- This repository is a collection of reusable GitHub actions, not one root action.
  Each public action occupies a lowercase kebab-case directory directly under the
  repository root.
- Each action must contain `action.yml`, its own `README.md`, and `tests/run.sh`.
  Keep implementation files, dependency manifests, lockfiles, and runtime assets
  within that action's directory. Choose the runtime per action; do not impose a
  repository-wide application framework or package version.
- Keep the root README concise and focused on business value, the action catalog,
  and consumer-facing usage. Keep internal development, CI, and release procedures
  in these instructions, not the README.
- Do not add placeholder actions or describe examples as published functionality.

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
  `python .github\scripts\monorepo.py validate`.
- Test repository tooling with
  `python -m unittest discover --start-directory .github\scripts --pattern "test_*.py"`.
- Run action tests with `python .github\scripts\monorepo.py test`. Each
  `tests/run.sh` runs from its own action directory through Bash, owns its
  dependency setup, and must return a nonzero exit code on failure. CI currently
  covers Ubuntu, Windows, and macOS with Node.js 24. Keep shell scripts LF-only,
  as required by `.gitattributes`, including when editing on Windows.
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
  output to downstream checkout steps.

## Commit messages

- For a brand-new action, use `feat(actions): add action <action-name>`.
- For a new version of an existing action, use
  `type(actions/<action-name>): release <version> - <description>`.
  Choose the appropriate commit type and keep the description short and concise.
- If an action version includes multiple updates, generate a separate
  release-format commit message for each update rather than combining them into
  one message.

## Releases and workflows

- Actions are versioned independently. Full release tags are
  `<action-name>/vX.Y.Z`; they are immutable and must never be moved or reused.
  Moving `<action-name>/vX` and `<action-name>/vX.Y` aliases track the highest stable
  release in their respective lines, never the most recently published by date.
- Do not create unprefixed repository-wide version tags or bump unaffected
  actions. A tag selects a whole-repository snapshot but releases only the named
  action.
- Use `.github/workflows/release.yml` from the default branch for stable releases.
  Its version input omits the `v`. Keep release notes scoped to that action and
  explain migrations for breaking changes. Prereleases require an explicit future
  extension, not a workaround.
- Never publish, push tags, or create GitHub releases without an explicit user
  request. Adding or updating release automation is not permission to run it.
- If tag pushes succeed but release creation fails, rerun the failed workflow on
  the same commit and version. Do not move immutable version tags to recover.
- Configure repository rulesets to prevent full-version tag updates/deletions
  while allowing major/minor alias updates. Consider immutable GitHub releases;
  never attach GitHub releases to moving aliases or mark an action release as
  the repository-wide "latest" release.
- Keep workflow permissions read-only by default; scope `contents: write` to the
  publishing job. Pin external actions to full commit SHAs, with version comments.
  Supply dispatch inputs to scripts via environment variables, not shell
  interpolation.
- Update the relevant action README and root catalog when adding an action or
  changing its API. See the root README for usage and Marketplace limitations.

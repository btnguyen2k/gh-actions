# workflow-context

Capture the workflow trigger, target SHA/branch, pull request details, and run
metadata as action outputs. No checkout is required. The action uses JavaScript
on Node.js 24 with no external dependencies or build step.

## Usage

Minimal job configuration; no GitHub API permission is needed:

```yaml
runs-on: ubuntu-latest
steps:
  - name: Capture workflow context
    id: context
    uses: btnguyen2k/gh-actions/workflow-context@<released-commit-sha>
  - name: Use the captured target
    shell: bash
    env:
      TARGET_SHA: ${{ steps.context.outputs.target-sha }}
      TARGET_BRANCH: ${{ steps.context.outputs.action-target-branch }}
    run: |
      printf 'Target SHA: %s\nTarget branch: %s\n' "$TARGET_SHA" "$TARGET_BRANCH"
```

To fetch current PR details and optionally target the PR's head commit:

```yaml
runs-on: ubuntu-latest
permissions:
  pull-requests: read
steps:
  - id: context
    uses: btnguyen2k/gh-actions/workflow-context@<released-commit-sha>
    with:
      github-token: ${{ github.token }}
      target-sha: ${{ github.event.pull_request.head.sha || github.sha }}
```

The action does not check out, build, or execute code from the captured ref.
Treat PR titles, labels, and branch names as untrusted data: pass them through
environment variables or arguments, not directly into shell source.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `target-sha` | `GITHUB_SHA` when empty | Nonempty input is passed through unchanged. It is not resolved, verified, or used to infer a branch. |
| `github-token` | Empty | Fetch current PR details when the event identifies a PR. No API request is made without a token, or for non-PR events. |

`github-token` is not automatically populated from `github.token`. Supply it
explicitly and grant access to the workflow repository's pull requests.
`pull-requests: read` is sufficient for this action; other steps may need additional
permissions. API lookups use the runner's `GITHUB_API_URL`, support HTTPS GitHub
Enterprise API prefixes, reject redirects, and time out after 30 seconds.
Authentication, permission, network, malformed-response, and other API failures
fail the action rather than silently returning event-only details.

## SHA and branch semantics

- `event-sha` always preserves `GITHUB_SHA`.
- The `target-sha` output passes through a nonempty `target-sha` input unchanged;
  otherwise it is `GITHUB_SHA`.
- `action-target-branch` is the PR head branch for PR-related events. For other
  events it is the branch from `refs/heads/...`; tag events have an empty branch.
- A SHA override never changes the branch output. The action does not claim that
  an arbitrary supplied SHA belongs to that branch.
- For ordinary PR runs, `GITHUB_SHA` can be a synthetic merge commit while
  `pr-head-sha` is the contributor's commit. They are intentionally separate.
- `pull_request_target` uses the repository's default branch for its workflow
  ref/SHA on current GitHub.com. Its `action-target-branch` still identifies the PR
  head, and `pr-base-ref` identifies the PR's actual target branch. See
  [GitHub's ref semantics change](https://github.blog/changelog/2025-11-07-actions-pull_request_target-and-environment-branch-protections-changes/).
- PR comments can identify a PR without carrying its head/base refs or SHAs.
  Without API enrichment, unknown fields and the target branch remain empty;
  the default workflow branch is not substituted for the PR head.
- For events such as `workflow_run`, the workflow ref/SHA remain the current
  workflow's values, not those of an upstream run nested in the payload.

## Outputs

All individual outputs are strings. Booleans are `"true"` or `"false"`; unknown PR
fields are empty strings, not fabricated values. Compare Boolean outputs explicitly,
for example `if: steps.context.outputs.is-pr == 'true'`.

### Event, target, and run

| Output | Meaning |
| --- | --- |
| `event-name` | Triggering event, such as `push` or `pull_request`. |
| `event-action` | Activity such as `opened` or `synchronize`, when present. |
| `event-sha` | Original `GITHUB_SHA`. |
| `target-sha` | Nonempty `target-sha` input passed through unchanged, otherwise `GITHUB_SHA`. |
| `action-target-branch` | PR head branch, non-PR workflow branch, or empty when unknown/not applicable. |
| `ref` | Raw full workflow ref. |
| `ref-name` | Short workflow ref name, including `42/merge` for a PR merge ref. |
| `ref-type` | Workflow ref type, `branch` or `tag`, when available. |
| `tag` | Tag name for a tag ref; otherwise empty. |
| `repository` | Workflow repository, in `owner/name` form. |
| `repository-owner` | Workflow repository's owner. |
| `actor` | Actor that initiated the original run. |
| `triggering-actor` | Actor that initiated this attempt, when provided by GitHub. |
| `workflow` | Workflow name. |
| `run-id` | Workflow run ID. |
| `run-number` | Workflow run number. |
| `run-attempt` | Workflow run attempt. |
| `run-url` | Workflow run URL, when the run ID is available. |

### Pull request

| Output | Meaning |
| --- | --- |
| `is-pr` | Whether the event/ref identifies PR context. |
| `pr-details-status` | `none`, `partial`, or `full`, as defined below. |
| `pr-details-source` | `none`, `event`, or `api`. |
| `pr-number` | PR number. |
| `pr-url` | PR web URL; can be constructed from the repository and known PR number. |
| `pr-title` | PR title. |
| `pr-state` | PR state, normally `open` or `closed`. |
| `pr-author` | PR author's login, not necessarily the event actor. |
| `pr-draft` | Whether the PR is a draft. |
| `pr-merged` | Whether the PR is merged, when known. |
| `pr-is-fork` | Whether head and base repository names differ, case-insensitively. |
| `pr-head-ref` | Source branch name. |
| `pr-head-sha` | Source commit SHA. |
| `pr-head-repository` | Source repository, in `owner/name` form. |
| `pr-base-ref` | Target branch name. |
| `pr-base-sha` | Target commit SHA. |
| `pr-base-repository` | Target repository, in `owner/name` form. |
| `pr-labels` | JSON array of label names, `[]` when known to be empty, or empty when unknown. |

`none` means this is not a PR context. `full` means every PR field from `pr-number`
through `pr-labels` is known and each string is nonempty; `false` flags and an empty
label array count as known. Otherwise the status is `partial`. An event payload
can already be full without a token. Conversely, a deleted fork can leave the
head repository unknown even after an API lookup, so the result remains partial.

With a token, the action always fetches the identified PR, even if the event
already contains full details. `api` data reflects the lookup time and can differ
from the original event snapshot. A token does not enrich ordinary issue comments
that are unrelated to a PR. PR events whose payload and ref omit a PR number
remain partial without a token, but fail explicitly if API enrichment is requested.

### Structured context

`context` is a JSON object with exactly the other output names as keys. It retains
Boolean values, the label array, and `null` for unknown PR fields; IDs remain
strings. It never contains the token or the raw event payload.

```yaml
env:
  CONTEXT_JSON: ${{ steps.context.outputs.context }}
```

## Compatibility and development

Supports Linux, Windows, and macOS runners that support JavaScript actions using
`node24`; CI covers all three operating systems. Self-hosted runners must be kept
compatible with that runtime. GitHub Enterprise installations retain their own
event semantics; the action reports runner variables and payloads rather than
assuming GitHub.com's defaults.

From the repository root in PowerShell:

```powershell
npm --prefix .\workflow-context ci --ignore-scripts --no-audit --no-fund
npm --prefix .\workflow-context test
```

The repository's test driver uses `tests/run.sh`, which performs the same setup and
tests through Bash. The shipped JavaScript is the runtime artifact; no compilation
or bundling is needed. Action versions come from `workflow-context/vX.Y.Z` tags;
this private package is not published to npm.

Licensed under the repository's [MIT license](../LICENSE.md).

# automerge-dependabot

Merge an open, non-draft Dependabot PR into the workflow repository's default
branch after successful CI for its current head. No checkout, package installation,
or build step is required. The action never executes PR code or downloads
artifacts from the upstream run.

## Usage

Your repository should have a CI workflow that runs for pull requests. Add a
post-CI workflow like the example below to merge eligible Dependabot PRs after
that CI succeeds.

The caller chooses the runner and controls the trigger, permissions, and
human-facing messages.

You can use a published version tag or the full commit SHA of that action's
release. Version-tag references include the action name, for example
`btnguyen2k/gh-actions/automerge-dependabot@automerge-dependabot/v0.1.0`
(replace `v0.1.0` with your chosen published version).

For reproducibility and stronger supply-chain security, prefer the full released
commit SHA. Replace `<released-commit-sha>` below with that SHA, or use the
selected action-prefixed version tag instead.

```yaml
# Automatically merge PRs from dependabot

name: Automerge Dependabot

on:
  workflow_run:
    workflows: ["CI"]
    types: ["completed"]

permissions:
  contents: read

jobs:
  automerge:
    runs-on: ubuntu-latest
    if: |
      github.event.workflow_run.event == 'pull_request' &&
      github.event.workflow_run.conclusion == 'success'
    permissions:
      contents: write
      pull-requests: write
    steps:
      - name: Merge PR
        id: merge
        uses: btnguyen2k/gh-actions/automerge-dependabot@<released-commit-sha>
        with:
          github-token: ${{ github.token }}
```

The workflow file must exist on the default branch, and `"CI"` must match the
upstream workflow's name. Do not add `workflow_run.branches: [main]` to filter
PR targets: that filter applies to the upstream run's head branch, not its PR's
base branch. Failed or non-PR CI runs can still trigger the workflow, but the
example's job condition skips them.

The action independently rechecks the event and successful completion; the job
condition is an optimization, not its only safety guard.

### CI coordination

A successful upstream run does not mean every required check or review has
finished. The action makes one immediate merge attempt, and GitHub still enforces
all repository rules. A blocked merge fails the action without polling or retries.

Avoid duplicate `push` and `pull_request` CI runs publishing the same required
check names for a Dependabot head. Keep PR CI and exclude Dependabot branches
from push CI. For example, the upstream CI workflow can use:

```yaml
on:
  push:
    branches: ["**", "!dependabot/**"]
  pull_request:
```

Keep any existing branch exclusions, PR event types, and other triggers. This
avoids the duplicate-run race; other required checks and reviews must still be
satisfied when the merge is attempted. The recommended post-CI workflow ignores
`push` completions, so they cannot provide a later merge attempt.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `github-token` | `${{ github.token }}` | Nonempty token for the workflow repository. Grant `contents: write` and `pull-requests: write` on the caller job. |

An explicitly empty or whitespace-only token fails the action. No App or
personal token is required. The action cannot grant permissions or bypass
repository rules.

## Outputs

| Output | Meaning |
| --- | --- |
| `status` | `merged` or `skipped`. |
| `reason` | Stable skip code from the table below; empty when `status` is `merged`. |
| `pr-number` | Associated PR number as a string; empty when skipped before selecting a PR. |

| Skip reason | Meaning |
| --- | --- |
| `unsupported-event` | The caller was not triggered by `workflow_run`. |
| `not-pull-request` | The upstream run was not triggered by `pull_request`, or no upstream run was provided. |
| `ci-not-successful` | The activity was not `completed`, or the upstream conclusion was not `success`. |
| `no-associated-pr` | The upstream run has no first associated PR number. |
| `ineligible-pr` | The current PR is not an open, non-draft `dependabot[bot]` PR into the repository's default branch. |
| `head-sha-mismatch` | The current PR head differs from the successful upstream run's top-level `head_sha`. |

Use `steps.merge.outputs.status`, `steps.merge.outputs.reason`, and
`steps.merge.outputs.pr-number` in subsequent steps to decide how to react or
display messages. Expected skips do not emit emoji logs. Outputs are written
only after successful execution; fatal errors fail the step rather than returning
a successful status-only result.

## Merge behavior and safeguards

- Only the first PR in `workflow_run.pull_requests` is considered. Missing
  associations skip without any API request.
- Current PR details are fetched from the caller repository. Eligibility uses
  the PR author, not the workflow actor, so a human CI rerun is supported.
- The current head must equal `workflow_run.head_sha`. The same SHA is supplied
  to the merge API, which rejects a concurrent head change. Associated PR
  metadata's head SHA is never used as historical CI identity.
- Merging uses GitHub's default merge-commit method. The repository must allow
  merge commits. Required checks, reviews, and up-to-date rules still apply.
- Malformed PR numbers, missing or malformed CI/head SHAs, missing default-branch
  metadata, API errors, and unconfirmed merges fail explicitly. Errors are not
  retried or converted into skipped outcomes.

The action does not enable native auto-merge, approve PRs, update branches, poll
for checks, or bypass protections. Enabling GitHub's native auto-merge setting is
not required. Choose trusted upstream CI in the caller; a successful conclusion
does not replace repository protection rules.

## License

Licensed under the MIT License. See
[LICENSE.md](https://github.com/btnguyen2k/gh-actions/blob/main/LICENSE.md).

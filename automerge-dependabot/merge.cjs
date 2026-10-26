function skipped(reason, prNumber = '') {
  return { status: 'skipped', reason, 'pr-number': String(prNumber) };
}

module.exports = async function automergeDependabot({ github, context }) {
  if (context.eventName !== 'workflow_run') {
    return skipped('unsupported-event');
  }
  const run = context.payload.workflow_run;
  if (run?.event !== 'pull_request') {
    return skipped('not-pull-request');
  }
  if (context.payload.action !== 'completed' || run.conclusion !== 'success') {
    return skipped('ci-not-successful');
  }

  const prNumber = run.pull_requests?.[0]?.number;
  if (prNumber == null) {
    return skipped('no-associated-pr');
  }
  if (!Number.isSafeInteger(prNumber) || prNumber < 1) {
    throw new Error('workflow_run.pull_requests[0].number must be a positive integer.');
  }
  if (typeof run.head_sha !== 'string' || !/^[0-9a-f]{40}$/i.test(run.head_sha)) {
    throw new Error('workflow_run.head_sha must be a full 40-character hexadecimal commit SHA.');
  }
  const defaultBranch = context.payload.repository?.default_branch;
  if (typeof defaultBranch !== 'string' || !defaultBranch.trim()) {
    throw new Error('repository.default_branch must be a nonempty string.');
  }

  const request = { ...context.repo, pull_number: prNumber };
  const { data: pr } = await github.rest.pulls.get(request);
  if (
    pr.user?.login !== 'dependabot[bot]' ||
    pr.base.ref !== defaultBranch ||
    pr.state !== 'open' ||
    pr.draft
  ) {
    return skipped('ineligible-pr', prNumber);
  }
  if (typeof pr.head?.sha !== 'string' || !/^[0-9a-f]{40}$/i.test(pr.head.sha)) {
    throw new Error(`PR #${prNumber} does not contain a valid head SHA.`);
  }
  if (pr.head.sha !== run.head_sha) {
    return skipped('head-sha-mismatch', prNumber);
  }

  const { data: result } = await github.rest.pulls.merge({
    ...request,
    sha: run.head_sha,
  });
  if (result.merged !== true) {
    throw new Error(
      `PR #${prNumber} was not merged: ${result.message || 'GitHub did not confirm the merge.'}`,
    );
  }
  return { status: 'merged', reason: '', 'pr-number': String(prNumber) };
};

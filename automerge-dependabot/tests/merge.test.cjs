const assert = require('node:assert/strict');
const test = require('node:test');
const automergeDependabot = require('../merge.cjs');

const HEAD = 'a'.repeat(40);
const REQUEST = { owner: 'consumer', repo: 'project', pull_number: 17 };

function fixture({ mergeResult = { merged: true }, error } = {}) {
  const calls = [];
  const context = {
    eventName: 'workflow_run',
    repo: { owner: 'consumer', repo: 'project' },
    payload: {
      action: 'completed',
      repository: { default_branch: 'main' },
      workflow_run: {
        event: 'pull_request',
        conclusion: 'success',
        actor: { login: 'human-rerunning-ci' },
        head_sha: HEAD,
        pull_requests: [{ number: 17, head: { sha: HEAD } }],
      },
    },
  };
  const pr = {
    user: { login: 'dependabot[bot]' },
    base: { ref: 'main' },
    state: 'open',
    draft: false,
    head: { sha: HEAD },
  };
  const respond = (method, data) => async (request) => {
    calls.push({ method, ...request });
    if (error?.method === method) {
      throw error.value;
    }
    return { data };
  };
  const github = {
    rest: {
      pulls: {
        get: respond('get', pr),
        merge: respond('merge', mergeResult),
      },
    },
  };
  return {
    calls, context, pr,
    run: () => automergeDependabot({ github, context }),
  };
}

function skipped(reason, prNumber = '') {
  return { status: 'skipped', reason, 'pr-number': String(prNumber) };
}

test('merges the current CI head in the caller repository, even after a human rerun', async () => {
  const state = fixture();
  assert.deepEqual(await state.run(), { status: 'merged', reason: '', 'pr-number': '17' });
  assert.deepEqual(state.calls, [
    { method: 'get', ...REQUEST },
    { method: 'merge', ...REQUEST, sha: HEAD },
  ]);
});

test('skips other parent events without making API calls', async () => {
  for (const eventName of ['push', 'pull_request', 'pull_request_target', 'workflow_dispatch']) {
    const state = fixture();
    state.context.eventName = eventName;
    assert.deepEqual(await state.run(), skipped('unsupported-event'));
    assert.deepEqual(state.calls, []);
  }
});

test('skips upstream workflows that were not triggered by a pull request', async () => {
  for (const event of ['push', 'pull_request_target', 'workflow_dispatch', undefined]) {
    const state = fixture();
    state.context.payload.workflow_run.event = event;
    assert.deepEqual(await state.run(), skipped('not-pull-request'));
    assert.deepEqual(state.calls, []);
  }
  const state = fixture();
  delete state.context.payload.workflow_run;
  assert.deepEqual(await state.run(), skipped('not-pull-request'));
  assert.deepEqual(state.calls, []);
});

test('requires a successful upstream conclusion before making API calls', async () => {
  for (const conclusion of ['failure', 'cancelled', 'timed_out', 'skipped', null, undefined]) {
    const state = fixture();
    state.context.payload.workflow_run.conclusion = conclusion;
    assert.deepEqual(await state.run(), skipped('ci-not-successful'));
    assert.deepEqual(state.calls, []);
  }
});

test('requires the completed workflow_run activity even when the conclusion is success', async () => {
  for (const action of ['requested', 'in_progress', undefined]) {
    const state = fixture();
    state.context.payload.action = action;
    assert.deepEqual(await state.run(), skipped('ci-not-successful'));
    assert.deepEqual(state.calls, []);
  }
});

test('skips missing PR associations before making API calls', async () => {
  for (const associations of [undefined, null, [], [{}], [{ number: null }]]) {
    const state = fixture();
    state.context.payload.workflow_run.pull_requests = associations;
    assert.deepEqual(await state.run(), skipped('no-associated-pr'));
    assert.deepEqual(state.calls, []);
  }
});

test('processes only the first associated PR', async () => {
  const state = fixture();
  state.context.payload.workflow_run.pull_requests.push({ number: 18 });
  assert.deepEqual(await state.run(), { status: 'merged', reason: '', 'pr-number': '17' });
  assert.deepEqual(state.calls, [
    { method: 'get', ...REQUEST },
    { method: 'merge', ...REQUEST, sha: HEAD },
  ]);
});

test('rejects invalid associated PR numbers without making API calls', async () => {
  for (const number of [0, -1, 1.5, '17', true, Number.MAX_SAFE_INTEGER + 1, NaN]) {
    const state = fixture();
    state.context.payload.workflow_run.pull_requests[0].number = number;
    await assert.rejects(state.run(), /number must be a positive integer/);
    assert.deepEqual(state.calls, []);
  }
});

test('rejects missing or malformed CI SHAs instead of omitting the merge guard', async () => {
  for (const sha of [undefined, null, '', 123, 'not-a-sha', 'g'.repeat(40), 'a'.repeat(39)]) {
    const state = fixture();
    state.context.payload.workflow_run.head_sha = sha;
    await assert.rejects(state.run(), /head_sha must be a full 40-character hexadecimal commit SHA/);
    assert.deepEqual(state.calls, []);
  }
});

test('rejects missing or malformed default branches without making API calls', async () => {
  for (const branch of [undefined, null, '', ' \t', 123]) {
    const state = fixture();
    state.context.payload.repository.default_branch = branch;
    await assert.rejects(state.run(), /default_branch must be a nonempty string/);
    assert.deepEqual(state.calls, []);
  }
  const state = fixture();
  delete state.context.payload.repository;
  await assert.rejects(state.run(), /default_branch must be a nonempty string/);
  assert.deepEqual(state.calls, []);
});

test('skips ineligible PRs using freshly fetched metadata', async () => {
  for (const changes of [
    { user: { login: 'octocat' } },
    { user: { login: 'github-actions[bot]' } },
    { user: null },
    { base: { ref: 'release/dummy' } },
    { state: 'closed' },
    { state: 'closed', merged: true },
    { draft: true },
  ]) {
    const state = fixture();
    Object.assign(state.pr, changes);
    assert.deepEqual(await state.run(), skipped('ineligible-pr', 17));
    assert.deepEqual(state.calls, [{ method: 'get', ...REQUEST }]);
  }
});

test('uses the caller repository default branch instead of hardcoded main', async () => {
  const state = fixture();
  state.context.payload.repository.default_branch = 'trunk';
  assert.deepEqual(await state.run(), skipped('ineligible-pr', 17));
  assert.deepEqual(state.calls, [{ method: 'get', ...REQUEST }]);

  state.calls.length = 0;
  state.pr.base.ref = 'trunk';
  assert.deepEqual(await state.run(), { status: 'merged', reason: '', 'pr-number': '17' });
  assert.deepEqual(state.calls, [
    { method: 'get', ...REQUEST },
    { method: 'merge', ...REQUEST, sha: HEAD },
  ]);
});

test('uses top-level CI identity even when associated PR metadata has advanced', async () => {
  const state = fixture();
  const latest = 'b'.repeat(40);
  state.context.payload.workflow_run.pull_requests[0].head.sha = latest;
  state.pr.head.sha = latest;
  assert.deepEqual(await state.run(), skipped('head-sha-mismatch', 17));
  assert.deepEqual(state.calls, [{ method: 'get', ...REQUEST }]);
});

test('fails explicitly when the current PR response has no valid head SHA', async () => {
  for (const head of [undefined, null, {}, { sha: '' }, { sha: 'not-a-sha' }]) {
    const state = fixture();
    state.pr.head = head;
    await assert.rejects(state.run(), /PR #17 does not contain a valid head SHA/);
    assert.deepEqual(state.calls, [{ method: 'get', ...REQUEST }]);
  }
});

test('requires an explicitly confirmed merge rather than returning a success-shaped result', async () => {
  for (const merged of [false, undefined, 'true', 1]) {
    const state = fixture({
      mergeResult: { merged, message: 'Required status checks have not passed' },
    });
    await assert.rejects(
      state.run(), /PR #17 was not merged: Required status checks have not passed/,
    );
    assert.deepEqual(state.calls, [
      { method: 'get', ...REQUEST },
      { method: 'merge', ...REQUEST, sha: HEAD },
    ]);
  }
});

test('explains a missing merge confirmation even when GitHub supplies no message', async () => {
  const state = fixture({ mergeResult: {} });
  await assert.rejects(state.run(), /PR #17 was not merged: GitHub did not confirm the merge/);
});

test('propagates API errors including atomic SHA conflicts without retrying', async () => {
  for (const [method, message] of [
    ['get', '403 Forbidden'],
    ['merge', '405 Required status checks have not passed'],
    ['merge', '409 Head branch was modified'],
    ['merge', '500 Internal Server Error'],
  ]) {
    const failure = new Error(message);
    const state = fixture({ error: { method, value: failure } });
    await assert.rejects(state.run(), (error) => error === failure);
    assert.deepEqual(state.calls, method === 'get'
      ? [{ method: 'get', ...REQUEST }]
      : [{ method: 'get', ...REQUEST }, { method: 'merge', ...REQUEST, sha: HEAD }]);
  }
});

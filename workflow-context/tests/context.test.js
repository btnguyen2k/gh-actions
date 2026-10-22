import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { captureContext, parseJsonObject } from '../src/context.js';
import { escapeWorkflowValue, serializeOutputs } from '../src/outputs.js';

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const EVENT_SHA = '1'.repeat(40);
const HEAD_SHA = '2'.repeat(40);
const BASE_SHA = '3'.repeat(40);
const ENV = {
  GITHUB_EVENT_NAME: 'push',
  GITHUB_SHA: EVENT_SHA,
  GITHUB_REF: 'refs/heads/main',
  GITHUB_REF_NAME: '',
  GITHUB_REF_TYPE: '',
  GITHUB_HEAD_REF: '',
  GITHUB_BASE_REF: '',
  GITHUB_REPOSITORY: 'example/project',
  GITHUB_SERVER_URL: 'https://github.com',
  GITHUB_API_URL: 'https://api.github.com',
  GITHUB_ACTOR: 'original-actor',
  GITHUB_TRIGGERING_ACTOR: 'rerun-actor',
  GITHUB_WORKFLOW: 'Build',
  GITHUB_RUN_ID: '12345678901234567890',
  GITHUB_RUN_NUMBER: '12',
  GITHUB_RUN_ATTEMPT: '2',
};

function fullPr() {
  return {
    number: 42,
    html_url: 'https://github.com/example/project/pull/42',
    title: 'Improve context',
    state: 'open',
    user: { login: 'contributor' },
    draft: false,
    merged: false,
    head: {
      ref: 'feature/context',
      sha: HEAD_SHA,
      repo: { full_name: 'contributor/project' },
    },
    base: { ref: 'main', sha: BASE_SHA, repo: { full_name: 'example/project' } },
    labels: [{ name: 'enhancement' }, { name: 'ci' }],
  };
}

function prPayload(pr = fullPr()) {
  return { action: 'opened', number: 42, pull_request: pr };
}

function commentPayload() {
  return {
    action: 'created',
    issue: {
      number: 42,
      title: 'Improve context',
      state: 'open',
      user: { login: 'contributor' },
      labels: [],
      pull_request: {
        html_url: 'https://github.com/example/project/pull/42',
        merged_at: null,
      },
    },
  };
}

async function unexpectedFetch() {
  throw new Error('Unexpected API call in fixture.');
}

function collect({ env = {}, payload = {}, targetSha = '', token = '', fetcher = unexpectedFetch } = {}) {
  return captureContext({ env: { ...ENV, ...env }, payload, targetSha, token, fetcher });
}

function api(pr = fullPr()) {
  const calls = [];
  return {
    calls,
    fetcher: async (...args) => {
      calls.push(args);
      return new Response(JSON.stringify(pr), { status: 200 });
    },
  };
}

function parseOutputs(source) {
  const values = {};
  let index = 0;
  function readLine() {
    const newline = source.indexOf('\n', index);
    assert.notEqual(newline, -1, 'Output lines must have a newline.');
    const end = process.platform === 'win32' && source[newline - 1] === '\r'
      ? newline - 1 : newline;
    const line = source.slice(index, end);
    index = newline + 1;
    return { line, end };
  }
  while (index < source.length) {
    const [name, delimiter] = readLine().line.split('<<');
    assert.ok(name && delimiter, 'Outputs must use the multiline protocol.');
    const start = index;
    let end = start;
    let current = readLine();
    while (current.line !== delimiter) {
      end = current.end;
      current = readLine();
    }
    assert.equal(Object.hasOwn(values, name), false, 'Output names must be unique.');
    values[name] = source.slice(start, end);
  }
  return values;
}

async function runAction(t, { payload = {}, rawPayload, env = {}, existing = '' } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'workflow context-'));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const output = join(directory, 'output file');
  const event = join(directory, 'event payload.json');
  await writeFile(output, existing, 'utf8');
  await writeFile(event, rawPayload ?? JSON.stringify(payload), 'utf8');
  const result = spawnSync(process.execPath, [join(ROOT, 'index.js')], {
    cwd: directory,
    encoding: 'utf8',
    env: {
      ...process.env,
      ...ENV,
      'INPUT_TARGET-SHA': '',
      'INPUT_GITHUB-TOKEN': '',
      GITHUB_OUTPUT: output,
      GITHUB_EVENT_PATH: event,
      ...env,
    },
  });
  if (result.error) throw result.error;
  return { ...result, output: await readFile(output, 'utf8') };
}

test('captures push and rerun metadata without an API call', async () => {
  const context = await collect();
  assert.equal(context['event-name'], 'push');
  assert.equal(context['event-action'], '');
  assert.equal(context['event-sha'], EVENT_SHA);
  assert.equal(context['target-sha'], EVENT_SHA);
  assert.equal(context['action-target-branch'], 'main');
  assert.equal(context.ref, 'refs/heads/main');
  assert.equal(context['ref-name'], 'main');
  assert.equal(context['ref-type'], 'branch');
  assert.equal(context.tag, '');
  assert.equal(context.repository, 'example/project');
  assert.equal(context['repository-owner'], 'example');
  assert.equal(context.actor, 'original-actor');
  assert.equal(context['triggering-actor'], 'rerun-actor');
  assert.equal(context.workflow, 'Build');
  assert.equal(context['run-id'], ENV.GITHUB_RUN_ID);
  assert.equal(context['run-number'], '12');
  assert.equal(context['run-attempt'], '2');
  assert.equal(context['run-url'], `https://github.com/example/project/actions/runs/${ENV.GITHUB_RUN_ID}`);
  assert.equal(context['is-pr'], false);
  assert.equal(context['pr-details-status'], 'none');
  assert.equal(context['pr-details-source'], 'none');
  assert.equal(context['pr-number'], null);
  assert.equal(context['pr-draft'], null);
});

test('captures slash-containing tags without inventing a branch', async () => {
  const context = await collect({ env: { GITHUB_REF: 'refs/tags/workflow-context/v1.2.3' } });
  assert.equal(context.tag, 'workflow-context/v1.2.3');
  assert.equal(context['ref-name'], 'workflow-context/v1.2.3');
  assert.equal(context['ref-type'], 'tag');
  assert.equal(context['action-target-branch'], '');
});

test('preserves explicitly provided ref metadata and handles absent refs', async () => {
  const explicit = await collect({ env: { GITHUB_REF_NAME: 'provided', GITHUB_REF_TYPE: 'branch' } });
  assert.equal(explicit['ref-name'], 'provided');
  const absent = await collect({ env: { GITHUB_REF: '', GITHUB_RUN_ID: '' } });
  assert.equal(absent['action-target-branch'], '');
  assert.equal(absent['ref-name'], '');
  assert.equal(absent['ref-type'], '');
  assert.equal(absent['run-url'], '');
});

for (const event of ['workflow_dispatch', 'schedule', 'workflow_run', 'repository_dispatch']) {
  test(`${event} preserves the current workflow SHA, not nested payload SHAs`, async () => {
    const context = await collect({
      env: { GITHUB_EVENT_NAME: event },
      payload: { workflow_run: { head_sha: HEAD_SHA, head_branch: 'upstream' } },
    });
    assert.equal(context['event-sha'], EVENT_SHA);
    assert.equal(context['action-target-branch'], 'main');
    assert.equal(context['is-pr'], false);
  });
}

test('passes target-sha through unchanged without branch inference or API lookup', async () => {
  for (const target of [' caller-supplied-target ', '   ']) {
    const context = await collect({ targetSha: target });
    assert.equal(context['target-sha'], target);
    assert.equal(context['event-sha'], EVENT_SHA);
    assert.equal(context['action-target-branch'], 'main');
  }
});

for (const event of ['pull_request', 'pull_request_review', 'pull_request_review_comment', 'pull_request_review_thread']) {
  test(`${event} separates the workflow merge SHA from PR head/base details`, async () => {
    const context = await collect({
      env: { GITHUB_EVENT_NAME: event, GITHUB_REF: 'refs/pull/42/merge' },
      payload: prPayload(),
    });
    assert.equal(context['event-action'], 'opened');
    assert.equal(context['event-sha'], EVENT_SHA);
    assert.equal(context['target-sha'], EVENT_SHA);
    assert.equal(context['action-target-branch'], 'feature/context');
    assert.equal(context['ref-name'], '42/merge');
    assert.equal(context['is-pr'], true);
    assert.equal(context['pr-details-status'], 'full');
    assert.equal(context['pr-details-source'], 'event');
    assert.equal(context['pr-number'], '42');
    assert.equal(context['pr-title'], 'Improve context');
    assert.equal(context['pr-author'], 'contributor');
    assert.equal(context['pr-head-sha'], HEAD_SHA);
    assert.equal(context['pr-head-repository'], 'contributor/project');
    assert.equal(context['pr-base-ref'], 'main');
    assert.equal(context['pr-base-sha'], BASE_SHA);
    assert.equal(context['pr-base-repository'], 'example/project');
    assert.equal(context['pr-is-fork'], true);
    assert.equal(context['pr-draft'], false);
    assert.equal(context['pr-merged'], false);
    assert.deepEqual(context['pr-labels'], ['enhancement', 'ci']);
  });
}

test('pull_request_target does not confuse default, base, and head branches', async () => {
  const pr = fullPr();
  pr.base.ref = 'release/1.x';
  const context = await collect({
    env: { GITHUB_EVENT_NAME: 'pull_request_target', GITHUB_REF: 'refs/heads/main' },
    payload: prPayload(pr),
    targetSha: HEAD_SHA,
  });
  assert.equal(context['event-sha'], EVENT_SHA);
  assert.equal(context['target-sha'], HEAD_SHA);
  assert.equal(context.ref, 'refs/heads/main');
  assert.equal(context['action-target-branch'], 'feature/context');
  assert.equal(context['pr-base-ref'], 'release/1.x');
});

test('closed/merged PRs retain the source branch even when the workflow ref is a branch', async () => {
  const pr = { ...fullPr(), state: 'closed', merged: true };
  const context = await collect({
    env: { GITHUB_EVENT_NAME: 'pull_request' },
    payload: { ...prPayload(pr), action: 'closed' },
  });
  assert.equal(context['pr-state'], 'closed');
  assert.equal(context['pr-merged'], true);
  assert.equal(context['action-target-branch'], 'feature/context');
});

test('same-repository PR comparison is case-insensitive and empty labels are complete', async () => {
  const pr = fullPr();
  pr.head.repo.full_name = 'EXAMPLE/PROJECT';
  pr.labels = [];
  const context = await collect({ payload: prPayload(pr) });
  assert.equal(context['pr-is-fork'], false);
  assert.equal(context['pr-details-status'], 'full');
  assert.deepEqual(context['pr-labels'], []);
});

test('empty PR payloads use known runner refs without fabricating missing details', async () => {
  const context = await collect({
    env: {
      GITHUB_EVENT_NAME: 'pull_request',
      GITHUB_REF: 'refs/pull/42/merge',
      GITHUB_HEAD_REF: 'feature/from-env',
      GITHUB_BASE_REF: 'main',
    },
  });
  assert.equal(context['is-pr'], true);
  assert.equal(context['pr-details-status'], 'partial');
  assert.equal(context['pr-number'], '42');
  assert.equal(context['pr-url'], 'https://github.com/example/project/pull/42');
  assert.equal(context['action-target-branch'], 'feature/from-env');
  assert.equal(context['pr-base-ref'], 'main');
  assert.equal(context['pr-head-sha'], null);
  assert.equal(context['pr-draft'], null);
  assert.equal(context['pr-is-fork'], null);
  assert.equal(context['pr-labels'], null);
});

test('PR comments remain partial without substituting the default branch for the PR head', async () => {
  const context = await collect({ env: { GITHUB_EVENT_NAME: 'issue_comment' }, payload: commentPayload() });
  assert.equal(context['is-pr'], true);
  assert.equal(context['pr-number'], '42');
  assert.equal(context['pr-title'], 'Improve context');
  assert.equal(context['pr-merged'], false);
  assert.equal(context['pr-details-status'], 'partial');
  assert.equal(context['action-target-branch'], '');
  assert.equal(context['pr-head-sha'], null);
});

test('ordinary issue comments are not PRs and do not call the API even with a token', async () => {
  const payload = commentPayload();
  delete payload.issue.pull_request;
  const context = await collect({
    env: { GITHUB_EVENT_NAME: 'issue_comment' }, payload, token: 'test-token',
  });
  assert.equal(context['is-pr'], false);
  assert.equal(context['pr-details-status'], 'none');
  assert.equal(context['pr-number'], null);
  assert.equal(context['pr-title'], null);
  assert.equal(context['action-target-branch'], 'main');
});

test('a deleted fork is partial rather than incorrectly reported as a same-repository PR', async () => {
  const pr = fullPr();
  pr.head.repo = null;
  const context = await collect({ payload: prPayload(pr) });
  assert.equal(context['pr-details-status'], 'partial');
  assert.equal(context['pr-head-repository'], null);
  assert.equal(context['pr-is-fork'], null);
  assert.equal(context['action-target-branch'], 'feature/context');
});

test('a token enriches PR comments using the trusted API base rather than payload URLs', async () => {
  const client = api();
  const payload = commentPayload();
  payload.issue.pull_request.url = 'https://untrusted.invalid/pulls/42';
  const context = await collect({
    env: { GITHUB_EVENT_NAME: 'issue_comment', GITHUB_API_URL: 'https://ghe.example/api/v3/' },
    payload, token: 'test-token', fetcher: client.fetcher,
  });
  assert.equal(client.calls.length, 1);
  const [url, options] = client.calls[0];
  assert.equal(url, 'https://ghe.example/api/v3/repos/example/project/pulls/42');
  assert.equal(options.method, 'GET');
  assert.equal(options.headers.authorization, 'Bearer test-token');
  assert.equal(options.redirect, 'error');
  assert.ok(options.signal instanceof AbortSignal);
  assert.equal(context['pr-details-status'], 'full');
  assert.equal(context['pr-details-source'], 'api');
  assert.equal(context['action-target-branch'], 'feature/context');
  assert.equal(JSON.stringify(context).includes('test-token'), false);
});

test('a supplied token fetches even complete event details and preserves the event SHA', async () => {
  const current = fullPr();
  current.title = 'Updated after event';
  current.head.ref = 'renamed-branch';
  const client = api(current);
  const context = await collect({ payload: prPayload(), token: 'test-token', fetcher: client.fetcher });
  assert.equal(client.calls.length, 1);
  assert.equal(context['pr-title'], 'Updated after event');
  assert.equal(context['action-target-branch'], 'renamed-branch');
  assert.equal(context['pr-details-source'], 'api');
  assert.equal(context['event-sha'], EVENT_SHA);
});

test('a token is not used for non-PR pushes', async () => {
  const context = await collect({ token: 'test-token' });
  assert.equal(context['pr-details-source'], 'none');
});

for (const status of [401, 403, 404, 429, 500]) {
  test(`HTTP ${status} is an explicit enrichment failure, not partial success`, async () => {
    await assert.rejects(collect({
      payload: prPayload(), token: 'test-token',
      fetcher: async () => new Response('Not exposed in error output', { status }),
    }), new RegExp(`HTTP ${status}`));
  });
}

test('network failures propagate instead of falling back to event data', async () => {
  const failure = new Error('Connection reset');
  await assert.rejects(collect({
    payload: prPayload(), token: 'test-token', fetcher: async () => { throw failure; },
  }), failure);
});

test('API responses must identify the requested PR and contain valid JSON', async () => {
  const wrong = api({ ...fullPr(), number: 43 });
  await assert.rejects(
    collect({ payload: prPayload(), token: 'test-token', fetcher: wrong.fetcher }),
    /requested PR/,
  );
  await assert.rejects(collect({
    payload: prPayload(), token: 'test-token',
    fetcher: async () => new Response('private-response-fragment', { status: 200 }),
  }), { message: 'GitHub PR API response must contain valid JSON.' });
});

for (const url of ['http://ghe.example/api/v3', 'https://user:password@ghe.example', 'https://ghe.example?query=1', 'https://ghe.example#fragment']) {
  test(`rejects unsafe API base ${url} before using a token`, async () => {
    await assert.rejects(collect({
      payload: prPayload(), token: 'test-token', env: { GITHUB_API_URL: url },
    }), /HTTPS API URL/);
  });
}

test('API enrichment cannot proceed without a PR number', async () => {
  const env = { GITHUB_EVENT_NAME: 'pull_request_target' };
  const partial = await collect({ env });
  assert.equal(partial['pr-details-status'], 'partial');
  await assert.rejects(collect({ env, token: 'test-token' }), /does not identify a PR number/);
});

test('conflicting PR numbers fail before any API request', async () => {
  await assert.rejects(collect({
    payload: { ...prPayload(), number: 43 }, token: 'test-token',
  }), /conflicting PR numbers/);
});

test('missing required workflow variables and malformed event fields fail explicitly', async () => {
  for (const key of ['GITHUB_EVENT_NAME', 'GITHUB_SHA', 'GITHUB_REPOSITORY', 'GITHUB_SERVER_URL']) {
    await assert.rejects(collect({ env: { [key]: '' } }), new RegExp(key));
  }
  for (const payload of [
    null, [], { pull_request: [] }, prPayload({ ...fullPr(), draft: 'false' }),
    prPayload({ ...fullPr(), labels: [{}] }), prPayload({ ...fullPr(), number: -1 }),
  ]) {
    await assert.rejects(collect({ payload }), /must|nonempty/);
  }
  await assert.rejects(collect({ token: '   ' }), /single-line token/);
  await assert.rejects(collect({ token: 'token\nextra' }), /single-line token/);
});

test('JSON parsing reports malformed or non-object payloads without exposing their content', () => {
  assert.throws(() => parseJsonObject('{"private-fragment":', 'Payload'), {
    message: 'Payload must contain valid JSON.',
  });
  for (const source of ['[]', 'null', '"text"']) {
    assert.throws(() => parseJsonObject(source, 'Payload'), /JSON object/);
  }
});

test('context is a whitelist, not a dump of environment variables or webhook fields', async () => {
  const context = await collect({
    env: { GITHUB_TOKEN: 'environment-secret' },
    payload: { private_body: 'payload-secret', token: 'payload-token' },
  });
  const json = JSON.stringify(context);
  for (const secret of ['environment-secret', 'payload-secret', 'payload-token']) {
    assert.equal(json.includes(secret), false);
  }
});

test('outputs preserve multiline text, Unicode, booleans, arrays, nulls, and typed JSON', async () => {
  const pr = fullPr();
  pr.title = 'First line\r\ninjected=value\n::error::not-a-command\n\u6771\u4eac \u{1F680}\n';
  const context = await collect({ payload: prPayload(pr) });
  const outputs = parseOutputs(serializeOutputs(context));
  assert.equal(outputs['pr-title'], pr.title);
  assert.equal(outputs['pr-draft'], 'false');
  assert.equal(outputs['is-pr'], 'true');
  assert.equal(outputs['pr-labels'], '["enhancement","ci"]');
  assert.equal(Object.hasOwn(outputs, 'injected'), false);
  assert.deepEqual(JSON.parse(outputs.context), context);
  const absent = parseOutputs(serializeOutputs(await collect()));
  assert.equal(absent['is-pr'], 'false');
  assert.equal(absent['pr-draft'], '');
  assert.equal(absent['pr-labels'], '');
  assert.equal(JSON.parse(absent.context)['pr-draft'], null);
});

test('output encoding rerolls conflicting delimiters and rejects invalid names and values', () => {
  const ids = ['collision', 'safe', 'json-safe'];
  const outputs = parseOutputs(serializeOutputs({ example: 'gh_collision\ntext' }, () => ids.shift()));
  assert.equal(outputs.example, 'gh_collision\ntext');
  assert.throws(() => serializeOutputs({ 'bad\nname': 'value' }), /output name/);
  assert.throws(() => serializeOutputs({ bad: undefined }), /Invalid value/);
  assert.throws(() => serializeOutputs({ example: 'value' }, () => 'bad\nid'), /delimiter/);
  assert.equal(escapeWorkflowValue('value%\r\n::error::text'), 'value%25%0D%0A::error::text');
});

test('output values retain trailing carriage returns and mixed line endings', () => {
  for (const value of ['trailing\r', 'trailing\n', 'trailing\r\n', '\r', '\n', '']) {
    assert.equal(parseOutputs(serializeOutputs({ example: value })).example, value);
  }
});

test('runtime output names exactly match action metadata and are documented', async () => {
  const context = await collect();
  const metadata = (await readFile(join(ROOT, 'action.yml'), 'utf8')).replaceAll('\r\n', '\n');
  const outputBlock = metadata.split('\noutputs:\n')[1].split('\nruns:\n')[0];
  const declared = [...outputBlock.matchAll(/^  ([a-z][a-z0-9-]*):$/gm)].map((match) => match[1]);
  const emitted = Object.keys(parseOutputs(serializeOutputs(context)));
  assert.ok(declared.includes('target-sha'));
  assert.equal(declared.includes('action-target-sha'), false);
  assert.deepEqual(declared.sort(), emitted.sort());
  const readme = await readFile(join(ROOT, 'README.md'), 'utf8');
  for (const name of declared) assert.ok(readme.includes(`\`${name}\``), `${name} must be documented.`);
});

test('the Bash test entrypoint uses LF line endings on every platform', async () => {
  const script = await readFile(join(ROOT, 'tests', 'run.sh'), 'utf8');
  assert.ok(script.startsWith('#!/usr/bin/env bash\n'));
  assert.equal(script.includes('\r'), false);
});

test('the shipped action executes from an unrelated directory and appends UTF-8 outputs', async (t) => {
  const pr = fullPr();
  pr.title = 'A multiline\n\u6771\u4eac title';
  const result = await runAction(t, {
    payload: prPayload(pr),
    env: { GITHUB_EVENT_NAME: 'pull_request', GITHUB_REF: 'refs/pull/42/merge', 'INPUT_TARGET-SHA': 'override' },
    existing: 'previous<<old\npreserved\nold\n',
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, '');
  const outputs = parseOutputs(result.output);
  assert.equal(outputs.previous, 'preserved');
  assert.equal(outputs['target-sha'], 'override');
  assert.equal(outputs['event-sha'], EVENT_SHA);
  assert.equal(outputs['action-target-branch'], 'feature/context');
  assert.equal(outputs['pr-title'], pr.title);
  const snapshot = JSON.parse(outputs.context);
  assert.equal(snapshot['target-sha'], 'override');
  assert.equal(snapshot['action-target-branch'], 'feature/context');
});

test('the shipped action emits default outputs without a checkout or token', async (t) => {
  const result = await runAction(t);
  assert.equal(result.status, 0, result.stderr);
  const outputs = parseOutputs(result.output);
  assert.equal(outputs['target-sha'], EVENT_SHA);
  assert.equal(outputs['action-target-branch'], 'main');
  assert.equal(JSON.parse(outputs.context)['target-sha'], EVENT_SHA);
  assert.equal(outputs['pr-details-status'], 'none');
  assert.equal(outputs['pr-details-source'], 'none');
});

test('invalid event JSON fails without writing outputs or exposing payload fragments', async (t) => {
  const result = await runAction(t, { rawPayload: '{"private-fragment":' });
  assert.notEqual(result.status, 0);
  assert.equal(result.output, '');
  assert.match(result.stderr, /::error::workflow-context: GitHub event payload must contain valid JSON/);
  assert.equal(result.stderr.includes('private-fragment'), false);
});

test('missing GITHUB_OUTPUT fails rather than using deprecated stdout output commands', async (t) => {
  const result = await runAction(t, { env: { GITHUB_OUTPUT: '' } });
  assert.notEqual(result.status, 0);
  assert.equal(result.output, '');
  assert.match(result.stderr, /Missing required environment variable GITHUB_OUTPUT/);
  assert.equal(result.stdout.includes('::set-output'), false);
});

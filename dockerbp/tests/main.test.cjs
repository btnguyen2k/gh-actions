const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const {
  normalizeInputs, prepare, verifySource, run, cleanup, readBuildRecords, manifestDescriptors,
} = require('../main.cjs');

const SHA = 'a'.repeat(40);
const IMAGE = 'docker.io/owner/app';
const LINUX_DIGEST = `sha256:${'1'.repeat(64)}`;
const WINDOWS_DIGEST = `sha256:${'2'.repeat(64)}`;
const FINAL_DIGEST = `sha256:${'3'.repeat(64)}`;
const LINUX = { digest: LINUX_DIGEST, platform: { os: 'linux', architecture: 'amd64' } };
const WINDOWS = {
  digest: WINDOWS_DIGEST,
  platform: { os: 'windows', architecture: 'amd64', 'os.version': '10.0.26100.1', 'os.features': ['win32k'] },
};

function inputs(values = {}) {
  return { DOCKERBP_IMAGE: 'owner/app', GITHUB_SHA: SHA, ...values };
}

function fixture(t, values = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dockerbp-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true, maxRetries: 5 }));
  const workspace = path.join(root, 'caller workspace');
  const workDirectory = path.join(root, 'dockerbp-private', 'work');
  const sourceDirectory = path.join(workspace, '.dockerbp-source-test');
  fs.mkdirSync(sourceDirectory, { recursive: true });
  fs.mkdirSync(workDirectory, { recursive: true });
  fs.writeFileSync(path.join(sourceDirectory, 'Dockerfile'), 'FROM scratch\n');
  const config = {
    ...normalizeInputs(inputs()), workspace, workDirectory, sourceDirectory,
    contextDirectory: sourceDirectory, dockerfilePath: path.join(sourceDirectory, 'Dockerfile'),
    sourceVerified: true, recordFile: path.join(root, 'record.json'), ...values,
  };
  const dockerConfig = path.join(root, 'caller docker config');
  fs.mkdirSync(dockerConfig);
  fs.writeFileSync(path.join(dockerConfig, 'config.json'), '{}');
  return { root, config, env: { DOCKER_CONFIG: dockerConfig } };
}

function fakeDocker(config, manifests = [LINUX], fail = () => false) {
  const calls = [];
  const exec = async (command, args, options = {}) => {
    calls.push({ command, args, options });
    if (fail(args)) throw new Error('Simulated Docker failure.');
    if (args[0] === 'buildx' && args[1] === 'build') {
      fs.writeFileSync(args[args.indexOf('--metadata-file') + 1], JSON.stringify({
        'containerimage.digest': LINUX_DIGEST,
      }));
      return '';
    }
    if (args[0] === 'image' && ['build', 'push', 'rm'].includes(args[1])) return '';
    if (args[0] === 'image' && args[1] === 'inspect') {
      return JSON.stringify([`ghcr.io/other/app@${LINUX_DIGEST}`, `owner/app@${WINDOWS_DIGEST}`]);
    }
    if (args[0] === 'manifest' && args[1] === 'inspect') {
      const digest = args.at(-1).split('@')[1];
      const selected = manifests.filter(item => digest === item.digest);
      return JSON.stringify((selected.length ? selected : manifests).map(item => ({ Descriptor: item })));
    }
    if (args[0] === 'manifest' && ['create', 'annotate'].includes(args[1])) return '';
    if (args[0] === 'manifest' && args[1] === 'push') {
      return `Pushed ref ${IMAGE}@${LINUX_DIGEST}\n${FINAL_DIGEST}`;
    }
    throw new Error(`Unexpected command: ${command} ${args.join(' ')}`);
  };
  return { exec, calls };
}

function record(config, manifests, values = {}) {
  return {
    schema: 1, image: config.image, targetSha: config.sha, published: true,
    digest: manifests[0].digest,
    stagingTag: `${config.image}:dockerbp-shard`,
    platforms: manifests.map(item => `${item.platform.os}/${item.platform.architecture}`),
    manifests, ...values,
  };
}

function writeRecords(config, records) {
  const directory = path.join(config.workspace, 'downloaded records');
  fs.mkdirSync(directory);
  records.forEach((value, index) => {
    fs.writeFileSync(path.join(directory, `${index}.json`), JSON.stringify(value));
  });
  config.records = 'downloaded records';
}

test('empty lists default to latest and linux/amd64, with a deduplicated full-SHA tag', () => {
  const config = normalizeInputs(inputs({ DOCKERBP_TAGS: ' ,\n ', DOCKERBP_PLATFORMS: '\n, ' }));
  assert.deepEqual(config.tags, ['latest', SHA]);
  assert.deepEqual(config.platforms, ['linux/amd64']);
  assert.equal(config.publish, false);
  assert.equal(config.image, IMAGE);
  const selected = normalizeInputs(inputs({
    DOCKERBP_TARGET_SHA: 'B'.repeat(40), DOCKERBP_PUBLISH: 'TRUE',
    DOCKERBP_TAGS: `0.1.0, latest\n0.1.0\n${'b'.repeat(40)}`,
    DOCKERBP_PLATFORMS: 'linux/amd64,\nlinux/arm64,linux/amd64',
  }));
  assert.deepEqual(selected.tags, ['0.1.0', 'latest', 'b'.repeat(40)]);
  assert.deepEqual(selected.platforms, ['linux/amd64', 'linux/arm64']);
  assert.equal(selected.publish, true);
});

test('rejects malformed or unsafe inputs rather than passing them to a shell', () => {
  for (const [values, message] of [
    [{ DOCKERBP_IMAGE: '' }, /image must/],
    [{ DOCKERBP_IMAGE: 'ghcr.io/owner/app' }, /Only Docker Hub/],
    [{ DOCKERBP_IMAGE: 'owner/App' }, /image must/],
    [{ DOCKERBP_IMAGE: 'owner/app:latest' }, /image must/],
    [{ DOCKERBP_IMAGE: 'owner/app; echo injected' }, /image must/],
    [{ DOCKERBP_TARGET_SHA: 'main' }, /full 40-character/],
    [{ DOCKERBP_TARGET_SHA: '', GITHUB_SHA: '' }, /full 40-character/],
    [{ DOCKERBP_TAGS: '$(whoami)' }, /bare Docker tags/],
    [{ DOCKERBP_TAGS: `a${'b'.repeat(128)}` }, /bare Docker tags/],
    [{ DOCKERBP_PUBLISH: 'yes' }, /publish must/],
    [{ DOCKERBP_MODE: 'unknown' }, /mode must/],
    [{ DOCKERBP_PLATFORMS: 'linux/386' }, /platforms must/],
    [{ DOCKERBP_PLATFORMS: 'linux/amd64,windows/amd64' }, /separate build-shard/],
    [{ DOCKERBP_PLATFORMS: 'windows/amd64,windows/arm64' }, /one native Windows/],
    [{ DOCKERBP_CONTEXT: '../outside' }, /relative path/],
    [{ DOCKERBP_CONTEXT: 'C:\\outside' }, /relative path/],
    [{ DOCKERBP_CONTEXT: '\\\\host\\share' }, /relative path/],
    [{ DOCKERBP_DOCKERFILE: '..\\Dockerfile' }, /relative path/],
    [{ DOCKERBP_MODE: 'merge', DOCKERBP_BUILD_RECORDS: 'records' }, /publish=true/],
    [{ DOCKERBP_MODE: 'merge', DOCKERBP_PUBLISH: 'true' }, /build-records/],
  ]) {
    assert.throws(() => normalizeInputs(inputs(values)), message);
  }
});

test('accepts Docker Hub prefixes, nested paths, and mixed-OS merge expectations', () => {
  const config = normalizeInputs(inputs({
    DOCKERBP_IMAGE: 'docker.io/owner/app',
    DOCKERBP_CONTEXT: 'source\\app', DOCKERBP_DOCKERFILE: 'docker\\Dockerfile',
    DOCKERBP_MODE: 'merge', DOCKERBP_PUBLISH: 'true', DOCKERBP_BUILD_RECORDS: 'records',
    DOCKERBP_PLATFORMS: 'linux/arm64,windows/arm64',
  }));
  assert.equal(config.image, IMAGE);
  assert.equal(config.context, 'source/app');
  assert.equal(config.dockerfile, 'docker/Dockerfile');
  assert.equal(normalizeInputs(inputs({ DOCKERBP_IMAGE: 'app' })).image, 'docker.io/library/app');
  assert.equal(normalizeInputs(inputs({ DOCKERBP_IMAGE: 'index.docker.io/owner/app' })).image, IMAGE);
});

test('preparation checks daemon OS/native architecture and only requests needed Linux emulation', async t => {
  const { root, config } = fixture(t);
  const output = path.join(root, 'output.txt');
  const env = {
    ...inputs({ DOCKERBP_PLATFORMS: 'linux/amd64,linux/arm64' }),
    GITHUB_WORKSPACE: config.workspace, RUNNER_TEMP: root, GITHUB_OUTPUT: output,
  };
  const result = await prepare(env, async () => '{"Os":"linux","Arch":"amd64"}', 'linux');
  t.after(() => cleanup(result));
  assert.match(fs.readFileSync(output, 'utf8'), /qemu-platforms<<[^\n]+\narm64\n/);
  assert.notEqual(result.sourceDirectory, config.workspace);
  assert.equal(path.dirname(result.sourceDirectory), config.workspace);
  await assert.rejects(prepare(env, async () => '{"Os":"windows","Arch":"amd64"}', 'linux'), /container mode/);
  await assert.rejects(prepare(env, async () => '{}', 'darwin'), /native linux runner/);
  await assert.rejects(prepare({
    ...env, DOCKERBP_PLATFORMS: 'windows/arm64',
  }, async () => '{"Os":"windows","Arch":"amd64"}', 'win32'), /architecture must match/);
});

test('merge preparation does not contact a Docker daemon or allocate a source checkout', async t => {
  const { root, config } = fixture(t);
  const result = await prepare({
    ...inputs({ DOCKERBP_MODE: 'merge', DOCKERBP_PUBLISH: 'true', DOCKERBP_BUILD_RECORDS: 'records' }),
    GITHUB_WORKSPACE: config.workspace, RUNNER_TEMP: root, GITHUB_OUTPUT: path.join(root, 'output.txt'),
  }, async () => { throw new Error('Unexpected Docker invocation.'); }, 'darwin');
  t.after(() => cleanup(result));
  assert.equal(result.sourceDirectory, undefined);
});

test('verifies the exact checkout SHA before resolving the build paths', async t => {
  const { config } = fixture(t, { sourceVerified: false });
  await assert.rejects(verifySource(config, async () => 'b'.repeat(40)), /does not match/);
  assert.equal(config.sourceVerified, false);
  await verifySource(config, async (command, args, options) => {
    assert.equal(command, 'git');
    assert.deepEqual(args, ['rev-parse', 'HEAD']);
    assert.equal(options.cwd, config.sourceDirectory);
    return SHA;
  });
  assert.equal(config.contextDirectory, fs.realpathSync(config.sourceDirectory));
  assert.equal(config.dockerfilePath, fs.realpathSync(path.join(config.sourceDirectory, 'Dockerfile')));
  const unverified = { ...config, sourceVerified: false };
  await assert.rejects(run(unverified, async () => assert.fail('Unexpected build.')), /must be verified/);
});

test('rejects context and Dockerfile symlinks escaping the pinned build paths', async t => {
  const { root, config } = fixture(t);
  const outside = path.join(root, 'outside');
  fs.mkdirSync(outside);
  fs.writeFileSync(path.join(outside, 'Dockerfile'), 'FROM scratch\n');
  const link = path.join(config.sourceDirectory, 'escape');
  fs.symlinkSync(outside, link, process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(verifySource({ ...config, context: 'escape' }, async () => SHA), /inside the pinned/);
  await assert.rejects(verifySource({ ...config, dockerfile: 'escape/Dockerfile' }, async () => SHA), /inside the build context/);
});

test('Linux build-only is cache-only and cannot produce a mergeable registry result', async t => {
  const { config, env } = fixture(t);
  const { exec, calls } = fakeDocker(config);
  const result = await run(config, exec, env);
  assert.equal(result.status, 'built');
  assert.equal(result.digest, '');
  assert.equal(result['staging-tags'], '');
  const args = calls[0].args;
  assert.equal(args[args.indexOf('--output') + 1], 'type=cacheonly');
  assert.equal(args[args.indexOf('--file') + 1], config.dockerfilePath);
  assert.equal(args.at(-1), config.contextDirectory);
  assert.equal(calls.length, 1);
  const built = JSON.parse(fs.readFileSync(result['record-file'], 'utf8'));
  assert.equal(built.published, false);
  assert.deepEqual(built.manifests, []);
});

test('a published Linux shard emits immutable metadata but never changes final tags', async t => {
  const { config, env } = fixture(t, { publish: true, mode: 'build-shard' });
  const { exec, calls } = fakeDocker(config);
  const result = await run(config, exec, env);
  assert.equal(result.status, 'shard-published');
  assert.equal(result.digest, LINUX_DIGEST);
  assert.equal(calls.some(call => call.args[0] === 'manifest' && call.args[1] !== 'inspect'), false);
  const built = JSON.parse(fs.readFileSync(result['record-file'], 'utf8'));
  assert.equal(built.targetSha, SHA);
  assert.equal(built.image, IMAGE);
  assert.deepEqual(built.manifests, [LINUX]);
  assert.match(built.stagingTag, new RegExp(`^${IMAGE}:dockerbp-${SHA}-linux-amd64-[a-f0-9]+$`));
  assert.equal(JSON.stringify(built).includes('auths'), false);
});

test('single-job publication owns both requested and full-SHA final tags', async t => {
  const { config, env } = fixture(t, { publish: true, tags: ['0.1.0', 'latest', SHA] });
  const { exec, calls } = fakeDocker(config);
  const result = await run(config, exec, env);
  assert.equal(result.status, 'published');
  assert.equal(result.digest, FINAL_DIGEST);
  assert.deepEqual(calls.filter(call => call.args[1] === 'create').map(call => call.args[2]), [
    `${IMAGE}:0.1.0`, `${IMAGE}:latest`, `${IMAGE}:${SHA}`,
  ]);
  assert.equal(result.tags, `${IMAGE}:0.1.0\n${IMAGE}:latest\n${IMAGE}:${SHA}`);
});

test('native Windows shards use the legacy builder, find the correct repository digest, and remove only their local tag', async t => {
  const { config, env } = fixture(t, {
    backend: 'windows', platforms: ['windows/amd64'], publish: true, mode: 'build-shard',
  });
  const { exec, calls } = fakeDocker(config, [WINDOWS]);
  const result = await run(config, exec, env);
  assert.equal(result.digest, WINDOWS_DIGEST);
  assert.deepEqual(calls[0].args.slice(0, 4), ['image', 'build', '--platform', 'windows/amd64']);
  assert.equal(calls[0].options.env.DOCKER_BUILDKIT, '0');
  const staging = result['staging-tags'];
  assert.deepEqual(calls.find(call => call.args[1] === 'rm').args, ['image', 'rm', '--no-prune', staging]);
  assert.deepEqual(JSON.parse(fs.readFileSync(result['record-file'], 'utf8')).manifests, [WINDOWS]);
});

test('native Windows build-only removes its temporary tag without pushing', async t => {
  const { config, env } = fixture(t, { backend: 'windows', platforms: ['windows/amd64'] });
  const { exec, calls } = fakeDocker(config);
  assert.equal((await run(config, exec, env)).status, 'built');
  assert.deepEqual(calls.map(call => call.args.slice(0, 2)), [['image', 'build'], ['image', 'rm']]);
});

test('Windows push failures stay fatal and still remove the private local tag', async t => {
  const { config, env } = fixture(t, {
    backend: 'windows', platforms: ['windows/amd64'], publish: true,
  });
  const { exec, calls } = fakeDocker(config, [WINDOWS], args => args[1] === 'push');
  await assert.rejects(run(config, exec, env), /Simulated Docker failure/);
  assert.equal(calls.at(-1).args[1], 'rm');
  assert.equal(fs.existsSync(config.recordFile), false);
});

test('merges complete Linux/Windows records by digest and preserves Windows OS details', async t => {
  const { config, env } = fixture(t, {
    mode: 'merge', backend: 'merge', publish: true, platforms: ['linux/amd64', 'windows/amd64'],
  });
  writeRecords(config, [record(config, [LINUX]), record(config, [WINDOWS])]);
  const { exec, calls } = fakeDocker(config, [LINUX, WINDOWS]);
  const result = await run(config, exec, env);
  assert.equal(result.status, 'published');
  assert.equal(result['record-file'], '');
  const creates = calls.filter(call => call.args[1] === 'create');
  assert.deepEqual(creates[0].args.slice(3), [`${IMAGE}@${LINUX_DIGEST}`, `${IMAGE}@${WINDOWS_DIGEST}`]);
  const windows = calls.filter(call => call.args[1] === 'annotate' && call.args.includes('--os=windows'));
  assert.equal(windows.length, 2);
  assert.ok(windows[0].args.includes('--os-version=10.0.26100.1'));
  assert.ok(windows[0].args.includes('--os-features=win32k'));
  assert.equal(calls.findIndex(call => call.args[1] === 'create'), 2);
});

test('missing, duplicate, inconsistent, and unpublished records fail before registry writes', async t => {
  for (const [name, records] of [
    ['missing', config => [record(config, [LINUX])]],
    ['duplicate', config => [record(config, [LINUX]), record(config, [LINUX])]],
    ['duplicate declaration', config => [record(config, [LINUX], { platforms: ['linux/amd64', 'linux/amd64'] })]],
    ['wrong image', config => [record(config, [LINUX], { image: 'docker.io/other/app' })]],
    ['wrong SHA', config => [record(config, [LINUX], { targetSha: 'b'.repeat(40) })]],
    ['unpublished', config => [record(config, [LINUX], { published: false })]],
    ['wrong schema', config => [record(config, [LINUX], { schema: 2 })]],
    ['bad digest', config => [record(config, [LINUX], { digest: 'latest' })]],
    ['missing Windows version', config => [record(config, [{ ...WINDOWS, platform: { os: 'windows', architecture: 'amd64' } }])]],
  ]) {
    await t.test(name, async t => {
      const { config, env } = fixture(t, {
        mode: 'merge', backend: 'merge', publish: true, platforms: ['linux/amd64', 'windows/amd64'],
      });
      writeRecords(config, records(config));
      const { exec, calls } = fakeDocker(config);
      await assert.rejects(run(config, exec, env));
      assert.deepEqual(calls, []);
    });
  }
});

test('registry metadata mismatches fail before any final manifest is created', async t => {
  const { config, env } = fixture(t, {
    mode: 'merge', backend: 'merge', publish: true, platforms: ['windows/amd64'],
  });
  writeRecords(config, [record(config, [WINDOWS])]);
  const changed = { ...WINDOWS, platform: { ...WINDOWS.platform, 'os.version': '10.0.26100.2' } };
  const { exec, calls } = fakeDocker(config, [changed]);
  await assert.rejects(run(config, exec, env), /does not match/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].args[1], 'inspect');
});

test('oversized, malformed, and symlinked records are rejected without leaking JSON contents', t => {
  const { root, config } = fixture(t);
  const filename = path.join(root, 'record.json');
  config.records = filename;
  fs.writeFileSync(filename, '{"private-value":not-json');
  assert.throws(() => readBuildRecords(config), error =>
    error.message === 'Build record is not valid JSON.' && !error.message.includes('private-value'));
  fs.writeFileSync(filename, 'x'.repeat(1024 * 1024 + 1));
  assert.throws(() => readBuildRecords(config), /1 MiB/);
  const linked = path.join(root, 'linked records');
  fs.symlinkSync(config.workspace, linked, process.platform === 'win32' ? 'junction' : 'dir');
  config.records = linked;
  assert.throws(() => readBuildRecords(config), /symbolic links/);
});

test('isolates manifest cache/auth config and cleanup preserves caller credentials and result files', async t => {
  const { config, env } = fixture(t, { publish: true, mode: 'build-shard' });
  const original = { auths: { 'https://index.docker.io/v1/': { auth: 'test-only-placeholder' } }, currentContext: 'other' };
  const callerConfig = path.join(env.DOCKER_CONFIG, 'config.json');
  fs.writeFileSync(callerConfig, JSON.stringify(original));
  const { exec, calls } = fakeDocker(config);
  await run(config, exec, env);
  const privateEnv = calls.find(call => call.args[1] === 'inspect').options.env;
  assert.notEqual(privateEnv.DOCKER_CONFIG, env.DOCKER_CONFIG);
  assert.equal(privateEnv.DOCKER_CONTEXT, 'default');
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(privateEnv.DOCKER_CONFIG, 'config.json'), 'utf8')), {
    auths: original.auths,
  });
  assert.equal(fs.readFileSync(config.recordFile, 'utf8').includes('test-only-placeholder'), false);
  cleanup(config);
  assert.equal(fs.existsSync(config.workDirectory), false);
  assert.equal(fs.existsSync(config.sourceDirectory), false);
  assert.equal(fs.existsSync(config.recordFile), true);
  assert.deepEqual(JSON.parse(fs.readFileSync(callerConfig, 'utf8')), original);
});

test('cleanup refuses the caller workspace and removes private config before handling a locked checkout', t => {
  const { config } = fixture(t);
  assert.throws(() => cleanup({ ...config, sourceDirectory: config.workspace }), /unexpected source/);
  assert.equal(fs.existsSync(config.workDirectory), true);
  const remove = fs.rmSync;
  t.mock.method(fs, 'rmSync', (filename, options) => {
    if (filename === config.sourceDirectory) throw new Error('Checkout is locked.');
    return remove(filename, options);
  });
  assert.throws(() => cleanup(config), /Checkout is locked/);
  assert.equal(fs.existsSync(config.workDirectory), false);
  assert.equal(fs.existsSync(config.sourceDirectory), true);
});

test('CLI reports malformed inputs as fatal errors from an unrelated working directory', t => {
  const { root, config } = fixture(t);
  const filename = path.join(root, 'output.txt');
  const result = spawnSync(process.execPath, [path.resolve(__dirname, '..', 'main.cjs'), 'prepare'], {
    cwd: config.workspace, env: { ...process.env, ...inputs({ DOCKERBP_PUBLISH: 'invalid' }), GITHUB_OUTPUT: filename },
    encoding: 'utf8',
  });
  assert.equal(result.status, 1);
  assert.match(result.stderr, /::error::publish must be true or false/);
  assert.equal(fs.existsSync(filename), false);
});

test('manifest inspection rejects absent platforms and non-digest sources', () => {
  assert.throws(() => manifestDescriptors({ Descriptor: { digest: LINUX_DIGEST } }), /platform/);
  assert.throws(() => manifestDescriptors({ Descriptor: { digest: 'latest', platform: LINUX.platform } }), /digest/);
  assert.throws(() => manifestDescriptors([]), /no image manifests/);
});

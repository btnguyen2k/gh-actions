const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const http = require('node:http');
const crypto = require('node:crypto');
const { run, execute, cleanup } = require('../main.cjs');

const IMAGE_TYPE = 'application/vnd.docker.distribution.manifest.v2+json';
const INDEX_TYPE = 'application/vnd.docker.distribution.manifest.list.v2+json';
const CONFIG_TYPE = 'application/vnd.docker.container.image.v1+json';
const digestOf = bytes => `sha256:${crypto.createHash('sha256').update(bytes).digest('hex')}`;

async function registry(t) {
  const objects = new Map();
  const pushed = new Map();
  function store(value) {
    const bytes = Buffer.from(JSON.stringify(value));
    const digest = digestOf(bytes);
    objects.set(digest, { bytes, mediaType: value.mediaType || 'application/octet-stream' });
    return { digest, size: bytes.length, mediaType: value.mediaType };
  }
  function image(platform) {
    const config = store({
      ...platform, rootfs: { type: 'layers', diff_ids: [] },
      config: {}, history: [],
    });
    return {
      ...store({
        schemaVersion: 2, mediaType: IMAGE_TYPE,
        config: { ...config, mediaType: CONFIG_TYPE }, layers: [],
      }),
      platform,
    };
  }
  const linuxAMD64 = image({ os: 'linux', architecture: 'amd64' });
  const linuxARM64 = image({ os: 'linux', architecture: 'arm64', variant: 'v8' });
  const windows = image({
    os: 'windows', architecture: 'amd64',
    'os.version': '10.0.26100.1', 'os.features': ['win32k'],
  });
  const linux = store({ schemaVersion: 2, mediaType: INDEX_TYPE, manifests: [linuxAMD64, linuxARM64] });
  const server = http.createServer(async (request, response) => {
    response.setHeader('Docker-Distribution-Api-Version', 'registry/2.0');
    if (request.url === '/v2/') {
      response.writeHead(200, { 'Content-Type': 'application/json' }).end('{}');
      return;
    }
    const match = /^\/v2\/test\/image\/(manifests|blobs)\/([^?]+)$/.exec(request.url);
    if (!match) {
      response.writeHead(404).end();
      return;
    }
    const reference = decodeURIComponent(match[2]);
    if (request.method === 'PUT' && match[1] === 'manifests') {
      const chunks = [];
      for await (const chunk of request) chunks.push(chunk);
      const bytes = Buffer.concat(chunks);
      const value = JSON.parse(bytes.toString('utf8'));
      if (value.manifests?.some(item => !objects.has(item.digest))) {
        response.writeHead(400).end();
        return;
      }
      const digest = digestOf(bytes);
      objects.set(digest, { bytes, mediaType: value.mediaType });
      objects.set(reference, { bytes, mediaType: value.mediaType });
      pushed.set(reference, { digest, value });
      response.writeHead(201, {
        'Docker-Content-Digest': digest, Location: request.url,
      }).end();
      return;
    }
    const value = objects.get(reference);
    if (!value) {
      response.writeHead(404, { 'Content-Type': 'application/json' }).end(JSON.stringify({
        errors: [{ code: 'MANIFEST_UNKNOWN', message: 'not found' }],
      }));
      return;
    }
    response.writeHead(200, {
      'Content-Type': value.mediaType, 'Content-Length': value.bytes.length,
      'Docker-Content-Digest': digestOf(value.bytes),
    });
    response.end(request.method === 'HEAD' ? undefined : value.bytes);
  });
  server.on('clientError', (_error, socket) => socket.end('HTTP/1.1 400 Bad Request\r\n\r\n'));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  return {
    image: `127.0.0.1:${server.address().port}/test/image`,
    linux, windows, manifests: [linuxAMD64, linuxARM64, windows], pushed,
  };
}

async function available(t) {
  try {
    await execute('docker', ['--version']);
  } catch (error) {
    if (!error.message.endsWith('ENOENT.')) throw error;
    t.skip('Docker CLI is not installed; native CLI protocol checks run on Linux/Windows CI.');
    return false;
  }
  await execute('docker', ['manifest', '--help']);
  return true;
}

function configuration(t, source, mutate = () => {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dockerbp-registry-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true, maxRetries: 5 }));
  const workDirectory = path.join(root, 'dockerbp-private', 'work');
  const records = path.join(root, 'records');
  const dockerConfig = path.join(root, 'caller-docker');
  for (const directory of [workDirectory, records, dockerConfig]) fs.mkdirSync(directory, { recursive: true });
  fs.writeFileSync(path.join(dockerConfig, 'config.json'), '{}');
  const sha = 'a'.repeat(40);
  const config = {
    mode: 'merge', backend: 'merge', image: source.image, sha, publish: true,
    platforms: ['linux/amd64', 'linux/arm64', 'windows/amd64'],
    tags: ['latest', sha], workspace: root, workDirectory, records,
  };
  for (const [name, digest, manifests] of [
    ['linux', source.linux.digest, source.manifests.slice(0, 2)],
    ['windows', source.windows.digest, source.manifests.slice(2)],
  ]) {
    const record = {
      schema: name === 'linux' ? 2 : 1, image: config.image, targetSha: sha, published: true, digest,
      platforms: manifests.map(item => `${item.platform.os}/${item.platform.architecture}`),
      manifests: manifests.map(item => ({ digest: item.digest, platform: item.platform })),
      stagingTag: name === 'linux' ? '' : `${config.image}:dockerbp-${name}`,
    };
    mutate(record);
    fs.writeFileSync(path.join(records, `${name}.json`), JSON.stringify(record));
  }
  return { config, env: { DOCKER_CONFIG: dockerConfig } };
}

// Only this localhost test adapter permits HTTP; the public action always uses Docker Hub TLS.
async function localDocker(command, args, options) {
  const modified = [...args];
  if (modified[0] === 'manifest' && ['inspect', 'create', 'push'].includes(modified[1])) {
    modified.splice(2, 0, '--insecure');
  }
  return execute(command, modified, options);
}

test('real Docker CLI merges digest-only Linux and tagged Windows without a daemon', { timeout: 90000 }, async t => {
  if (!await available(t)) return;
  const source = await registry(t);
  const { config, env } = configuration(t, source);
  const result = await run(config, localDocker, env);
  assert.equal(result.status, 'published');
  assert.equal(result['staging-tags'], `${config.image}:dockerbp-windows`);
  assert.equal(result.digest, source.pushed.get('latest').digest);
  assert.equal(source.pushed.get(config.sha).digest, result.digest);
  const manifests = source.pushed.get('latest').value.manifests;
  assert.deepEqual(manifests.map(item => `${item.platform.os}/${item.platform.architecture}`).sort(), config.platforms);
  assert.deepEqual(manifests.find(item => item.platform.os === 'windows').platform, {
    architecture: 'amd64', os: 'windows', 'os.version': '10.0.26100.1', 'os.features': ['win32k'],
  });
  assert.equal(manifests.find(item => item.platform.architecture === 'arm64').platform.variant, 'v8');
  assert.deepEqual(manifests.map(item => item.digest).sort(), source.manifests.map(item => item.digest).sort());
  cleanup(config);
});

test('native Linux publication pushes by digest without marker tags', {
  timeout: 240000, skip: process.env.DOCKERBP_NATIVE_PUBLISH_TEST !== 'true',
}, async t => {
  assert.equal(await execute('docker', ['version', '--format', '{{.Server.Os}}']), 'linux');
  const network = await execute('docker', ['network', 'create', `dockerbp-test-${crypto.randomUUID()}`]);
  const registryHost = 'dockerbp-registry:5000';
  const builder = `dockerbp-test-${crypto.randomUUID()}`;
  let container;
  let builderCreated = false;
  t.after(async () => {
    const removals = [];
    if (builderCreated) removals.push(execute('docker', ['buildx', 'rm', '--force', builder]));
    if (container) removals.push(execute('docker', ['container', 'rm', '--force', '--volumes', container]));
    const results = await Promise.allSettled(removals);
    await execute('docker', ['network', 'rm', network]);
    for (const result of results) if (result.status === 'rejected') throw result.reason;
  });
  container = await execute('docker', [
    'run', '--detach', '--network', network, '--network-alias', 'dockerbp-registry',
    '--publish', '127.0.0.1::5000', 'registry:3',
  ]);
  const address = await execute('docker', ['port', container, '5000/tcp']);
  assert.match(address, /^127\.0\.0\.1:\d+$/);
  const registryURL = `http://${address}`;
  let ready = false;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      ready = (await fetch(`${registryURL}/v2/`, { signal: AbortSignal.timeout(1000) })).ok;
    } catch {}
    if (ready) break;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  assert.ok(ready, 'The local registry did not become ready.');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dockerbp-buildx-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true, maxRetries: 5 }));
  const buildkitConfig = path.join(root, 'buildkit.toml');
  fs.writeFileSync(buildkitConfig, `[registry."${registryHost}"]\nhttp = true\n`);
  await execute('docker', [
    'buildx', 'create', '--name', builder, '--driver', 'docker-container',
    '--driver-opt', `network=${network}`, '--buildkitd-config', buildkitConfig,
  ]);
  builderCreated = true;
  await execute('docker', ['buildx', 'inspect', builder, '--bootstrap']);
  // BuildKit uses container DNS; native CLI commands use the published loopback port.
  const nativeDocker = (command, args, options) => {
    const modified = args[0] === 'buildx' && args[1] === 'build'
      ? [...args.slice(0, 2), '--builder', builder, ...args.slice(2)]
      : args.map(value => value.startsWith(`${registryHost}/`)
        ? `${address}${value.slice(registryHost.length)}` : value);
    return localDocker(command, modified, options);
  };
  for (const mode of ['build-shard', 'build']) {
    for (const platforms of ['linux/amd64', 'linux/arm64', 'linux/amd64,linux/arm64']) {
      await t.test(`${mode} ${platforms}`, async t => {
        const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'dockerbp-native-test-'));
        t.after(() => fs.rmSync(workspace, { recursive: true, force: true, maxRetries: 5 }));
        const context = path.join(workspace, 'source');
        const workDirectory = path.join(workspace, 'dockerbp-private', 'work');
        const dockerConfig = path.join(workspace, 'caller-docker');
        for (const directory of [context, workDirectory, dockerConfig]) fs.mkdirSync(directory, { recursive: true });
        fs.writeFileSync(path.join(dockerConfig, 'config.json'), '{}');
        fs.writeFileSync(path.join(context, 'Dockerfile'), 'FROM scratch\nCOPY payload.txt /payload.txt\n');
        fs.writeFileSync(path.join(context, 'payload.txt'), 'DockerBP native publication check\n');
        const repository = `test/${mode}-${platforms.replaceAll('/', '-').replaceAll(',', '-')}`;
        const config = {
          mode, backend: 'linux', publish: true, image: `${registryHost}/${repository}`,
          sha: 'a'.repeat(40), tags: ['latest', 'a'.repeat(40)],
          workspace, workDirectory, platforms: platforms.split(','),
          sourceVerified: true, contextDirectory: context,
          dockerfilePath: path.join(context, 'Dockerfile'),
          recordFile: path.join(workspace, 'result.json'),
        };
        const result = await run(config, nativeDocker, { DOCKER_CONFIG: dockerConfig });
        const response = await fetch(`${registryURL}/v2/${repository}/tags/list`);
        if (mode === 'build-shard' && response.status === 404) {
          // Digest-only repositories may have no tag directory.
          assert.deepEqual((await response.json()).errors.map(error => error.code), ['NAME_UNKNOWN']);
        } else {
          assert.ok(response.ok, `Registry tag listing failed (${response.status}).`);
          const { tags } = await response.json();
          assert.deepEqual((tags || []).sort(), mode === 'build' ? [...config.tags].sort() : []);
        }
        assert.equal(result.status, mode === 'build' ? 'published' : 'shard-published');
        assert.equal(result['staging-tags'], '');
        const record = JSON.parse(fs.readFileSync(result['record-file'], 'utf8'));
        assert.equal(record.schema, 2);
        assert.equal(record.stagingTag, '');
        assert.deepEqual(record.manifests.map(item =>
          `${item.platform.os}/${item.platform.architecture}`).sort(), [...config.platforms].sort());
        if (mode === 'build') {
          const manifest = await fetch(`${registryURL}/v2/${repository}/manifests/${config.sha}`, {
            headers: { Accept: INDEX_TYPE },
          });
          assert.ok(manifest.ok);
          assert.equal(result.digest, manifest.headers.get('Docker-Content-Digest'));
        }
        cleanup(config);
      });
    }
  }
});

test('real registry verification rejects forged Windows version metadata before publishing any tag', { timeout: 90000 }, async t => {
  if (!await available(t)) return;
  const source = await registry(t);
  const { config, env } = configuration(t, source, record => {
    if (record.platforms[0] === 'windows/amd64') record.manifests[0].platform = {
      ...record.manifests[0].platform, 'os.version': '10.0.26100.2',
    };
  });
  await assert.rejects(run(config, localDocker, env), /does not match/);
  assert.equal(source.pushed.size, 0);
  cleanup(config);
});

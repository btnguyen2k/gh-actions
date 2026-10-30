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
      schema: 1, image: config.image, targetSha: sha, published: true, digest,
      platforms: manifests.map(item => `${item.platform.os}/${item.platform.architecture}`),
      manifests: manifests.map(item => ({ digest: item.digest, platform: item.platform })),
      stagingTag: `${config.image}:dockerbp-${name}`,
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

test('real Docker CLI merges Linux/Windows manifests without a daemon and preserves OS metadata', { timeout: 90000 }, async t => {
  if (!await available(t)) return;
  const source = await registry(t);
  const { config, env } = configuration(t, source);
  const result = await run(config, localDocker, env);
  assert.equal(result.status, 'published');
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

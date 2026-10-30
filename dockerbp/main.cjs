const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFile, spawn } = require('node:child_process');
const { promisify } = require('node:util');

const execFileAsync = promisify(execFile);
const PLATFORMS = new Set(['linux/amd64', 'linux/arm64', 'windows/amd64', 'windows/arm64']);
const DIGEST = /^sha256:[a-f0-9]{64}$/;
const TAG = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/;
const COMPONENT = /^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$/;

function list(value, fallback) {
  const entries = String(value || '').split(/[,\r\n]+/).map(item => item.trim()).filter(Boolean);
  return [...new Set(entries.length ? entries : fallback)];
}

function imageName(value) {
  const parts = String(value || '').trim().split('/');
  if (['docker.io', 'index.docker.io'].includes(parts[0])) {
    parts.shift();
  } else if (parts.length > 1 && /[.:]/.test(parts[0])) {
    throw new Error('Only Docker Hub images are supported.');
  }
  if (parts.length === 1) parts.unshift('library');
  if (parts.length !== 2 || parts.some(part => !COMPONENT.test(part)) ||
      parts.join('/').length > 255) {
    throw new Error('image must be a lowercase Docker Hub name without a tag or digest.');
  }
  return `docker.io/${parts.join('/')}`;
}

function relativePath(value, name, fallback) {
  const result = String(value || '').trim() || fallback;
  const normalized = result.replace(/\\/g, '/');
  if (/[\x00-\x1f\x7f:]/.test(result) || path.posix.isAbsolute(normalized) ||
      normalized.split('/').includes('..')) {
    throw new Error(`${name} must be a relative path without parent traversal.`);
  }
  return normalized;
}

function normalizeInputs(env = process.env) {
  const mode = env.DOCKERBP_MODE?.trim() || 'build';
  if (!['build', 'build-shard', 'merge'].includes(mode)) {
    throw new Error('mode must be build, build-shard, or merge.');
  }
  const publishInput = env.DOCKERBP_PUBLISH?.trim().toLowerCase() || 'false';
  if (!['true', 'false'].includes(publishInput)) {
    throw new Error('publish must be true or false.');
  }
  const sha = (env.DOCKERBP_TARGET_SHA?.trim() || env.GITHUB_SHA || '').toLowerCase();
  if (!/^[a-f0-9]{40}$/.test(sha)) {
    throw new Error('target-sha (or GITHUB_SHA) must be a full 40-character commit SHA.');
  }
  const platforms = list(env.DOCKERBP_PLATFORMS, ['linux/amd64']);
  if (platforms.some(platform => !PLATFORMS.has(platform))) {
    throw new Error(`platforms must contain only ${[...PLATFORMS].join(', ')}.`);
  }
  const operatingSystems = new Set(platforms.map(platform => platform.split('/')[0]));
  if (mode !== 'merge' && operatingSystems.size !== 1) {
    throw new Error('Build Linux and Windows platforms in separate build-shard jobs, then merge.');
  }
  if (mode !== 'merge' && platforms[0].startsWith('windows/') && platforms.length !== 1) {
    throw new Error('Each Windows build job must target one native Windows architecture.');
  }
  const tags = list(env.DOCKERBP_TAGS, ['latest']);
  if (tags.some(tag => !TAG.test(tag))) {
    throw new Error('tags must be bare Docker tags of 1-128 letters, digits, underscores, dots, or hyphens.');
  }
  if (!tags.includes(sha)) tags.push(sha);
  const records = env.DOCKERBP_BUILD_RECORDS?.trim() || '';
  if (mode === 'merge' && (publishInput !== 'true' || !records)) {
    throw new Error('merge mode requires publish=true and build-records from published shards.');
  }
  return {
    mode,
    image: imageName(env.DOCKERBP_IMAGE),
    sha,
    platforms,
    tags,
    publish: publishInput === 'true',
    context: relativePath(env.DOCKERBP_CONTEXT, 'context', './'),
    dockerfile: relativePath(env.DOCKERBP_DOCKERFILE, 'dockerfile', 'Dockerfile'),
    records,
    backend: mode === 'merge' ? 'merge' : platforms[0].split('/')[0],
  };
}

async function execute(command, args, { stream = false, env = {}, cwd } = {}) {
  const options = { cwd, env: { ...process.env, ...env }, windowsHide: true };
  if (stream) {
    await new Promise((resolve, reject) => {
      const child = spawn(command, args, { ...options, stdio: ['ignore', 'inherit', 'inherit'] });
      child.once('error', error => reject(new Error(`${command} could not start: ${error.code}.`)));
      child.once('close', code => code === 0 ? resolve() :
        reject(new Error(`${command} ${args.slice(0, 2).join(' ')} failed (exit ${code}).`)));
    });
    return '';
  }
  try {
    const result = await execFileAsync(command, args, {
      ...options, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024,
    });
    return result.stdout.trim();
  } catch (error) {
    throw new Error(
      `${command} ${args.slice(0, 2).join(' ')} failed: ${error.stderr?.trim() || error.code}.`,
    );
  }
}

function parseJSON(text, label) {
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`${label} is not valid JSON.`);
  }
}

function writeOutputs(values, filename = process.env.GITHUB_OUTPUT) {
  if (!filename) throw new Error('GITHUB_OUTPUT is required.');
  for (const [name, value] of Object.entries(values)) {
    const delimiter = `dockerbp_${crypto.randomUUID()}`;
    fs.appendFileSync(filename, `${name}<<${delimiter}\n${value}\n${delimiter}\n`, 'utf8');
  }
}

async function prepare(env = process.env, exec = execute, runnerOS = process.platform) {
  const config = normalizeInputs(env);
  config.workspace = path.resolve(env.GITHUB_WORKSPACE || process.cwd());
  let emulation = [];
  if (config.backend !== 'merge') {
    const expectedOS = config.backend === 'windows' ? 'win32' : 'linux';
    if (runnerOS !== expectedOS) {
      throw new Error(`${config.backend} builds require a native ${config.backend} runner.`);
    }
    const server = parseJSON(
      await exec('docker', ['version', '--format', '{{json .Server}}']),
      'Docker server information',
    );
    if (!server || server.Os !== config.backend) {
      throw new Error(`A Docker daemon in ${config.backend} container mode is required.`);
    }
    if (config.backend === 'windows' && config.platforms[0] !== `windows/${server.Arch}`) {
      throw new Error('The Windows Docker daemon architecture must match the requested platform.');
    }
    emulation = config.platforms.map(platform => platform.split('/')[1])
      .filter(architecture => architecture !== server.Arch);
  }
  const directory = fs.mkdtempSync(path.join(env.RUNNER_TEMP || os.tmpdir(), 'dockerbp-'));
  config.workDirectory = path.join(directory, 'work');
  fs.mkdirSync(config.workDirectory, { mode: 0o700 });
  if (config.backend !== 'merge') {
    config.sourceDirectory = fs.mkdtempSync(path.join(config.workspace, '.dockerbp-source-'));
  }
  const label = config.platforms.join('-').replaceAll('/', '-');
  config.recordFile = path.join(directory, `dockerbp-${label}-${crypto.randomUUID()}.json`);
  const configuration = path.join(config.workDirectory, 'configuration.json');
  fs.writeFileSync(configuration, JSON.stringify(config), { mode: 0o600 });
  writeOutputs({
    configuration,
    backend: config.backend,
    'target-sha': config.sha,
    'checkout-path': config.sourceDirectory ? path.basename(config.sourceDirectory) : '',
    'qemu-platforms': emulation.join(','),
  }, env.GITHUB_OUTPUT);
  return config;
}

function inside(root, filename) {
  const relative = path.relative(root, filename);
  return relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}

async function verifySource(config, exec = execute) {
  const head = await exec('git', ['rev-parse', 'HEAD'], { cwd: config.sourceDirectory });
  if (head.toLowerCase() !== config.sha) {
    throw new Error('Checked-out HEAD does not match target-sha.');
  }
  const source = fs.realpathSync(config.sourceDirectory);
  const context = fs.realpathSync(path.resolve(source, config.context));
  if (!inside(source, context) || !fs.statSync(context).isDirectory()) {
    throw new Error('context must resolve to a directory inside the pinned checkout.');
  }
  const dockerfile = fs.realpathSync(path.resolve(context, config.dockerfile));
  if (!inside(context, dockerfile) || !fs.statSync(dockerfile).isFile()) {
    throw new Error('dockerfile must resolve to a file inside the build context.');
  }
  config.contextDirectory = context;
  config.dockerfilePath = dockerfile;
  config.sourceVerified = true;
}

function platformInfo(value) {
  if (!value || !PLATFORMS.has(`${value.os}/${value.architecture}`)) {
    throw new Error('Build metadata contains an unsupported or missing platform.');
  }
  const result = { os: value.os, architecture: value.architecture };
  for (const name of ['variant', 'os.version']) {
    if (value[name] !== undefined) {
      if (typeof value[name] !== 'string' || value[name].length > 128 ||
          !/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(value[name])) {
        throw new Error(`Build metadata contains an invalid ${name}.`);
      }
      result[name] = value[name];
    }
  }
  if (value['os.features'] !== undefined) {
    if (!Array.isArray(value['os.features']) || value['os.features'].some(feature =>
      typeof feature !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(feature))) {
      throw new Error('Build metadata contains invalid OS features.');
    }
    result['os.features'] = [...new Set(value['os.features'])].sort();
  }
  if (result.os === 'windows' && !result['os.version']) {
    throw new Error('Windows image metadata must include os.version from a compatible base image.');
  }
  return result;
}

function descriptor(value) {
  if (!value || !DIGEST.test(value.digest)) {
    throw new Error('Build metadata contains an invalid image digest.');
  }
  return { digest: value.digest, platform: platformInfo(value.platform) };
}

function manifestDescriptors(value) {
  const entries = Array.isArray(value) ? value : [value];
  if (!entries.length) throw new Error('The registry returned no image manifests.');
  return entries.map(entry => descriptor(entry?.Descriptor || {
    digest: entry?.Digest, platform: entry?.Platform,
  }));
}

function validateCoverage(manifests, expected) {
  const found = manifests.map(item => `${item.platform.os}/${item.platform.architecture}`);
  if (new Set(found).size !== found.length || new Set(expected).size !== expected.length) {
    throw new Error('Build results contain duplicate platforms.');
  }
  if (found.length !== expected.length || expected.some(platform => !found.includes(platform))) {
    throw new Error(`Build results must cover exactly these platforms: ${expected.join(', ')}.`);
  }
}

function manifestEnvironment(config, env = process.env) {
  const directory = path.join(config.workDirectory, 'docker-config');
  fs.mkdirSync(directory, { mode: 0o700 });
  const source = path.join(env.DOCKER_CONFIG || path.join(os.homedir(), '.docker'), 'config.json');
  const original = fs.existsSync(source) ? parseJSON(fs.readFileSync(source, 'utf8'), 'Docker configuration') : {};
  const credentials = {};
  for (const key of ['auths', 'credsStore', 'credHelpers', 'HttpHeaders']) {
    if (original[key] !== undefined) credentials[key] = original[key];
  }
  fs.writeFileSync(path.join(directory, 'config.json'), JSON.stringify(credentials), { mode: 0o600 });
  return { DOCKER_CONFIG: directory, DOCKER_CONTEXT: 'default' };
}

async function inspectManifests(config, digest, exec, env) {
  const text = await exec('docker', [
    'manifest', 'inspect', '--verbose', `${config.image}@${digest}`,
  ], { env });
  return manifestDescriptors(parseJSON(text, 'Registry manifest'));
}

async function publishManifests(config, manifests, exec, env) {
  const sources = manifests.map(item => `${config.image}@${item.digest}`);
  const tags = config.tags.map(tag => `${config.image}:${tag}`);
  for (const tag of tags) {
    await exec('docker', ['manifest', 'create', tag, ...sources], { env });
    for (let index = 0; index < manifests.length; index += 1) {
      const platform = manifests[index].platform;
      const args = ['manifest', 'annotate', tag, sources[index],
        `--os=${platform.os}`, `--arch=${platform.architecture}`];
      if (platform.variant) args.push(`--variant=${platform.variant}`);
      if (platform['os.version']) args.push(`--os-version=${platform['os.version']}`);
      if (platform['os.features']?.length) {
        args.push(`--os-features=${platform['os.features'].join(',')}`);
      }
      await exec('docker', args, { env });
    }
  }
  let digest = '';
  for (const tag of tags) {
    const output = await exec('docker', ['manifest', 'push', '--purge', tag], { env });
    const current = output.trim().split(/\r?\n/).at(-1);
    if (!DIGEST.test(current)) throw new Error('Docker did not return a final manifest-list digest.');
    if (digest && current !== digest) {
      throw new Error('Final tags unexpectedly resolved to different manifest-list digests.');
    }
    digest = current;
  }
  return digest;
}

function readBuildRecords(config) {
  const files = [];
  function collect(filename) {
    const stat = fs.lstatSync(filename);
    if (stat.isSymbolicLink()) throw new Error('build-records must not contain symbolic links.');
    if (stat.isDirectory()) {
      for (const entry of fs.readdirSync(filename).sort()) collect(path.join(filename, entry));
    } else if (stat.isFile() && filename.endsWith('.json')) {
      if (stat.size > 1024 * 1024) throw new Error('Each build record must be smaller than 1 MiB.');
      files.push(filename);
    }
  }
  collect(path.resolve(config.workspace, config.records));
  if (!files.length || files.length > PLATFORMS.size) {
    throw new Error('build-records must contain between one and four JSON result files.');
  }
  const records = files.map(filename => {
    const record = parseJSON(fs.readFileSync(filename, 'utf8'), 'Build record');
    if (!record || record.schema !== 1 || record.published !== true ||
        record.image !== config.image || record.targetSha !== config.sha ||
        !DIGEST.test(record.digest) || !Array.isArray(record.platforms) ||
        !Array.isArray(record.manifests) || !record.manifests.length) {
      throw new Error('Build records must be published schema-1 results for the same image and target-sha.');
    }
    if (record.platforms.some(platform => !PLATFORMS.has(platform))) {
      throw new Error('A build record declares an unsupported platform.');
    }
    record.manifests = record.manifests.map(descriptor);
    validateCoverage(record.manifests, record.platforms);
    const prefix = `${config.image}:`;
    if (typeof record.stagingTag !== 'string' || !record.stagingTag.startsWith(prefix) ||
        !TAG.test(record.stagingTag.slice(prefix.length))) {
      throw new Error('A build record contains an invalid staging tag.');
    }
    return record;
  });
  validateCoverage(records.flatMap(record => record.manifests), config.platforms);
  return records;
}

async function build(config, exec) {
  if (!config.sourceVerified) throw new Error('Source checkout must be verified before building.');
  const label = config.platforms.join('-').replaceAll('/', '-');
  const stagingTag = `${config.image}:dockerbp-${config.sha}-${label}-${crypto.randomBytes(8).toString('hex')}`;
  let digest = '';
  if (config.backend === 'linux') {
    const metadata = path.join(config.workDirectory, 'build-metadata.json');
    await exec('docker', [
      'buildx', 'build', '--platform', config.platforms.join(','),
      '--file', config.dockerfilePath, '--metadata-file', metadata,
      '--provenance=false', '--sbom=false', '--tag', stagingTag,
      '--output', config.publish ? 'type=image,push=true,oci-mediatypes=false' : 'type=cacheonly',
      config.contextDirectory,
    ], { stream: true });
    if (config.publish) {
      digest = parseJSON(fs.readFileSync(metadata, 'utf8'), 'Buildx metadata')['containerimage.digest'];
    }
  } else {
    let built = false;
    try {
      await exec('docker', [
        'image', 'build', '--platform', config.platforms[0],
        '--file', config.dockerfilePath, '--tag', stagingTag, config.contextDirectory,
      ], { stream: true, env: { DOCKER_BUILDKIT: '0' } });
      built = true;
      if (config.publish) {
        await exec('docker', ['image', 'push', stagingTag], { stream: true });
        const values = parseJSON(await exec('docker', [
          'image', 'inspect', '--format', '{{json .RepoDigests}}', stagingTag,
        ]), 'Windows image digests');
        const reference = Array.isArray(values) && values.find(value => {
          if (typeof value !== 'string' || !value.includes('@')) return false;
          try {
            return imageName(value.split('@')[0]) === config.image;
          } catch {
            return false;
          }
        });
        digest = reference ? reference.split('@')[1] : '';
      }
    } finally {
      if (built) await exec('docker', ['image', 'rm', '--no-prune', stagingTag]);
    }
  }
  if (config.publish && !DIGEST.test(digest)) {
    throw new Error('The build did not return a published image digest.');
  }
  return { digest, stagingTag: config.publish ? stagingTag : '' };
}

async function run(config, exec = execute, env = process.env) {
  let digest = '';
  let recordFile = '';
  let stagingTags = [];
  let status = 'built';
  if (config.mode === 'merge') {
    const records = readBuildRecords(config);
    const registryEnv = manifestEnvironment(config, env);
    for (const record of records) {
      const actual = await inspectManifests(config, record.digest, exec, registryEnv);
      validateCoverage(actual, record.platforms);
      const sorted = values => values.map(item => JSON.stringify(item)).sort();
      if (JSON.stringify(sorted(actual)) !== JSON.stringify(sorted(record.manifests))) {
        throw new Error('Build record metadata does not match its immutable registry manifests.');
      }
    }
    digest = await publishManifests(config, records.flatMap(record => record.manifests), exec, registryEnv);
    stagingTags = records.map(record => record.stagingTag);
    status = 'published';
  } else {
    const result = await build(config, exec);
    let manifests = [];
    let registryEnv;
    if (config.publish) {
      registryEnv = manifestEnvironment(config, env);
      manifests = await inspectManifests(config, result.digest, exec, registryEnv);
      validateCoverage(manifests, config.platforms);
      stagingTags = [result.stagingTag];
    }
    const record = {
      schema: 1, image: config.image, targetSha: config.sha, published: config.publish,
      platforms: config.platforms, digest: result.digest, stagingTag: result.stagingTag, manifests,
    };
    recordFile = config.recordFile;
    fs.writeFileSync(recordFile, `${JSON.stringify(record, null, 2)}\n`, { mode: 0o600 });
    digest = result.digest;
    if (config.publish) {
      status = 'shard-published';
      if (config.mode === 'build') {
        digest = await publishManifests(config, manifests, exec, registryEnv);
        status = 'published';
      }
    }
  }
  return {
    status,
    image: config.image,
    'target-sha': config.sha,
    tags: config.tags.map(tag => `${config.image}:${tag}`).join('\n'),
    digest,
    'record-file': recordFile,
    'staging-tags': stagingTags.join('\n'),
  };
}

function cleanup(config) {
  if (config.sourceDirectory && (path.dirname(config.sourceDirectory) !== config.workspace ||
      !path.basename(config.sourceDirectory).startsWith('.dockerbp-source-'))) {
    throw new Error('Refusing to remove an unexpected source checkout path.');
  }
  if (path.basename(config.workDirectory) !== 'work' ||
      !path.basename(path.dirname(config.workDirectory)).startsWith('dockerbp-')) {
    throw new Error('Refusing to remove an unexpected temporary configuration path.');
  }
  fs.rmSync(config.workDirectory, { recursive: true, force: true, maxRetries: 5 });
  if (config.sourceDirectory) {
    fs.rmSync(config.sourceDirectory, { recursive: true, force: true, maxRetries: 5 });
  }
}

async function main() {
  const command = process.argv[2];
  if (command === 'prepare') {
    await prepare();
    return;
  }
  if (!process.env.DOCKERBP_CONFIGURATION) throw new Error('Missing private dockerbp configuration.');
  const config = parseJSON(fs.readFileSync(process.env.DOCKERBP_CONFIGURATION, 'utf8'), 'Private configuration');
  if (command === 'verify-source') {
    await verifySource(config);
    fs.writeFileSync(process.env.DOCKERBP_CONFIGURATION, JSON.stringify(config), { mode: 0o600 });
  } else if (command === 'run') {
    writeOutputs(await run(config));
  } else if (command === 'cleanup') {
    cleanup(config);
  } else {
    throw new Error('Unknown dockerbp command.');
  }
}

module.exports = {
  normalizeInputs, prepare, verifySource, run, cleanup, execute,
  manifestDescriptors, readBuildRecords, publishManifests, writeOutputs,
};

if (require.main === module) {
  main().catch(error => {
    const message = error.message.replaceAll('%', '%25').replaceAll('\r', '%0D').replaceAll('\n', '%0A');
    process.stderr.write(`::error::${message}\n`);
    process.exitCode = 1;
  });
}

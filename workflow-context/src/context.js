const PR_EVENTS = new Set([
  'pull_request',
  'pull_request_target',
  'pull_request_review',
  'pull_request_review_comment',
  'pull_request_review_thread',
]);

function object(value, field) {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${field} must be a JSON object.`);
  }
  return value;
}

function text(value, field) {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'string') throw new Error(`${field} must be a string.`);
  return value;
}

function flag(value, field) {
  if (value === undefined || value === null) return null;
  if (typeof value !== 'boolean') throw new Error(`${field} must be a boolean.`);
  return value;
}

function number(value) {
  if (value === undefined || value === null) return null;
  if (typeof value === 'number' && Number.isSafeInteger(value) && value > 0) {
    return String(value);
  }
  if (typeof value === 'string' && /^[1-9][0-9]*$/.test(value)) return value;
  throw new Error('PR number must be a positive integer.');
}

function labels(value) {
  if (value === undefined || value === null) return null;
  if (!Array.isArray(value)) throw new Error('PR labels must be an array.');
  return value.map((label) => {
    const name = typeof label === 'string' ? label : object(label, 'PR label')?.name;
    if (typeof name !== 'string' || !name) {
      throw new Error('Each PR label must have a nonempty name.');
    }
    return name;
  });
}

function merged(pr, marker) {
  const value = flag(pr?.merged, 'PR merged');
  if (value !== null) return value;
  const source = pr && Object.hasOwn(pr, 'merged_at') ? pr : marker;
  if (!source || !Object.hasOwn(source, 'merged_at')) return null;
  if (source.merged_at === null) return false;
  if (!text(source.merged_at, 'PR merged_at')) {
    throw new Error('PR merged_at must be a nonempty timestamp or null.');
  }
  return true;
}

export function environment(env, name, required = false) {
  const value = env[name];
  if (value === undefined || value === '') {
    if (required) throw new Error(`Missing required environment variable ${name}.`);
    return '';
  }
  if (typeof value !== 'string') throw new Error(`${name} must be a string.`);
  return value;
}

export function parseJsonObject(source, description) {
  let result;
  try {
    result = JSON.parse(source);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new Error(`${description} must contain valid JSON.`);
  }
  if (!object(result, description)) {
    throw new Error(`${description} must be a JSON object.`);
  }
  return result;
}

async function fetchPullRequest(env, repository, prNumber, token, fetcher) {
  const url = new URL(environment(env, 'GITHUB_API_URL', true));
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) {
    throw new Error('GITHUB_API_URL must be an HTTPS API URL without credentials, query, or fragment.');
  }
  const [owner, repo] = repository.split('/');
  url.pathname = `${url.pathname.replace(/\/+$/, '')}/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/pulls/${prNumber}`;
  const response = await fetcher(url.href, {
    method: 'GET',
    headers: {
      accept: 'application/vnd.github+json',
      authorization: `Bearer ${token}`,
      'x-github-api-version': '2022-11-28',
      'user-agent': 'btnguyen2k-gh-actions-workflow-context',
    },
    redirect: 'error',
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) {
    throw new Error(
      `PR lookup failed with HTTP ${response.status}. Check github-token access and pull-requests: read permission.`,
    );
  }
  const pr = parseJsonObject(await response.text(), 'GitHub PR API response');
  if (number(pr.number) !== prNumber) {
    throw new Error('GitHub PR API response did not identify the requested PR.');
  }
  return pr;
}

export async function captureContext({
  env,
  payload,
  targetSha = '',
  token = '',
  fetcher = globalThis.fetch,
}) {
  if (!object(payload, 'GitHub event payload')) {
    throw new Error('GitHub event payload must be a JSON object.');
  }
  if (typeof targetSha !== 'string' || typeof token !== 'string') {
    throw new Error('target-sha and github-token must be strings.');
  }
  const apiToken = token.trim();
  if ((token && !apiToken) || /[\r\n]/.test(apiToken)) {
    throw new Error('github-token must be a nonempty, single-line token when supplied.');
  }
  const eventName = environment(env, 'GITHUB_EVENT_NAME', true);
  const eventSha = environment(env, 'GITHUB_SHA', true);
  const eventAction = text(payload.action, 'Event action') ?? '';
  const repository = environment(env, 'GITHUB_REPOSITORY', true);
  const parts = repository.split('/');
  if (parts.length !== 2 || parts.some((part) => !part || part === '.' || part === '..')) {
    throw new Error('GITHUB_REPOSITORY must be in owner/name form.');
  }
  const server = environment(env, 'GITHUB_SERVER_URL', true).replace(/\/+$/, '');
  const ref = environment(env, 'GITHUB_REF');
  const prRef = /^refs\/pull\/([1-9][0-9]*)\/(?:merge|head)$/.exec(ref);
  const eventPr = object(payload.pull_request, 'pull_request');
  const issue = object(payload.issue, 'issue');
  const marker = object(issue?.pull_request, 'issue.pull_request');
  const isPr = Boolean(eventPr || marker || PR_EVENTS.has(eventName) || prRef);
  const numbers = [
    eventPr?.number,
    eventPr || PR_EVENTS.has(eventName) ? payload.number : null,
    marker ? issue.number : null,
    prRef?.[1],
  ].map(number).filter((value) => value !== null);
  if (new Set(numbers).size > 1) throw new Error('Event contains conflicting PR numbers.');
  const prNumber = numbers[0] ?? null;
  let pr = eventPr ?? (marker ? issue : null);
  let source = isPr ? 'event' : 'none';
  if (isPr && apiToken) {
    if (!prNumber) throw new Error('Cannot fetch PR details: the event does not identify a PR number.');
    pr = await fetchPullRequest(env, repository, prNumber, apiToken, fetcher);
    source = 'api';
  }
  const head = object(pr?.head, 'PR head');
  const base = object(pr?.base, 'PR base');
  const headRepo = text(object(head?.repo, 'PR head repository')?.full_name, 'PR head repository name');
  const baseRepo = text(object(base?.repo, 'PR base repository')?.full_name, 'PR base repository name');
  const eventMarker = source === 'event' ? marker : null;
  const details = {
    'pr-number': prNumber,
    'pr-url': text(pr?.html_url, 'PR URL') ?? text(eventMarker?.html_url, 'PR URL')
      ?? (prNumber ? `${server}/${repository}/pull/${prNumber}` : null),
    'pr-title': text(pr?.title, 'PR title'),
    'pr-state': text(pr?.state, 'PR state'),
    'pr-author': text(object(pr?.user, 'PR author')?.login, 'PR author login'),
    'pr-draft': flag(pr?.draft, 'PR draft'),
    'pr-merged': merged(pr, eventMarker),
    'pr-is-fork': headRepo && baseRepo ? headRepo.toLowerCase() !== baseRepo.toLowerCase() : null,
    'pr-head-ref': text(head?.ref, 'PR head ref')
      ?? (isPr && source === 'event' ? environment(env, 'GITHUB_HEAD_REF') || null : null),
    'pr-head-sha': text(head?.sha, 'PR head SHA'),
    'pr-head-repository': headRepo,
    'pr-base-ref': text(base?.ref, 'PR base ref')
      ?? (isPr && source === 'event' ? environment(env, 'GITHUB_BASE_REF') || null : null),
    'pr-base-sha': text(base?.sha, 'PR base SHA'),
    'pr-base-repository': baseRepo,
    'pr-labels': labels(pr?.labels),
  };
  const complete = Object.values(details).every(
    (value) => value !== null && (typeof value !== 'string' || value !== ''),
  );
  const branch = ref.startsWith('refs/heads/') ? ref.slice('refs/heads/'.length) : '';
  const tag = ref.startsWith('refs/tags/') ? ref.slice('refs/tags/'.length) : '';
  const shortRef = /^refs\/(?:heads|tags|pull)\/(.*)$/.exec(ref)?.[1] ?? '';
  const runId = environment(env, 'GITHUB_RUN_ID');
  return {
    'event-name': eventName,
    'event-action': eventAction,
    'event-sha': eventSha,
    'target-sha': targetSha || eventSha,
    'action-target-branch': isPr ? details['pr-head-ref'] ?? '' : branch,
    ref,
    'ref-name': environment(env, 'GITHUB_REF_NAME') || shortRef,
    'ref-type': environment(env, 'GITHUB_REF_TYPE') || (tag ? 'tag' : branch || prRef ? 'branch' : ''),
    tag,
    repository,
    'repository-owner': parts[0],
    actor: environment(env, 'GITHUB_ACTOR'),
    'triggering-actor': environment(env, 'GITHUB_TRIGGERING_ACTOR'),
    workflow: environment(env, 'GITHUB_WORKFLOW'),
    'run-id': runId,
    'run-number': environment(env, 'GITHUB_RUN_NUMBER'),
    'run-attempt': environment(env, 'GITHUB_RUN_ATTEMPT'),
    'run-url': runId ? `${server}/${repository}/actions/runs/${runId}` : '',
    'is-pr': isPr,
    'pr-details-status': isPr ? complete ? 'full' : 'partial' : 'none',
    'pr-details-source': source,
    ...details,
  };
}

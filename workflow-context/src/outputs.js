import { randomUUID } from 'node:crypto';
import { EOL } from 'node:os';

export function escapeWorkflowValue(value) {
  return String(value).replaceAll('%', '%25').replaceAll('\r', '%0D').replaceAll('\n', '%0A');
}

export function serializeOutputs(context, createId = randomUUID) {
  const values = { ...context, context: JSON.stringify(context) };
  return Object.entries(values).map(([name, value]) => {
    if (!/^[a-z][a-z0-9-]*$/.test(name)) throw new Error('Invalid output name.');
    const data = value === null ? '' : typeof value === 'string' ? value : JSON.stringify(value);
    if (typeof data !== 'string') throw new Error(`Invalid value for output ${name}.`);
    let delimiter;
    do {
      delimiter = `gh_${createId()}`;
      if (!/^[a-zA-Z0-9_-]+$/.test(delimiter)) throw new Error('Invalid output delimiter.');
    } while (data.includes(delimiter));
    // Native separators preserve trailing CR characters in the runner's parser.
    return `${name}<<${delimiter}${EOL}${data}${EOL}${delimiter}${EOL}`;
  }).join('');
}

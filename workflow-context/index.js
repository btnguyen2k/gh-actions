import { appendFile, readFile, stat } from 'node:fs/promises';
import { captureContext, environment, parseJsonObject } from './src/context.js';
import { escapeWorkflowValue, serializeOutputs } from './src/outputs.js';

try {
  const token = environment(process.env, 'INPUT_GITHUB-TOKEN');
  if (token.trim()) {
    process.stdout.write(`::add-mask::${escapeWorkflowValue(token.trim())}\n`);
  }
  const outputPath = environment(process.env, 'GITHUB_OUTPUT', true);
  if (!(await stat(outputPath)).isFile()) {
    throw new Error('GITHUB_OUTPUT must point to a file.');
  }
  const eventPath = environment(process.env, 'GITHUB_EVENT_PATH', true);
  const payload = parseJsonObject(
    await readFile(eventPath, 'utf8'),
    'GitHub event payload',
  );
  const context = await captureContext({
    env: process.env,
    payload,
    targetSha: environment(process.env, 'INPUT_TARGET-SHA'),
    token,
  });
  await appendFile(outputPath, serializeOutputs(context), 'utf8');
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`::error::workflow-context: ${escapeWorkflowValue(message)}\n`);
  process.exitCode = 1;
}

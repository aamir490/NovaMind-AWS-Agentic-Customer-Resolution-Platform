import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../frontend/src/main.jsx', import.meta.url), 'utf8');

test('landing copy uses the approved AWS/Strands positioning', () => {
  assert.match(source, /AWS-POWERED AGENTIC AI/);
  assert.match(source, /Every case, understood\./);
  assert.match(source, /Intelligent customer resolution powered by Amazon Bedrock, Strands Agents, and human oversight\./);
  assert.match(source, /EVIDENCE-DRIVEN.*POLICY-GROUNDED.*HUMAN-APPROVED/s);
});

test('Admin and Customer login copy matches their implemented responsibilities', () => {
  assert.match(source, /Oversee every resolution\./);
  assert.match(source, /AI-assisted investigation, policy checks, and human approval/);
  assert.match(source, /Your resolution starts here\./);
  assert.match(source, /Report an issue, follow its progress, and track your resolution updates/);
  assert.match(source, /beginCognitoLogin|finishCognitoLogin/);
});

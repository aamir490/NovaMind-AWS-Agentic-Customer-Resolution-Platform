import test from 'node:test';
import assert from 'node:assert/strict';
import {
  architectureStatus,
  architectureViews,
  architectureViewIds,
  buildRecordedLiveGraph,
  validateArchitectureGraph,
} from '../frontend/src/architectureGraphModern.js';
import { readFile } from 'node:fs/promises';

const edgeKey = (graph, from, to) => graph.edges.some((edge) => edge.from === from && edge.to === to);

test('the four architecture tabs validate and expose the current service boundaries', () => {
  assert.deepEqual(architectureViewIds, ['agent', 'aws', 'network', 'deployment']);
  for (const id of architectureViewIds) {
    assert.equal(validateArchitectureGraph(architectureViews[id]), true, id);
  }
  const agent = architectureViews.agent;
  assert.equal(edgeKey(agent, 'outbox', 'publisher'), true);
  assert.equal(edgeKey(agent, 'publisher', 'sqs'), true);
  assert.equal(edgeKey(agent, 'sqs', 'worker'), true);
  assert.equal(edgeKey(agent, 'worker', 'tools'), true);
  assert.equal(edgeKey(agent, 'policy', 'postgres'), true);
  assert.equal(edgeKey(agent, 'review', 'outcome'), true);
  assert.equal(edgeKey(agent, 'dispatcher', 'langgraph'), false);
  assert.equal(agent.nodes.find((item) => item.id === 'langgraph').status, 'legacy');
});

test('AWS, network and deployment views label unverified runtime claims explicitly', () => {
  assert.equal(architectureViews.aws.nodes.find((item) => item.id === 'api').status, 'deployed');
  assert.equal(architectureViews.aws.nodes.find((item) => item.id === 'publisher').status, 'planned');
  assert.equal(architectureViews.aws.nodes.find((item) => item.id === 'worker').status, 'planned');
  assert.equal(architectureViews.network.nodes.find((item) => item.id === 'egress').status, 'unavailable');
  assert.equal(architectureViews.deployment.nodes.find((item) => item.id === 'image').detail.includes('sha256'), true);
  assert.equal(architectureStatus.legacy.label, 'Legacy compatibility path');
});

test('recorded graph remains read-only and distinguishes completed records from review', () => {
  const graph = buildRecordedLiveGraph(
    [{ workflow_status: 'COMPLETED' }, { workflow_status: 'RECOVERY_REQUIRED' }],
    [{ operation: 'tool' }, { operation: 'workflow' }],
  );
  assert.equal(validateArchitectureGraph(graph), true);
  assert.match(graph.nodes.find((item) => item.id === 'recorded-outcome').detail, /1 completed/);
  assert.equal(edgeKey(graph, 'recorded-outcome', 'recorded-review'), true);
});

test('Architecture Explorer is administrator-only and advertises no execution capability', async () => {
  const source = await readFile(new URL('../frontend/src/ArchitectureExplorer.jsx', import.meta.url), 'utf8');
  assert.match(source, /identity\?\.role !== 'ADMIN'/);
  assert.match(source, /No AWS calls · no execution/);
  assert.doesNotMatch(source, /startCaseInvestigation|invoke|bedrock/i);
});

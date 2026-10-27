import test from 'node:test';
import assert from 'node:assert/strict';
import { clampTourStep, productTourServices, productTourSteps } from '../frontend/src/productTour.js';

test('public product tour contains four synthetic, non-executing stages', () => {
  assert.deepEqual(productTourSteps.map((step) => step.id), ['complaint', 'investigation', 'review', 'response']);
  assert.equal(productTourSteps.length, 4);
  for (const step of productTourSteps) {
    assert.ok(step.preview);
    assert.ok(step.description);
    assert.match(`${step.preview} ${step.description}`, /synthetic|customer|recommendation|review|response/i);
  }
});

test('tour navigation is bounded and architecture list uses named AWS services', () => {
  assert.equal(clampTourStep(-10), 0);
  assert.equal(clampTourStep(99), 3);
  assert.ok(productTourServices.some((service) => service.name === 'Amazon Bedrock'));
  assert.ok(productTourServices.some((service) => service.name === 'Amazon ECS Fargate'));
  assert.ok(productTourServices.some((service) => service.name === 'Amazon Cognito'));
  assert.ok(productTourServices.length <= 12);
});

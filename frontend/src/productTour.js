export const productTourSteps = Object.freeze([
  Object.freeze({ id: 'complaint', number: '01', label: 'Customer Complaint', tone: 'cyan', title: 'Start with the customer’s story.', description: 'A customer describes an issue in plain language and shares the details needed to begin a careful review.', previewLabel: 'Synthetic example', preview: '“My laptop arrived with a damaged screen. What can you do to help?”', outcome: 'A case is created with a clear request and an evidence trail.' }),
  Object.freeze({ id: 'investigation', number: '02', label: 'AI Investigation', tone: 'purple', title: 'Evidence and policy come together.', description: 'A bounded workflow checks case facts, order context and policy knowledge before preparing a grounded recommendation.', previewLabel: 'Synthetic activity', preview: 'Case facts  ·  Order evidence  ·  Policy citations  ·  Structured recommendation', outcome: 'The AI output is attributed as validated model output or a deterministic fallback.' }),
  Object.freeze({ id: 'review', number: '03', label: 'Admin Review', tone: 'cyan', title: 'A person stays in control.', description: 'An authorized reviewer sees the evidence and recommendation, can edit the response, and records an explicit decision.', previewLabel: 'Human oversight', preview: 'Recommendation: Request additional evidence  ·  Actions: None', outcome: 'Review is durable and approval never silently executes a refund or replacement.' }),
  Object.freeze({ id: 'response', number: '04', label: 'Customer Response', tone: 'purple', title: 'The customer receives a clear next step.', description: 'Only the approved customer-facing response is shown to the customer, with internal reasoning and tool traces kept private.', previewLabel: 'Synthetic response', preview: '“Thanks for the details. Our support team needs one more photo before deciding the next step.”', outcome: 'The case lifecycle reflects the recorded review without claiming an unapproved business action.' }),
]);

export const productTourServices = Object.freeze([
  Object.freeze({ name: 'CloudFront', detail: 'Frontend delivery' }),
  Object.freeze({ name: 'Amazon S3', detail: 'Static frontend assets' }),
  Object.freeze({ name: 'Amazon Cognito', detail: 'Portal authentication' }),
  Object.freeze({ name: 'Application Load Balancer', detail: 'API entry point' }),
  Object.freeze({ name: 'Amazon ECS Fargate', detail: 'Backend runtime' }),
  Object.freeze({ name: 'FastAPI + LangGraph', detail: 'Workflow orchestration' }),
  Object.freeze({ name: 'Amazon Bedrock', detail: 'Optional grounded model provider' }),
  Object.freeze({ name: 'RDS PostgreSQL', detail: 'Durable production persistence' }),
  Object.freeze({ name: 'CloudWatch', detail: 'Operational telemetry' }),
]);

export function clampTourStep(value) {
  return Math.max(0, Math.min(productTourSteps.length - 1, value));
}

const statuses = Object.freeze({
  verified: { label: 'Source verified', tone: 'verified' },
  deployed: { label: 'Previously deployed; not live-queried here', tone: 'deployed' },
  planned: { label: 'Planned / not deployment-verified', tone: 'planned' },
  unavailable: { label: 'Not verified in this local context', tone: 'unavailable' },
  recorded: { label: 'Recorded locally', tone: 'recorded' },
  legacy: { label: 'Legacy compatibility path', tone: 'legacy' },
});

const node = (id, label, group, status, x, y, detail, evidence, kind = 'service') =>
  Object.freeze({ id, label, group, status, x, y, detail, evidence, kind });
const edge = (from, to, label) => Object.freeze({ from, to, label, direction: 'forward' });

const source = 'Repository source, tests and deployment contracts; this explorer makes no AWS or model calls.';
const deployed = 'Previously verified deployment evidence; this read-only explorer does not live-query AWS.';
const planned = 'Implemented in source/templates but not live-verified by this browser view.';
const legacy = 'Retained for regression comparison and compatibility; not the Strands production target.';
const boundary = 'Deterministic server boundary; never delegated to the model or browser.';

const views = {
  agent: {
    id: 'agent', title: 'Agent Graph',
    subtitle: 'Customer request → durable job → bounded Strands investigation → policy-safe Admin review.',
    nodes: [
      node('customer', 'Customer portal', 'intake', 'deployed', 90, 150, 'Authenticated Customer submits a case through the API. Ownership comes from the Cognito subject binding, never from a browser field.', deployed, 'input'),
      node('api', 'FastAPI API', 'intake', 'deployed', 280, 150, 'Cognito Admin/Customer authorization, ownership checks, idempotency and read-only status projections.', deployed),
      node('postgres', 'PostgreSQL state', 'durability', 'deployed', 470, 150, 'System of record for cases, jobs, leases, results, proposals, reviews and audit events.', deployed),
      node('outbox', 'Transactional outbox', 'durability', 'deployed', 470, 330, 'Case/job registration and one dispatch event commit atomically; no queue send occurs inside the database transaction.', deployed),
      node('publisher', 'Outbox publisher', 'queue', 'planned', 660, 330, 'Independent ECS process claims and fences outbox rows, publishes reference-only SQS messages, and retries safely.', planned),
      node('sqs', 'SQS dispatch queue', 'queue', 'deployed', 850, 330, 'Carries versioned references: job_id, outbox_id, event_type and schema_version. Payloads contain no credentials or prompts.', deployed),
      node('worker', 'Strands worker', 'agent', 'planned', 1040, 330, 'Independent worker identity claims a PostgreSQL lease, runs once, and persists the result before acknowledging SQS.', planned),
      node('tools', 'Read-only tools', 'agent', 'verified', 850, 510, 'Bounded evidence retrieval: customer/order/case, knowledge, policy and proposal-status reads. No refund or mutable action tool.', source),
      node('nova', 'Bedrock Nova Lite', 'agent', 'deployed', 1040, 510, 'Strands model provider with the Nova Lite allowlist and a maximum of three model calls per investigation.', deployed),
      node('policy', 'Deterministic policy', 'policy', 'verified', 660, 510, 'Validates structured output, evidence grounding, issue policy and safe error states outside the agent.', source),
      node('review', 'Admin HITL review', 'review', 'deployed', 850, 150, 'Admin approves or rejects the durable proposal with binding checks, idempotency and audit. No agent resume or refund execution.', deployed),
      node('outcome', 'Case outcome', 'review', 'verified', 1040, 150, 'Investigation COMPLETED is distinct from final case RESOLVED; actions_executed remains false.', boundary),
      node('langgraph', 'LangGraph path', 'legacy', 'legacy', 280, 510, 'Legacy graph/checkpoint runtime remains in the repository for regression comparison and controlled compatibility, but is not the target queue worker.', legacy),
    ],
    edges: [
      edge('customer', 'api', 'case request'), edge('api', 'postgres', 'authorize + persist'),
      edge('postgres', 'outbox', 'same transaction'), edge('outbox', 'publisher', 'claim + fence'),
      edge('publisher', 'sqs', 'publish reference'), edge('sqs', 'worker', 'deliver'),
      edge('worker', 'postgres', 'claim / persist'), edge('worker', 'tools', 'bounded reads'),
      edge('tools', 'nova', 'grounded context'), edge('nova', 'policy', 'structured result'),
      edge('policy', 'postgres', 'result + proposal'), edge('postgres', 'review', 'pending review'),
      edge('review', 'outcome', 'decision'), edge('langgraph', 'postgres', 'legacy checkpoints'),
    ],
  },
  aws: {
    id: 'aws', title: 'AWS Architecture',
    subtitle: 'The deployed dev baseline and the Strands/SQS services defined by the reviewed templates.',
    nodes: [
      node('cloudfront', 'CloudFront', 'edge', 'deployed', 90, 120, 'Default HTTPS frontend distribution; routes browser assets and /api traffic.', deployed),
      node('s3', 'Private S3 + OAC', 'edge', 'deployed', 280, 120, 'Private frontend bucket accessed through CloudFront Origin Access Control.', deployed),
      node('cognito', 'Cognito', 'identity', 'deployed', 470, 120, 'Public SPA client with authorization-code + PKCE, Admin/Customer groups and required software-token MFA.', deployed),
      node('vpcOrigin', 'CloudFront VPC Origin', 'edge', 'deployed', 660, 120, 'Existing VPC Origin terminates at the internal ALB over the reviewed development HTTP path.', deployed),
      node('alb', 'Internal ALB', 'network', 'deployed', 850, 120, 'Internal listener forwards only to the API target group; no public ALB origin is used.', deployed),
      node('api', 'ECS API service', 'compute', 'deployed', 1040, 120, 'FastAPI API task definition uses the immutable backend image and project-only secret references.', deployed),
      node('publisher', 'ECS outbox publisher', 'compute', 'planned', 280, 320, 'Separate process role and service for transactional-outbox publication.', planned),
      node('worker', 'ECS Strands worker', 'compute', 'planned', 470, 320, 'Separate process role and service for SQS delivery and fenced Strands execution.', planned),
      node('sqs', 'SQS + DLQ', 'messaging', 'deployed', 660, 320, 'Versioned reference queue with bounded retry and poison-message recovery policy.', deployed),
      node('rds', 'RDS PostgreSQL 17.11', 'data', 'deployed', 850, 320, 'Private database with migration version 13 verified during bootstrap; TLS uses the trusted AWS RDS CA bundle.', deployed),
      node('bedrock', 'Bedrock Nova Lite', 'ai', 'deployed', 1040, 320, 'Worker-only model provider; IAM is scoped to the approved inference profile/foundation models.', deployed),
      node('iam', 'IAM task roles', 'security', 'deployed', 280, 520, 'Execution and task roles are separated; ECR/log/secret/queue/model permissions are least scoped by role.', deployed),
      node('secrets', 'Secrets Manager', 'security', 'deployed', 470, 520, 'Project-specific PostgreSQL and Cognito configuration references; values are never shown in the explorer.', deployed),
      node('cloudwatch', 'CloudWatch Logs', 'observability', 'deployed', 660, 520, 'Separate API, publisher and worker log groups support bounded operational diagnostics.', deployed),
      node('legacy', 'LangGraph / SQLite', 'legacy', 'legacy', 850, 520, 'Repository compatibility and regression path only; excluded from the Strands AWS production target.', legacy),
    ],
    edges: [
      edge('s3', 'cloudfront', 'private assets'), edge('cloudfront', 'vpcOrigin', '/api + frontend'),
      edge('cognito', 'api', 'access token'), edge('vpcOrigin', 'alb', 'private origin'), edge('alb', 'api', 'target group'),
      edge('api', 'rds', 'TLS persistence'), edge('api', 'secrets', 'secret projection'), edge('api', 'cloudwatch', 'logs'),
      edge('publisher', 'sqs', 'publish'), edge('worker', 'sqs', 'consume'), edge('worker', 'rds', 'fenced state'),
      edge('worker', 'bedrock', 'bounded inference'), edge('iam', 'api', 'role boundary'), edge('iam', 'publisher', 'role boundary'),
      edge('iam', 'worker', 'role boundary'), edge('secrets', 'publisher', 'only required config'), edge('secrets', 'worker', 'RDS only'),
      edge('legacy', 'rds', 'comparison only'),
    ],
  },
  network: {
    id: 'network', title: 'Networking',
    subtitle: 'Browser edge, private origin, ECS workloads and private PostgreSQL boundaries.',
    nodes: [
      node('browser', 'Admin / Customer browser', 'edge', 'verified', 80, 300, 'HTTPS browser boundary. The explorer itself performs no network calls.', source),
      node('cf', 'CloudFront HTTPS', 'edge', 'deployed', 260, 300, 'Default CloudFront URL serves private S3 assets and the API path.', deployed),
      node('s3', 'Private S3 + OAC', 'edge', 'deployed', 260, 120, 'No public bucket access; CloudFront is the asset reader.', deployed),
      node('origin', 'VPC Origin', 'edge', 'deployed', 470, 300, 'CloudFront VPC Origin connects to the internal ALB on the reviewed dev HTTP origin path.', deployed),
      node('albsg', 'ALB security group', 'security', 'deployed', 660, 120, 'Ingress restricted to the CloudFront origin-facing prefix list.', deployed),
      node('alb', 'Internal ALB :80', 'network', 'deployed', 660, 300, 'Internal-only listener forwards to the API target group.', deployed),
      node('ecsSubnets', 'ECS subnets', 'subnet', 'deployed', 660, 500, 'Reviewed ECS subnet IDs are 172.31.96.0/24 and 172.31.97.0/24 in us-east-1a/b.', deployed),
      node('ecssg', 'API / workload SGs', 'security', 'deployed', 860, 120, 'API accepts ALB traffic; API/workload egress is explicitly limited to approved destinations.', deployed),
      node('api', 'API / publisher / worker', 'compute', 'deployed', 860, 300, 'Separate process roles share private subnet placement but not HTTP identity or task permissions.', deployed),
      node('rdsSG', 'RDS SG :5432', 'security', 'deployed', 860, 500, 'Ingress is limited to the API and workload security groups; no broad CIDR rule.', deployed),
      node('rds', 'Private RDS', 'data', 'deployed', 1060, 500, 'Private PostgreSQL endpoint with verified TLS CA and schema owned by novamind_app.', deployed),
      node('egress', 'Egress route / endpoints', 'network', 'unavailable', 470, 520, 'The explorer does not live-query current route-table/NAT/VPC-endpoint state. Treat egress as a deployment prerequisite, not a claim.', 'No live AWS network query is made by this frontend.'),
    ],
    edges: [
      edge('browser', 'cf', 'HTTPS'), edge('s3', 'cf', 'OAC assets'), edge('cf', 'origin', '/api/*'),
      edge('origin', 'albsg', 'prefix-list policy'), edge('origin', 'alb', 'private HTTP'), edge('alb', 'api', 'target group'),
      edge('ecsSubnets', 'api', 'awsvpc'), edge('ecssg', 'api', 'ingress/egress'), edge('api', 'rdsSG', 'TCP 5432'),
      edge('rdsSG', 'rds', 'allow'), edge('api', 'egress', 'AWS API path'), edge('egress', 'api', 'route prerequisite'),
    ],
  },
  deployment: {
    id: 'deployment', title: 'Deployment Pipeline',
    subtitle: 'Immutable image → reviewed infrastructure → independently scaled API, publisher and worker processes.',
    nodes: [
      node('source', 'Repository source', 'source', 'verified', 90, 140, 'Backend, frontend, migrations, templates and focused regression tests are reviewed together.', source),
      node('tests', 'Offline tests + build', 'build', 'verified', 280, 140, 'Frontend tests/build and mocked backend/Strands/Bedrock tests gate artifacts; live AWS/model calls are separate approvals.', source),
      node('image', 'Immutable backend image', 'build', 'deployed', 470, 140, 'Published ECR image is selected by sha256 digest, never by mutable tag.', deployed),
      node('ecr', 'ECR repository', 'build', 'deployed', 660, 140, 'Scan-on-push, immutable tags and project-specific repository.', deployed),
      node('cfn', 'CloudFormation review', 'infra', 'deployed', 280, 350, 'Network, RDS, Cognito, messaging, ECS services and frontend delivery are prepared as reviewable stacks/change sets.', deployed),
      node('network', 'Network + RDS', 'infra', 'deployed', 470, 350, 'VPC origin, private database, SG ingress and TLS bootstrap precede services.', deployed),
      node('identity', 'Cognito + config', 'infra', 'deployed', 660, 350, 'Admin/Customer-only groups, public PKCE SPA and fail-closed customer bindings.', deployed),
      node('services', 'ECS services', 'deploy', 'planned', 850, 350, 'API, publisher and Strands worker use separate task definitions/roles and bounded health/deployment settings.', planned),
      node('frontend', 'S3 + CloudFront', 'deploy', 'deployed', 1040, 350, 'Private frontend assets and /api routing through the CloudFront VPC Origin.', deployed),
      node('api', 'API · 2 tasks', 'runtime', 'deployed', 760, 570, 'FastAPI/Cognito/PostgreSQL service; no inline Bedrock execution in the production target.', deployed),
      node('publisher', 'Publisher · 1 task', 'runtime', 'planned', 920, 570, 'Claims outbox rows and publishes reference-only SQS messages.', planned),
      node('worker', 'Worker · 2 tasks', 'runtime', 'planned', 1080, 570, 'Consumes SQS, claims leases, runs Strands and persists before acknowledgement.', planned),
    ],
    edges: [
      edge('source', 'tests', 'validate'), edge('tests', 'image', 'build'), edge('image', 'ecr', 'push digest'),
      edge('cfn', 'network', 'foundation'), edge('cfn', 'identity', 'auth config'), edge('cfn', 'services', 'reviewed stack'),
      edge('ecr', 'services', 'immutable image'), edge('network', 'services', 'subnets / SG / RDS'), edge('identity', 'services', 'secret refs'),
      edge('services', 'api', 'deploy'), edge('services', 'publisher', 'deploy'), edge('services', 'worker', 'deploy'),
      edge('cfn', 'frontend', 'delivery'),
    ],
  },
};

export const architectureStatus = statuses;
export const architectureViews = Object.freeze(views);
export const architectureViewIds = Object.freeze(Object.keys(views));

export const requiredArchitectureNodeIds = Object.freeze([
  'customer', 'api', 'postgres', 'outbox', 'publisher', 'sqs', 'worker', 'tools', 'nova', 'policy', 'review', 'outcome', 'langgraph',
  'cloudfront', 's3', 'cognito', 'vpcOrigin', 'alb', 'rds', 'bedrock', 'iam', 'secrets', 'cloudwatch', 'legacy',
  'browser', 'cf', 'origin', 'albsg', 'ecsSubnets', 'ecssg', 'rdsSG', 'egress',
  'source', 'tests', 'image', 'ecr', 'cfn', 'network', 'identity', 'services', 'frontend',
]);

export function validateArchitectureGraph(graph) {
  if (!graph || !Array.isArray(graph.nodes) || !Array.isArray(graph.edges)) return false;
  const nodeIds = new Set(graph.nodes.map((item) => item.id));
  if (nodeIds.size !== graph.nodes.length) return false;
  if (graph.nodes.some((item) => !item.id || !item.label || !statuses[item.status]
      || !Number.isFinite(item.x) || !Number.isFinite(item.y))) return false;
  return graph.edges.every((item) => nodeIds.has(item.from) && nodeIds.has(item.to)
    && item.from !== item.to && item.direction === 'forward');
}

export function buildRecordedLiveGraph(investigations = [], events = []) {
  const completed = investigations.filter((item) => item.workflow_status === 'COMPLETED').length;
  const failed = investigations.filter((item) => ['FAILED', 'RECOVERY_REQUIRED'].includes(item.workflow_status)).length;
  const tools = events.filter((item) => item.operation === 'tool').length;
  return {
    id: 'live', title: 'Live Workflow', subtitle: 'Read-only summary of durable records; this never replays a workflow.',
    nodes: [
      node('recorded-runs', 'Recorded investigations', 'recorded', 'recorded', 140, 300, `${investigations.length} durable investigation record(s) loaded.`, 'Admin diagnostics endpoint.'),
      node('recorded-events', 'Recorded stages', 'recorded', 'recorded', 390, 300, `${events.length} diagnostic event(s) loaded.`, 'Admin diagnostics endpoint.'),
      node('recorded-tools', 'Tool activity', 'recorded', 'recorded', 640, 180, `${tools} tool event(s) recorded.`, 'Process metadata only; no live tool call.'),
      node('recorded-outcome', 'Recorded outcomes', 'recorded', 'recorded', 640, 420, `${completed} completed and ${failed} failed/recovery record(s).`, 'Durable workflow metadata only.'),
      node('recorded-review', 'Admin / HITL boundary', 'recorded', 'recorded', 900, 300, 'Review state is shown only when recorded by the API.', 'No proposal is approved or executed by this view.'),
    ],
    edges: [edge('recorded-runs', 'recorded-events', 'metadata'), edge('recorded-events', 'recorded-tools', 'tool events'), edge('recorded-events', 'recorded-outcome', 'workflow events'), edge('recorded-outcome', 'recorded-review', 'review metadata')],
  };
}

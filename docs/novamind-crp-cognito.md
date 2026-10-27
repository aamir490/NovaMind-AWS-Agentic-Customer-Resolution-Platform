# NovaMind CRP Cognito contract

The additive `novamind-cognito-components.template.json` stack is opt-in. Its
defaults create no Cognito resources. It can create, independently:

- one email-based Cognito User Pool with verified email recovery and required
  software-token MFA;
- the `customers` and `admins` groups;
- one public SPA client using authorization-code OAuth with PKCE (no client
  secret); and
- an AWS-managed Hosted UI domain.

The backend remains authoritative. `backend/app/security.py` accepts only a
verified Cognito access token, requires `token_use=access`, validates issuer
and client ID, maps exactly one configured group to `CUSTOMER`, `REVIEWER`, or
`ADMIN`, and resolves a customer identity only through the durable binding
lookup. A browser portal selection is presentation state and never grants a
role.

## MFA policy

Cognito User Pool MFA is pool-wide; it cannot require MFA for `admins` while
allowing it to be absent for `customers` in the same pool.
The reviewed dev policy therefore requires `SOFTWARE_TOKEN_MFA` for every
role. This gives privileged users strong protection without creating a second
pool or weakening the single-issuer backend contract. Users must complete TOTP
setup before they can authenticate. If customer usability later requires
optional MFA, that is a separate reviewed change: the backend must first
validate an MFA assurance claim for privileged roles, because group membership
alone is not evidence of MFA.

## API configuration secret

Set `CreateConfigurationSecret=true` only when role groups, the SPA client,
and a user-pool source are also selected. The stack then creates the
project-specific secret `novamind-crp-cognito-config-dev` unless an explicitly
validated name is supplied. Its JSON fields match
`CognitoConfigurationSecretArn` in the ECS services template:

```json
{
  "issuer": "https://cognito-idp.us-east-1.amazonaws.com/POOL_ID",
  "client_id": "PUBLIC_SPA_CLIENT_ID",
  "customer_group": "customers",
  "reviewer_group": "reviewers",
  "admin_group": "admins",
  "customer_bindings": []
}
```

This secret contains no Cognito client secret, password, access token, or
legacy secret. Customer bindings are trusted application data and must be
populated only from an approved subject-to-customer onboarding process; an
empty array intentionally causes customer authentication to fail closed until
bindings exist. The API task reads the JSON keys through Secrets Manager
secret projections. Publisher and worker tasks do not receive Cognito
configuration. The `reviewer_group` field remains only as a compatibility
field for the existing backend schema; this stack does not create a reviewer
group or expose Reviewer as an application role.

## Review-only deployment order

1. Confirm no existing pool/client/domain is being replaced. If an existing
   pool is selected, pass its ID and leave `CreateUserPool=false`.
2. Prepare a parameter file outside the repository. Do not put credentials or
   token material in it. Use the CloudFormation helper with
   `ChangeSetType CREATE` for `novamind-cognito-dev`.
3. Inspect the change set. The expected resources are only the explicitly
   enabled pool, groups, public SPA client, Hosted UI domain, and configuration
   secret. Do not execute an unexpected resource set.
4. Record only stack outputs and metadata. Feed the configuration-secret ARN
   to the reviewed ECS services stack and supply the same issuer/client/group
   contract to the frontend build as `VITE_COGNITO_DOMAIN`,
   `VITE_COGNITO_CLIENT_ID`, `VITE_COGNITO_REDIRECT_URI`, and
   `VITE_COGNITO_LOGOUT_URI`.

The current repository does not establish that an ALB, CloudFront frontend,
or ECS application services are deployed. Those remain separate prerequisites;
creating Cognito does not make the application reachable.

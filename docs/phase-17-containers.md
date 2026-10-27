# Phase 17 — Containerization

Container configuration and focused local checks are implemented. **14/14 Phase 17 tests passed**, both Compose configurations validated, and the frontend built offline using installed dependencies. **Image builds and running-container verification remain unverified:** Docker CLI/Compose are installed, but the Docker Desktop Linux engine is unavailable (`dockerDesktopLinuxEngine` pipe missing). No image pull, package install, registry call, container deployment, or AWS operation ran. Phase 18 is not started.

AI-01B later retired the Gemini adapter and its direct dependency. Verification
counts and implementation-history notes below describe Phase 17 at the time;
they are not AI-01B test results.

## Packaging and routing

- `backend/Dockerfile` uses Python 3.12 slim Bookworm stages. The dependency stage installs the existing exact-version `requirements.txt` into a virtual environment using binary wheels only. The runtime copies the environment, application, knowledge fixtures, and small container bootstrap/probe modules. No compiler, wheel cache, host virtual environment, tests, or evaluation datasets are copied.
- `frontend/Dockerfile` uses Node.js 24 and the existing npm lockfile to build static Vite assets. Its final Nginx Alpine stage contains the build output and routing configuration; Node, npm, source code, and node_modules are absent from that stage. Nginx serves the built application rather than running a Vite development/preview server.
- Both runtime processes use UID/GID **10001:10001**, high internal ports, and exec-form commands. Compose uses read-only root filesystems, dropped capabilities, no-new-privileges, init/reaping, and bounded writable `/tmp` tmpfs mounts. No host source/data directory or Docker socket is mounted.
- The backend runs exactly **one Uvicorn worker**, without reload or trusted proxy headers. Uvicorn access logs and Nginx access logs are disabled; existing Phase 16 application telemetry remains available with its original behavior. Nginx operational errors still go to stderr and are not application telemetry.
- Only the frontend publishes a host port: `127.0.0.1:8080` by default. In the base configuration, the backend belongs only to the internal `application` network, with no published port or external egress. The frontend joins both `application` (for `/api/` proxying to the backend) and the non-internal bridge network `edge` (for published-port connectivity). The frontend therefore has an external network path, while host publication remains loopback-only. Browser requests remain same-origin; no new CORS configuration is needed. The optional Bedrock overlay explicitly enables backend egress.
- Nginx forwards `/api/` with the original path/query and bearer header to `backend:8000`, preserves API status codes and trace headers, and disables upstream retries so writes/reviews are not replayed. Docker's local DNS resolver refreshes the backend address after recreation. Missing `/assets/` files return 404; other frontend routes use the SPA fallback. Fingerprinted assets are cacheable; HTML is revalidated. Backend `/docs` and `/openapi.json` are not exposed through this frontend proxy, consistent with the earlier Vite `/api` proxy.

Each image has its own narrow build context (`backend/` or `frontend/`) and allowlist `.dockerignore`. Root files—including the three pre-existing untracked JSON files—are outside both contexts and were not read or changed. Environment files, private keys, conversation stores, caches, local dependency directories, and host builds are excluded. No build argument or frontend variable contains credentials.

## Environment and authentication

The optional `.env.container.example` contains only `NOVAMIND_PORT=8080` and configuration comments. Pass it with `--env-file`, or copy it to a local ignored `.env` and change the port. Runtime images set tracing exports off; base Compose does not pass provider credentials or inherit arbitrary host environment variables.

The backend container factory delegates to the existing `create_app()`. With no `NOVAMIND_AUTH_FILE`, authentication retains the original empty registry: public `/api/health` works, while business requests return 401. The existing frontend still has no login/bearer UI; containerization does not introduce an anonymous fallback.

For authenticated local API clients, the optional `compose.auth.yaml` mounts a host-provided credential file as a read-only Compose secret at `/run/secrets/novamind_auth`. `NOVAMIND_AUTH_SOURCE` is the host file path, not the credential value. The factory validates entries using existing Phase 13 `LocalCredential` and `LocalAuthenticationProvider`; roles, customer bindings, duplicate-token rejection, ownership, and authenticated review are unchanged.

The file is a JSON array of existing credential objects, for example this **shape only** (replace the token with a host-generated unpredictable value):

```json
[
  {
    "token": "<host-generated-unpredictable-local-token>",
    "identity": {
      "user_id": "00000000-0000-4000-8000-000000000017",
      "role": "ADMIN"
    }
  }
]
```

Keep the real file outside the repository, or in the ignored `.container-secrets/` directory. Provision it deliberately; no default credential file or token is generated by this phase. On Linux, arrange host ownership/permissions so runtime UID 10001 can read the mounted file; Compose file secrets do not reliably remap host UID/mode. Docker Desktop file-sharing permissions require runtime verification. These local mounted files are not an encrypted secret manager.

Configured paths must be absolute. Missing/unreadable files, malformed or duplicate-key JSON, invalid identity/token shapes, duplicate/conflicting credentials, files larger than 65,536 bytes, and lists longer than 1,000 entries stop startup with a fixed error without printing token values or paths. No invalid-configuration fallback grants access. Empty lists are allowed and remain default-deny. File loading occurs only at startup; recreate the backend to load changes. No token expiry/rotation, customer seeding, or durable business-state provisioning is added.

## Optional SQLite durable volume (P0-4A)

The base Compose stack remains stateless: it has no backend data mount and does
not set `NOVAMIND_SQLITE_PATH`. To opt in, combine the new backend-only
`compose.persistence.yaml` overlay with `compose.yaml` (and, as needed, the
existing auth, offline, Bedrock, or demo overlays):

```powershell
docker compose --env-file .env.container.example `
  -f compose.yaml -f compose.persistence.yaml config --quiet
docker compose --env-file .env.container.example `
  -f compose.yaml -f compose.persistence.yaml up --no-build --pull never -d --wait
```

The overlay sets the server-only path
`/srv/state/novamind/novamind.sqlite` and mounts the durable named volume
`novamind_sqlite` at `/srv/state`. Set `NOVAMIND_SQLITE_VOLUME` in the
invoking shell only when an operator needs an explicitly managed replacement
volume name. It does not set a provider, agent engine, network, credential, or
frontend setting, so LangGraph remains the default and the optional Strands and
Bedrock selections retain their existing rules.

The backend image creates `/srv/state/novamind` with mode `0750` and UID/GID
`10001:10001` before switching to its non-root runtime user. Docker's default
empty-volume population copies that pre-owned child directory into a newly
created named volume; do not add `volume.nocopy: true`. Consequently, the backend can
create the database and run its existing migrations as UID/GID 10001 while its
root filesystem remains read-only. The volume is the sole durable write target;
`/tmp` remains temporary. Initialization happens at backend startup, not at
image build, and a bad or unwritable database fails startup rather than falling
back to in-memory state.

This is local, single-node SQLite persistence, not a shared or multi-writer
database. It preserves only the application components currently wired to the
SQLite persistence boundary; it does not change authentication, provider
credentials, model calls, network policy, or create a data-recovery guarantee.
Use one backend worker, retain the same volume across recreations, and do not
attach the volume to another writer.

### Backup, rollback, and restore

Back up only while the backend is stopped, so the database is quiescent. Use
the same Compose file list that created the stack and omit `-v` from `down`:

```powershell
docker compose --env-file .env.container.example `
  -f compose.yaml -f compose.persistence.yaml stop backend
docker compose --env-file .env.container.example `
  -f compose.yaml -f compose.persistence.yaml cp backend:/srv/state/novamind/novamind.sqlite .\backups\novamind.sqlite
Get-FileHash .\backups\novamind.sqlite -Algorithm SHA256
```

Keep the backup outside the repository with its checksum, creation time, image
tag, and Compose overlay list. If a future SQLite journal sidecar exists, copy
the complete `/srv/state/novamind` directory instead of only the database file.
Do not use `docker compose down -v`, `docker volume rm`, or an ad-hoc recursive
delete as a normal stop/upgrade procedure: each can destroy the named volume.

SQLite schema migrations are forward-only. Before an image upgrade, make and
verify a quiescent backup. To roll back application code while retaining data,
stop the backend, select the earlier image/configuration, and start it with the
same persistence overlay and volume only if that image supports the current
schema. To roll back data, first stop the backend and preserve a copy of the
current volume; then restore the pre-upgrade backup into a freshly created,
separate volume. The following procedure preserves the original volume rather
than overwriting it (replace only the backup filename and image tag after
reviewing them):

```powershell
$restoreVolume = 'novamind_sqlite_rollback'
$backupDirectory = (Resolve-Path .\backups).Path
docker volume create $restoreVolume
docker run --rm --read-only --user 10001:10001 `
  --mount "type=volume,source=$restoreVolume,target=/srv/state" `
  --mount "type=bind,source=$backupDirectory,target=/backup,readonly" `
  --entrypoint python novamind-ai-customer-resolution-backend:local `
  -c "from pathlib import Path; import shutil; shutil.copyfile(Path('/backup/novamind.sqlite'), Path('/srv/state/novamind/novamind.sqlite'))"
docker run --rm --read-only --user 10001:10001 `
  --mount "type=volume,source=$restoreVolume,target=/srv/state,readonly" `
  --entrypoint python novamind-ai-customer-resolution-backend:local `
  -c "import sqlite3; c=sqlite3.connect('file:/srv/state/novamind/novamind.sqlite?mode=ro', uri=True); assert c.execute('PRAGMA quick_check').fetchone() == ('ok',)"
$env:NOVAMIND_SQLITE_VOLUME = $restoreVolume
docker compose --env-file .env.container.example `
  -f compose.yaml -f compose.persistence.yaml up --no-build --pull never -d --wait
```

The one-off restore commands run as UID/GID 10001 and mount no writable path
other than the new data volume. They do not start the API or contact AWS. Use
an earlier compatible image for the final `up` when rolling back application
code as well as data. There is no automatic schema downgrade; do not remove the
original volume until the restored stack has been checked and explicitly
accepted.

Before using this overlay, Docker Desktop/Linux containers must be available,
the local backend image must already exist (or normal build/network access must
be explicitly authorized), and the operator needs permission to create and
retain named volumes. The backup destination must be writable by the Docker
client. No Docker, AWS, or model operation was performed for this change.

## Optional Bedrock provider wiring (AI-02B)

Set `NOVAMIND_LLM_PROVIDER=bedrock`, `AWS_REGION` (for example, `us-east-1`),
and `BEDROCK_MODEL_ID` to a Converse-compatible model ID, inference-profile ID,
or supported Bedrock ARN. Both configuration values are required and validated
locally at startup; malformed values stop startup with a fixed error. An unset
selector remains unavailable, and unknown selectors or stale `GEMINI_*`
configuration still stop startup. Region/model syntax validation does not check
whether a model is available or whether the runtime has permission to invoke it.

For Compose, add `-f compose.bedrock.yaml` to the base and auth overlays. This
backend-only overlay supplies the selector, region, model ID, and outbound
network access. It supplies **no credentials**. Make a standard boto3 credential
provider available to the backend process at runtime, such as a workload role,
an injected environment credential set, or a mounted shared profile. The code
passes no access key, secret key, profile, or custom endpoint to boto3; the
runtime identity needs permission for `bedrock:InvokeModel` on the selected
model or inference profile. Keep credentials outside the repository, Compose
files, and frontend configuration. See [boto3 credential resolution](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html)
and [Converse model selection](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html).

Amazon Bedrock is the only supported cloud model provider. Identity and run snapshots
report `bedrock`; model diagnostics use `bedrock`.
The Bedrock adapter requests JSON in a system instruction, while existing
application validation still decides whether the response is usable. SDK
connection/read timeouts are bounded at 30 seconds each and automatic SDK
retries are disabled. Offline mocked tests do not verify live AWS access or
model-specific Converse support.

## Health and local verification

The backend Docker health check calls only `http://127.0.0.1:8000/api/health`, with a two-second timeout, bounded response, disabled environment proxies, and no redirects. It validates the existing health response. It does not check a model, database, external service, authorization readiness, or workflow recovery.

The frontend Docker health check uses its local `/healthz`. Compose waits for initial backend health before starting the frontend; this does not promise automatic recovery when a dependency later becomes unhealthy. The separate loopback smoke checker verifies the page, built JavaScript, frontend health, proxied backend health, anonymous API denial, trace header, and missing-asset 404 behavior. It performs read-only requests, follows no redirects, uses no credentials/proxies, and never contacts a model/cloud service.

After Docker Desktop's Linux engine is available, the intended commands from the repository root are:

```powershell
docker compose --env-file .env.container.example config --quiet
docker compose --env-file .env.container.example build
docker compose --env-file .env.container.example up --no-build --pull never -d --wait
docker compose --env-file .env.container.example exec -T frontend nginx -t
./backend/.venv/Scripts/python.exe -B scripts/container_smoke.py --port 8080
docker compose --env-file .env.container.example ps
```

The **build command requires preloaded base images and cached packages or permitted registry access**. It was not run in this offline session. BuildKit dependency download access is separate from the runtime internal network. Runtime `up --no-build --pull never` deliberately cannot silently fetch or rebuild missing images. If using a different port, pass the same port to the checker. Stop the base local stack with `docker compose --env-file .env.container.example down`; without the persistence overlay, stopping/recreating the backend loses its in-memory records/checkpoints as before. With the persistence overlay, use its documented Compose file list and do not add `-v`.

For the optional credential overlay, set the host path and use both files consistently for startup/management:

```powershell
$env:NOVAMIND_AUTH_SOURCE = 'C:/private/novamind-auth.json'
docker compose --env-file .env.container.example -f compose.yaml -f compose.auth.yaml config --quiet
docker compose --env-file .env.container.example -f compose.yaml -f compose.auth.yaml up --no-build --pull never -d --wait
```

No real secret file was used during verification. The overlay was syntax-validated with an existing non-secret file as a temporary path placeholder; containers were not launched with it.

## Recorded focused checks

- `./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_containers.py -v` — **14/14 passed**. Covers default-deny/health/trace startup, mounted credentials and roles, isolated state after restart, safe configuration failure, bounded loopback probes, image/Compose/context boundaries, API routing/retry configuration, and the smoke checker's positive/negative outcomes. Two initial probe-test mock setup errors were corrected before the passing run.
- `python -B -m unittest discover -s tests -p test_container_offline.py -v` — **7/7 passed** for this change. The tests use in-process ASGI, temporary files, blocked sockets/DNS, and YAML parsing only. They verify opt-in absolute-path handling, the named writable persistence volume, UID/GID 10001 image preparation, retained read-only base behavior, and non-interference with auth/offline/demo/Bedrock overlays or the default LangGraph selector.
- `python -B -m unittest discover -s tests -p test_containers.py -v` — **14/14 passed** for this change. These mocked/static container contracts retain default-deny authentication, loopback-only exposure, read-only hardening, and the single-worker image configuration.
- `docker compose --env-file .env.container.example -f compose.yaml config --quiet` — **passed**.
- The same config check with `-f compose.auth.yaml` and an explicit non-secret source path — **passed**.
- `npm.cmd --offline run build` in `frontend/` — **passed**, Vite 8.3.1, 17 modules. No install or registry query ran; output is ignored `frontend/dist/`.
- `git diff --check` — **passed**, line-ending normalization warnings only.
- Docker image builds, Nginx binary/config validation, live container health/startup, runtime permissions, Linux wheel installation, and the end-to-end smoke checker against running containers — **not run**, engine unavailable. Static configuration checks and mocked health checks do not establish those results.

Only Phase 17 focused tests/checks ran. No earlier test suite, Phase 15 evaluation, full regression, AWS/Gemini call, image push, or deployment ran.

## Dependencies, retained limits, and files

No Python/npm dependency or lockfile changes. Tests reuse installed HTTPX/PyYAML; bootstrap and probes use the standard library plus existing application contracts. Python/Node/Nginx base images are required packaging inputs, not additional application packages. Their tags are version-family tags, not immutable digests; exact image reproducibility, availability, vulnerabilities, sizes, and architecture compatibility remain unverified. No image-size or production-readiness claim is made. Linux builds intentionally fail if a pinned Python package lacks an appropriate binary wheel; no compiler or dependency upgrade is silently added.

Application modules under `backend/app`, frontend source, business rules, tool allowlists, guardrails, authentication/ownership, human review, evaluation, and observability code are unchanged by this container overlay. Container bootstrap remains the configuration boundary. Existing agent/HITL Python entry points remain available; no new agent HTTP endpoints or autonomous execution are added. Approval still executes nothing. Without the persistence overlay, records/checkpoints remain in memory. The opt-in volume uses the existing SQLite composition; it adds no replication, TLS, production identity, multi-worker coordination, or scaling guarantee.

Added backend/frontend Dockerfiles and context allowlists, Nginx configuration, base/optional-auth Compose files, an environment example, container bootstrap/health modules, `scripts/container_smoke.py`, and `tests/test_containers.py`. P0-4A additionally adds `compose.persistence.yaml`, a UID/GID 10001-owned backend volume mountpoint, and offline configuration coverage. Updated relevant READMEs, local-development guidance, both roadmaps, the Phase 16 status pointer, and this guide. The three untracked root JSON files remain untouched. **No Docker deployment, AWS operation, or Phase 18 work was started.**

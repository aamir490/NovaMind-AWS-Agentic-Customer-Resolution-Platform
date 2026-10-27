# NovaMind AI — Complete Local Deployment Guide

**Project:** NovaMind AI — Agentic Customer Resolution Platform
**Built by Aamir**
**Workspace:** `E:\codex-projetc\NovaMind-local-smoke-test`
**Platform:** Windows PowerShell + Docker Desktop (Linux containers) + Docker Compose V2 + AWS CLI
**Application URL:** <http://127.0.0.1:8080>
**Guide prepared:** 21 October 2026
**Deployment policy:** Clean up **only NovaMind** containers and images; **preserve SQLite volumes and data**. Live Amazon Bedrock inference is **optional and must never be triggered without explicit approval**.

> **READ FIRST — commands in this file are instructions, not actions already run by this document.** This guide was prepared from the successful local deployment and terminal/UI verification in our conversation. It does not alter your Windows files, Docker engine, AWS account, or Git repository. Download it and save it to your project (for example, `docs\NovaMind-Complete-Local-Deployment-Guide.md`) before using it.

## Quick navigation

1. Safety rules and verified configuration
2. Prerequisites and project inventory
3. Readiness checks before stopping the application
4. Back up and verify existing SQLite data
5. Configure private authentication and AWS/Bedrock settings
6. Validate the six-file Compose deployment
7. Stop/remove previous NovaMind containers (preserve volumes)
8. Optionally remove only NovaMind image tags
9. Rebuild backend and frontend images
10. Start the complete application
11. Verify health, SQLite schema and historical data
12. Verify LangGraph and Bedrock configuration **offline**
13. Verify CUSTOMER → ADMIN → CUSTOMER communication
14. Optional, explicitly approved real Bedrock end-to-end test
15. Logs, debugging and safe redeployment
16. Shutdown and restart commands
17. SQLite disaster recovery / restore procedure
18. Troubleshooting table and deployment checklist

---

## 1. Safety rules and verified configuration

**Never run these as part of the standard deployment:**

```powershell
# DANGEROUS — examples to AVOID; do not execute:
# docker compose down -v
# docker volume rm novamind_sqlite
# docker volume prune
# docker system prune --volumes
# docker rm -f $(docker ps -aq)
# docker rmi -f $(docker images -q)
```

They can remove your case history or unrelated projects. Do not run global cleanup or delete a Docker volume to fix an application error.

**Verified working configuration (21 Oct 2026):**

| Item | Actual local value |
|---|---|
| Workspace | `E:\codex-projetc\NovaMind-local-smoke-test` |
| Backend container | `novamind-local-backend-1` |
| Frontend container | `novamind-local-frontend-1` |
| Backend image | `novamind-ai-customer-resolution-backend:local` |
| Frontend image | `novamind-ai-customer-resolution-frontend:local` |
| Browser | `http://127.0.0.1:8080` |
| SQLite database inside backend | `/srv/state/novamind/novamind.sqlite` |
| Latest verified schema migration | `17` |
| Existing cases at verification | `10` |
| Communication table | `case_customer_communications` |
| AWS profile on Windows | `novamind` |
| AWS region | `us-east-1` |
| Bedrock model / inference profile | `us.amazon.nova-lite-v1:0` |
| Dispatcher user ID | `40dcf944-851f-4d22-953a-6b3b91a40808` |
| LLM mode | `bedrock`, supplied by `compose.bedrock.yaml` |
| Automatic dispatcher | `true`, supplied by the private dispatcher overlay |
| Confirmed host backup | `%LOCALAPPDATA%\NovaMindAI\backups\novamind-before-v17-20261021-151319.sqlite` |

The confirmed backup was **7,524,352 bytes**, passed `PRAGMA integrity_check`, and included **10 cases**. After migration, the running database passed integrity checks, reported **10 cases**, had `case_customer_communications`, and had `MAX(version) = 17`.

**Important distinction:** Docker `Healthy` and successful offline tests do **not** by themselves prove an actual Bedrock invocation on the latest code. A real Bedrock investigation of **Test 03** completed on an earlier version. After subsequent changes, the deterministic troubleshooting and final LangGraph response were verified **offline**, while the ADMIN-to-CUSTOMER communication and point-wise rendering were verified in the running application.

**Stop condition:** If an investigation is in progress, an ADMIN is mid-review, a Compose file is missing, or the database cannot be backed up, do not proceed with stopping/recreating containers. Restarting can interrupt active work. A dispatcher job in `CLAIMED` is *not by itself* proof of an active run: check the corresponding workflow status.

---

## 2. Prerequisites and project inventory

Open **Windows PowerShell** (not a Linux shell):

```powershell
cd E:\codex-projetc\NovaMind-local-smoke-test
Get-Location

docker --version
docker compose version
docker info

aws --version
py -3.12 --version
git status --short
```

**Why:** Confirms your actual project folder, Windows Python 3.12, Docker Desktop/Linux engine, Compose V2, AWS CLI and local source checkout are available.

**Effect:** Read-only. `docker info` can display lengthy engine information; `git status --short` may show intentional uncommitted changes — do not discard them.

**Successful result:** Commands return versions/engine details without errors and `Get-Location` shows the expected folder. Python 3.12 is used below only for validating a Windows-side SQLite backup, not for running the containerized backend.

Inspect project containers and images **before** cleanup:

```powershell
docker ps -a --filter "name=novamind-local"
docker images novamind-ai-customer-resolution-backend
docker images novamind-ai-customer-resolution-frontend
docker volume ls --filter "name=novamind"
```

**Why:** Inventory resources so you remove only the intended NovaMind stack. **Effect:** Read-only. **Success:** Lists match the expected stack, if previously deployed. On a new laptop these lists may be empty.

**Do not assume a Docker volume's exact engine-level name from a friendly Compose name.** A Compose project prefix may be applied. For recovery, discover the volume actually mounted by the backend (Section 17).

---

## 3. Readiness checks before stopping the application

If the app is running, check the ADMIN Portal's cases, workflow status, pending human reviews, and dispatcher activity. Do not recreate containers while an active investigation is processing or someone is submitting a decision.

Basic read-only checks:

```powershell
docker ps --filter "name=novamind-local"
docker logs --tail 50 novamind-local-backend-1
```

**Why:** Avoid interrupting in-flight runs. **Effect:** Read-only. **Success:** Expected containers are running, without recent recurring fatal errors. Logs can contain case/customer information; redact before sharing. Logs are not sufficient alone to prove there are no active jobs — use the ADMIN interface/workflow status.

If this is a **new installation with no backend container**, skip the backup commands that need a running container and proceed only when you have valid private authentication/AWS files. Do not fabricate private overlay contents.

---

## 4. Back up and verify the existing SQLite database — BEFORE cleanup

This section is based on the **backup sequence that actually succeeded** after the earlier failed `/tmp` copy. Run only if `novamind-local-backend-1` exists and the database is mounted.

> Do not use `/tmp/novamind-before-v17.sqlite` as the transfer staging location. In the earlier attempt Docker reported that the file did not exist when copied. Also, opening a nonexistent Windows backup with ordinary `sqlite3.connect(path)` can create a **0-byte empty database** and misleadingly return `Integrity: ok`. Use **read-only URI mode** for verification.

### 4.1 Create a SQLite snapshot inside the persistent mount

```powershell
cd E:\codex-projetc\NovaMind-local-smoke-test

$backupDir = Join-Path $env:LOCALAPPDATA "NovaMindAI\backups"
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$containerSnapshot = "/srv/state/novamind/backup-before-redeploy-$stamp.sqlite"
$backupFile = Join-Path $backupDir "novamind-before-redeploy-$stamp.sqlite"

docker exec novamind-local-backend-1 python -c "import sqlite3,os; src=sqlite3.connect('file:/srv/state/novamind/novamind.sqlite?mode=ro',uri=True); dst=sqlite3.connect('$containerSnapshot'); src.backup(dst); dst.close(); src.close(); print('Snapshot bytes:',os.path.getsize('$containerSnapshot'))"

if ($LASTEXITCODE -ne 0) { throw "SQLite snapshot failed; DO NOT redeploy" }
```

**Why:** SQLite's backup API makes a consistent snapshot even when WAL mode is enabled. **Effect:** Reads the live database and **creates a separate new snapshot file** within the persistent mount; it does not overwrite the live database. **Success:** `Snapshot bytes: [positive number]` (the verified prior snapshot was `7524352` bytes).

### 4.2 Copy snapshot to Windows

```powershell
docker cp "novamind-local-backend-1:$containerSnapshot" "$backupFile"
if ($LASTEXITCODE -ne 0) { throw "Backup copy failed; DO NOT redeploy" }

$file = Get-Item -LiteralPath $backupFile
Write-Host "Backup size:" $file.Length "bytes"
if ($file.Length -lt 4096) { throw "Backup is empty or unexpectedly small" }
```

**Why:** A backup stored outside Docker protects against accidental volume loss. **Effect:** Creates a Windows file under `%LOCALAPPDATA%\NovaMindAI\backups`; does not modify live cases. **Success:** A substantial nonzero file (historically `7,524,352` bytes).

### 4.3 Validate the copied backup WITHOUT creating a new one

```powershell
py -3.12 -c "import sqlite3,sys,pathlib; uri=pathlib.Path(sys.argv[1]).resolve().as_uri()+'?mode=ro'; db=sqlite3.connect(uri,uri=True); integrity=db.execute('PRAGMA integrity_check').fetchone()[0]; table=db.execute('SELECT name FROM sqlite_master WHERE name=?',('support_cases',)).fetchone(); print('Integrity:',integrity); print('Support cases table:',bool(table)); print('Case count:',db.execute('SELECT COUNT(*) FROM support_cases').fetchone()[0] if table else 'missing'); db.close(); sys.exit(0 if integrity=='ok' and table else 1)" "$backupFile"

if ($LASTEXITCODE -ne 0) { throw "Backup integrity or schema verification failed; DO NOT redeploy" }

Write-Host "BACKUP VERIFIED SUCCESSFULLY" -ForegroundColor Green
Write-Host "Location: $backupFile"
```

**Why:** Confirms that the Windows copy is a readable SQLite database with the application's case table. **Effect:** Read-only. **Success:** `Integrity: ok`, `Support cases table: True`, case count as expected, then `BACKUP VERIFIED SUCCESSFULLY`.

On an existing installation, investigate an unexpected drop in case count before continuing. On a genuinely new empty installation, zero cases are normal after first boot.

**Known-good historical backup, for reference only:**

```text
C:\Users\aamir\AppData\Local\NovaMindAI\backups\novamind-before-v17-20261021-151319.sqlite
Size: 7524352 bytes | Integrity: ok | Cases: 10
```

This contains private customer data: do not check backup files into Git or share them publicly. Extra snapshots inside the volume can be cleaned up later only after confirming an external backup; no auto-delete command is provided.

---

## 5. Configure authentication, AWS credentials and the dispatcher

**Re-use the existing working private files.** Do not commit them or generate fake secrets. The verified configuration uses a private directory outside Git.

```powershell
cd E:\codex-projetc\NovaMind-local-smoke-test

$private = Join-Path $env:LOCALAPPDATA "NovaMindAI\private"

$env:NOVAMIND_AUTH_SOURCE = Join-Path $private "novamind-auth.json"
$env:NOVAMIND_DISPATCHER_USER_ID = "40dcf944-851f-4d22-953a-6b3b91a40808"
$env:AWS_PROFILE = "novamind"
$env:AWS_REGION = "us-east-1"
$env:BEDROCK_MODEL_ID = "us.amazon.nova-lite-v1:0"
```

**Why:** Supplies the same environment variables used during the successful six-overlay startup. **Effect:** Changes variables **in the current PowerShell session only**; it does not edit your Windows user environment, Git files, or AWS account. **Success:** No error output.

Check required private files **without printing their contents**:

```powershell
$requiredPrivate = @(
    $env:NOVAMIND_AUTH_SOURCE,
    (Join-Path $private "compose.aws-credentials.yaml"),
    (Join-Path $private "compose.dispatcher.yaml")
)

foreach ($path in $requiredPrivate) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required private configuration file missing: $path"
    }
    Write-Host "Present:" (Split-Path $path -Leaf)
}
```

**Why:** Missing credentials/auth files prevent reproducing the working deployment. **Effect:** Read-only; file contents and bearer tokens are not shown. **Success:** Three `Present:` entries. If missing, **stop** and recover the existing private files securely. Do not replace them with invented JSON/YAML, and never paste tokens or AWS secret keys into frontend code.

The auth file supports CUSTOMER/ADMIN identity (and other configured roles). The dispatcher user ID must correspond to a permitted ADMIN identity in the actual auth configuration. Private AWS credentials must be available **inside the backend container** via the existing private AWS Compose overlay; having a working Windows CLI profile alone does not supply container credentials.

### AWS CLI preflight — read-only cloud operations

```powershell
aws configure list-profiles
aws sts get-caller-identity --profile novamind --region us-east-1
```

**Why:** Confirms the host AWS profile exists and identifies its authenticated AWS principal. **Effect:** Contacts AWS APIs but does **not** invoke a model. **Success:** `novamind` appears in the profile list and STS returns an identity without credential errors. Do not publish full identity details unnecessarily.

**Optional Bedrock catalog/permission diagnostic (read-only; not previously used as a passing acceptance test):**

```powershell
aws bedrock get-inference-profile `
  --inference-profile-identifier us.amazon.nova-lite-v1:0 `
  --profile novamind `
  --region us-east-1
```

**Why:** Checks information/visibility for this inference profile. **Effect:** A Bedrock **control-plane** request, not inference. **Success:** Profile information returned; an `AccessDenied` here may only mean the identity lacks the *describe* permission and is not by itself a complete model-runtime test. Conversely, a successful describe call does not prove runtime `InvokeModel`/`Converse` permissions.

**No live Bedrock inference is part of preparation, image building, startup, migration, or the routine checks in this guide.** A later new complaint may trigger the automatic dispatcher and billable Bedrock inference.

---

## 6. Validate the EXACT six Compose files

These six files — in this order — were used for the successfully tested app:

| Order | File | Purpose |
|---:|---|---|
| 1 | `compose.yaml` | Base backend/frontend services and local networking |
| 2 | `compose.persistence.yaml` | Durable SQLite volume |
| 3 | `compose.auth.yaml` | Auth mounting/role configuration |
| 4 | `compose.bedrock.yaml` | AWS Bedrock provider settings |
| 5 | `%LOCALAPPDATA%\NovaMindAI\private\compose.aws-credentials.yaml` | Private container-side AWS credential mounting |
| 6 | `%LOCALAPPDATA%\NovaMindAI\private\compose.dispatcher.yaml` | Automatic dispatcher configuration |

For repeated Compose commands **in this PowerShell session**, define an argument array:

```powershell
$composeArgs = @(
    '-f', 'compose.yaml',
    '-f', 'compose.persistence.yaml',
    '-f', 'compose.auth.yaml',
    '-f', 'compose.bedrock.yaml',
    '-f', (Join-Path $private 'compose.aws-credentials.yaml'),
    '-f', (Join-Path $private 'compose.dispatcher.yaml')
)

# Validate syntax and variable resolution without printing resolved secret paths/values.
docker compose @composeArgs config --quiet
if ($LASTEXITCODE -ne 0) { throw "Compose configuration invalid; DO NOT continue" }
Write-Host "Compose configuration valid"
```

**Why:** The private overlays are needed for the *real* authenticated Bedrock+dispatcher setup, not an offline-only demo. **Effect:** Read-only, no containers started; `--quiet` limits accidental config disclosure. **Success:** No Compose errors and `Compose configuration valid`.

`$composeArgs` is PowerShell argument splatting for exactly the same six-file order as the verified multiline `docker compose -f ...` command. If opening a **new** PowerShell window, repeat Section 5 and this section before invoking later `docker compose @composeArgs` commands.

**Not equivalent:** The older `NovaMind-Clean-Rebuild.ps1` helper described in an earlier guide starts an **offline** Compose mode and does **not** reproduce this Bedrock/auth/automatic-dispatcher deployment. Do not silently substitute it for this six-file procedure.

---

## 7. Stop and remove previous NovaMind containers — preserve volumes

**Only after Section 4 verifies a genuine backup** (or after confirming this is a truly new installation with no previous data), and **only when no work is in progress**:

```powershell
# Show exactly which services belong to this six-file Compose project.
docker compose @composeArgs ps -a

# Remove Compose containers/networks but NOT named volumes.
docker compose @composeArgs down

if ($LASTEXITCODE -ne 0) { throw "NovaMind Compose shutdown failed; inspect containers" }
```

**Why:** Start the next deployment without old NovaMind containers. **Effect:** Stops/removes containers and associated Compose network, but **preserves named volumes** because `-v` is absent. It can interrupt in-flight investigations and sessions, hence the readiness gate.

**Success:** Compose reports stopped/removed project containers. Other Docker projects should not be affected. If unfamiliar containers appear in `ps -a`, pause and inspect before running `down`.

Check:

```powershell
docker ps -a --filter "name=novamind-local"
docker volume ls --filter "name=novamind"
```

**Success:** Old NovaMind containers no longer running; SQLite volume remains. Do not delete it.

---

## 8. OPTIONAL: Remove ONLY previous NovaMind images

Removing images is **not required** for normal rebuilds, but is part of the requested clean procedure.

```powershell
docker images novamind-ai-customer-resolution-backend
docker images novamind-ai-customer-resolution-frontend

docker image rm novamind-ai-customer-resolution-backend:local
docker image rm novamind-ai-customer-resolution-frontend:local
```

**Why:** Discard old NovaMind image tags before rebuilding. **Effect:** Removes just these two image tags and unreferenced layers as applicable, **not** containers from other projects or SQLite volumes. **Success:** Docker reports image tags/layers removed; `No such image` is normal for a first installation.

If Docker reports `image is being used`, **do not add `--force`**. Inspect dependent containers before taking action. Docker may keep shared layers used by other images.

---

## 9. Build updated backend and frontend images

This is the **exact build command** used in the successful October 21 deployment:

```powershell
cd E:\codex-projetc\NovaMind-local-smoke-test

docker compose -f compose.yaml build backend frontend
if ($LASTEXITCODE -ne 0) { throw "Backend/frontend Docker build failed" }
```

**Why:** Builds the current local code (FastAPI, LangGraph, Bedrock integration, SQLite/dispatcher, React ADMIN/CUSTOMER portals and the updated point-wise support message renderer). **Effect:** Builds local images; no database or cloud inference operations. It may download container layers/dependencies and uses build cache where possible.

**Verified successful output:**

```text
Image novamind-ai-customer-resolution-frontend:local Built
Image novamind-ai-customer-resolution-backend:local Built
```

For a normal one-service code change, build only that service instead:

```powershell
# Use one OR the other only when relevant:
docker compose -f compose.yaml build backend
docker compose -f compose.yaml build frontend
```

If you suspect a bad build cache, Docker supports `docker compose -f compose.yaml build --no-cache backend frontend`; this is a **troubleshooting alternative**, not the tested default and is slower. Building does **not** deploy new code into already running containers.

---

## 10. Deploy the complete application — AUTH + SQLite + LangGraph + Bedrock + dispatcher

After environment setup and build, this is the **verified six-file deployment** in its full, copy-paste form:

```powershell
cd E:\codex-projetc\NovaMind-local-smoke-test

# Re-run the Section 5 environment setup if using a fresh PowerShell session.
$private = Join-Path $env:LOCALAPPDATA "NovaMindAI\private"
$env:NOVAMIND_AUTH_SOURCE = Join-Path $private "novamind-auth.json"
$env:NOVAMIND_DISPATCHER_USER_ID = "40dcf944-851f-4d22-953a-6b3b91a40808"
$env:AWS_PROFILE = "novamind"
$env:AWS_REGION = "us-east-1"
$env:BEDROCK_MODEL_ID = "us.amazon.nova-lite-v1:0"

docker compose `
  -f compose.yaml `
  -f compose.persistence.yaml `
  -f compose.auth.yaml `
  -f compose.bedrock.yaml `
  -f "$private\compose.aws-credentials.yaml" `
  -f "$private\compose.dispatcher.yaml" `
  up -d --no-build --pull never --force-recreate --wait backend frontend

if ($LASTEXITCODE -ne 0) { throw "NovaMind local deployment failed; inspect logs" }
```

**Why:** Uses the working project configuration, including authentication, durable SQLite, Bedrock, dispatcher, API and UI. **Effect:** Starts/recreates backend and frontend containers; **may apply database schema migrations on backend startup** (such as v17), so an externally verified backup must already exist for an existing deployment. Named volumes are preserved. Does not invoke an AI model merely by starting.

**Verified successful output:**

```text
Container novamind-local-backend-1 Healthy
Container novamind-local-frontend-1 Healthy
```

**Important:** `--force-recreate` replaces service containers even if unchanged, but **not volumes**. It can interrupt active requests; do not run it casually while a workflow is processing. `--no-build --pull never` uses the images built locally in Section 9. If an image is missing, build it first.

Open the app:

```powershell
Start-Process "http://127.0.0.1:8080"
```

**Why:** Opens the running local frontend in your default browser. **Effect:** Local browser action only. **Success:** NovaMind login/portal page loads.

---

## 11. Verify containers, APIs, SQLite and migrations

### 11.1 Container/HTTP checks

```powershell
docker ps --filter "name=novamind-local"

(Invoke-WebRequest -Uri "http://127.0.0.1:8080" -UseBasicParsing).StatusCode
Invoke-RestMethod -Uri "http://127.0.0.1:8080/api/health"
```

**Why:** Verify both local containers, the frontend HTTP server and nginx-to-FastAPI `/api` proxy. **Effect:** Read-only localhost requests. **Success:** Containers show healthy, site returns HTTP `200`, health API responds successfully (often with a `status: ok` field).

If an authenticated API returns `401` anonymously, that is an **expected authorization boundary**, not automatically a problem.

### 11.2 Read-only SQLite integrity, historical cases and migration version

```powershell
@'
import sqlite3

uri = "file:/srv/state/novamind/novamind.sqlite?mode=ro"
with sqlite3.connect(uri, uri=True) as db:
    db.execute("PRAGMA query_only=ON")
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    cases = db.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0]
    communication_tables = sorted(t for t in tables if "communication" in t.lower())
    print("SQLite integrity:", integrity)
    print("Customer cases:", cases)
    print("Communication tables:", communication_tables)
    if "schema_migrations" in tables:
        print("Latest migration:", db.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()[0])
    else:
        print("Migration tracking: schema_migrations not found")
    print("DATA CHECK:", "PASS" if integrity == "ok" else "REVIEW REQUIRED")
'@ | docker exec -i novamind-local-backend-1 python -

if ($LASTEXITCODE -ne 0) { throw "SQLite verification failed" }
```

**Why:** Confirms the app opens the preserved database rather than a blank, unexpected one. **Effect:** Strictly read-only SQL checks. **Verified October 21 output:**

```text
SQLite integrity: ok
Customer cases: 10
Communication tables: ['case_customer_communications']
Latest migration: 17
DATA CHECK: PASS
```

On a **fresh** install, an initial case count of `0` is normal. For the preserved historical deployment, check that the count remains **at least the pre-deployment count** rather than hardcoding `10` forever. If migration does not report `17` or a later expected migration, **stop and inspect startup logs**; do not run a manual schema hack.

---

## 12. Verify Bedrock configuration and LangGraph responses WITHOUT spending on inference

### 12.1 Check resolved runtime configuration (safe fields only)

```powershell
docker exec novamind-local-backend-1 python -c "import os; keys=('NOVAMIND_AUTOMATIC_DISPATCHER','NOVAMIND_LLM_PROVIDER','AWS_REGION','BEDROCK_MODEL_ID'); [print(k,'=',os.getenv(k)) for k in keys]"
```

**Why:** Confirms the private dispatcher overlay and Bedrock overlay are active in the backend. **Effect:** Read-only; prints only these non-secret configuration fields, not credentials.

**Verified earlier:**

```text
NOVAMIND_AUTOMATIC_DISPATCHER = true
NOVAMIND_LLM_PROVIDER = bedrock
AWS_REGION = us-east-1
BEDROCK_MODEL_ID = us.amazon.nova-lite-v1:0
```

This does **not** call Bedrock and does not prove that credentials currently work. An STS call from Windows is likewise not proof of container-side credentials.

### 12.2 Offline MALFUNCTION classification + troubleshooting draft

**This command was executed successfully on the running backend before the communication UI change:**

```powershell
@'
from types import SimpleNamespace as NS
from uuid import uuid4
from backend.app.resolution_drafts import build_resolution_draft

case = NS(
    id=uuid4(),
    subject="NovaControl Input 1 - Technical Troubleshooting",
    description="The device intermittently stops responding for 3-5 seconds. Technical help only. No refund, return, or replacement."
)
order = NS(id=uuid4(), items=[NS(sku="INP-1")])
draft = build_resolution_draft(
    case, order,
    trusted_delivery_evidence=False,
    policy_id="standard-return",
    policy_version="1",
    message="Please provide technical troubleshooting assistance."
)
print("Issue type:", draft.issue_type.value)
print("Recommended decision:", draft.recommended_decision.value)
print("Missing evidence:", "; ".join(draft.missing_evidence))
print("Customer response:", draft.customer_facing_response)
assert draft.issue_type.value == "malfunction"
assert "delivery" not in " ".join(draft.missing_evidence).lower()
print("OFFLINE CHECK: PASS")
'@ | docker exec -i novamind-local-backend-1 python -
```

**Why:** Proves the local deterministic logic asks for technical diagnostics, not unrelated delivery evidence. **Effect:** No AWS calls, no SQLite writes, no real complaint submission. **Verified:** `Issue type: malfunction`, `Recommended decision: REQUEST_EVIDENCE`, `OFFLINE CHECK: PASS`.

### 12.3 Offline LangGraph final-result response check

**Also previously executed successfully:**

```powershell
@'
from types import SimpleNamespace as NS
from uuid import uuid4
from backend.app.agent import AgentRequest
from backend.app.graph_agent import GraphState, GraphResolutionAgent
from backend.app.resolution_drafts import build_resolution_draft

case_id = uuid4()
case = NS(
    id=case_id,
    subject="NovaControl Input 1 - Technical Troubleshooting",
    description="Device intermittently stops responding. Technical assistance only."
)
order = NS(id=uuid4(), items=[NS(sku="INP-1")])
draft = build_resolution_draft(
    case, order,
    trusted_delivery_evidence=False,
    policy_id="standard-return",
    policy_version="1",
    message="Please help troubleshoot the connection problem."
)
state = GraphState(
    request=AgentRequest(case_id=case_id, message="Technical troubleshooting only."),
    resolution_draft=draft
)
result = GraphResolutionAgent._result(state, ())
print("Agent result:", result.status)
print("Final customer response:", result.customer_response)
assert result.status == "INFORMATIONAL"
assert "technical issue" in result.customer_response.lower()
assert "delivery evidence" not in result.customer_response.lower()
assert "no refund" in result.customer_response.lower()
print("LANGGRAPH RESPONSE CHECK: PASS")
'@ | docker exec -i novamind-local-backend-1 python -
```

**Why:** Proves the result assembly uses the safe, relevant customer guidance rather than the old generic informational message. **Effect:** Read-only/in-memory test. **Verified:** `Agent result: INFORMATIONAL` and `LANGGRAPH RESPONSE CHECK: PASS`.

---

## 13. Verify authentication and ADMIN → CUSTOMER communication without a new AI call

This is the **actual UI workflow** tested successfully after migration v17. It requires no new complaint or model invocation.

1. Open <http://127.0.0.1:8080>, sign in as **ADMIN** using your existing credentials.
2. In **Cases**, select an existing case, e.g. **NovaControl Input 1 — Technical Troubleshooting Test 03** (`1ee5277c-5c2f-4fdc-a242-308dfa4ccc0b`).
3. Inspect **Grounded AI Draft → Review and send customer guidance**. An **older saved case may contain outdated shipping-evidence wording** because historical checkpoints are immutable. Do **not** assume old drafts will be recomputed by deploying new code.
4. **Do not click Send guidance again just to test rendering.** It creates a new immutable communication. Instead, inspect **Latest sent guidance**, already verified for Test 03 at **21 Oct 2026, 3:25:08 PM**.
5. Sign out; sign in as the **CUSTOMER who owns that same case**. Go to **My Support Requests** and open Test 03.
6. Confirm **Latest Support Update** matches the approved ADMIN message, shows its sent timestamp and displays the four diagnostic requests **as numbered list items**, separated from ordinary paragraphs.
7. Confirm the customer case remains `open` unless a separate formal decision subsequently changed it. The guidance send action must **not** create a refund/return/replacement proposal, trigger HITL approval, or perform a business action.
8. Do not expose ADMIN drafts, AI reasoning, internal citations or staff identity through the CUSTOMER view.

**Why:** Verifies role-based authentication, the existing immutable ADMIN-approved communication, customer-safe projection and improved frontend formatting. **Effect:** Read-only UI navigation when inspecting historical guidance. **Verified:** ADMIN displayed `Latest sent guidance`; CUSTOMER displayed `Latest Support Update` and its numbered steps; case status remained `open`.

**Security rules:** The communication send route is ADMIN-only, the latest communication read is case-authorized, exact idempotent retries should not create duplicate records, and conflicting reuse should be rejected. Sending a **new** message is a persistent append-only action and requires an intentional ADMIN click; don't do it in routine smoke checks.

**Distinguish workflows:** Completed informational/technical LangGraph runs are not necessarily in the **Reviews** queue; that queue is for pending HITL business-action proposals. Customer case status, dispatcher job status and AI workflow status are **different** state machines. A job showing `CLAIMED` does not necessarily mean the associated `workflow_runs` is still running.

---

## 14. OPTIONAL: One explicitly approved live Amazon Bedrock end-to-end test

**DISABLED BY DEFAULT. Do not execute this section unless you separately approve a new potentially billable AI test.** There is deliberately no unattended command here that submits a complaint or invokes Bedrock.

If approved later, the controlled procedure is:

1. Verify backend/frontend health, container-side credentials, Bedrock permission, ADMIN and CUSTOMER accounts, and that no earlier investigation is still processing.
2. Use a unique **synthetic** customer case (never reuse or replay a failed case). Confirm the customer is requesting *technical help only*, not a refund, return or replacement.
3. Submit **one** new complaint as CUSTOMER. With the automatic dispatcher enabled, this can invoke `us.amazon.nova-lite-v1:0` and **incur AWS charges**.
4. Inspect the new workflow read-only. Confirm provider `bedrock`, correct classification (`malfunction`), and actual workflow completion or accurately reported failure. Do not infer success from the dispatcher job's `CLAIMED` status alone.
5. ADMIN reviews the resulting draft; explicitly edits/sends safe customer guidance if appropriate. Do not send unreviewed AI text automatically.
6. CUSTOMER verifies the approved **Latest Support Update**. Ensure no unauthorized refund, replacement or payment action occurred.
7. Record case ID, observable workflow state, audit result and costs without exposing API credentials or customer details publicly.

**No Bedrock call was made while developing this guide.** The earlier historical live Test 03 investigation ran before the most recent improvements, so don't represent it as a new final-build live acceptance test.

---

## 15. Logs, troubleshooting and safe code redeployment

### Read-only diagnosis

```powershell
docker ps -a --filter "name=novamind-local"

docker logs --tail 100 novamind-local-backend-1
docker logs --tail 100 novamind-local-frontend-1

# Follow backend logs until Ctrl+C; does not stop the backend.
docker logs -f novamind-local-backend-1
```

**Why:** Detect startup/migration errors, API issues and model/dispatcher exceptions. **Effect:** Read-only. **Success:** Normal startup and health messages, no persistent fatal errors. Redact logs if sharing them: they may include case IDs, customer text, paths or sensitive metadata.

If a command fails, capture its exact error and **do not** respond by deleting volumes, resetting SQLite, replaying dispatcher jobs or repeatedly invoking Bedrock.

### Frontend-only code change — actual tested deployment pattern

```powershell
cd E:\codex-projetc\NovaMind-local-smoke-test

docker compose -f compose.yaml build frontend

# Assumes the Section 5 $private/environment setup is active.
docker compose `
  -f compose.yaml `
  -f compose.persistence.yaml `
  -f compose.auth.yaml `
  -f compose.bedrock.yaml `
  -f "$private\compose.aws-credentials.yaml" `
  -f "$private\compose.dispatcher.yaml" `
  up -d --no-deps --no-build --pull never --force-recreate --wait frontend
```

**Why:** Deploy the frontend without interrupting the backend/database. **Effect:** Rebuilds and recreates **only** frontend; preserves backend, SQLite and AWS settings. **Verified output:** `Container novamind-local-frontend-1 Healthy`. Reload Chrome with **Ctrl+Shift+R** to avoid old cached assets. This was the exact pattern used to deploy point-wise guidance formatting.

### Backend-only change — tested deployment pattern

```powershell
cd E:\codex-projetc\NovaMind-local-smoke-test

docker compose -f compose.yaml build backend

# Assumes the Section 5 $private/environment setup is active.
docker compose `
  -f compose.yaml `
  -f compose.persistence.yaml `
  -f compose.auth.yaml `
  -f compose.bedrock.yaml `
  -f "$private\compose.aws-credentials.yaml" `
  -f "$private\compose.dispatcher.yaml" `
  up -d --no-deps --no-build --pull never --force-recreate --wait backend
```

**Why:** Deploy the backend without a frontend rebuild. **Effect:** Recreates backend and may perform SQLite migrations. **Back up before doing so**, check no active workflows. **Previously verified output:** `Container novamind-local-backend-1 Healthy`.

### Restart without rebuilding source

```powershell
docker restart novamind-local-backend-1 novamind-local-frontend-1
```

**Why:** Restart *existing* containers after a transient runtime problem; this **does not** load newly built code. **Effect:** Interrupts requests, keeps named volumes. **Success:** Containers start; then confirm health with `docker ps`.

**Important:** Do not use restart as a routine fix for in-flight LangGraph/dispatcher problems. Inspect state first.

---

## 16. Stop / start / remove application safely

### Stop, leaving existing containers available for later start

```powershell
docker stop novamind-local-backend-1 novamind-local-frontend-1
```

**Effect:** Stops only those containers; preserves named SQLite volume and built images. **Success:** Docker prints the stopped container names.

### Start the same existing containers (not a new deployment)

```powershell
docker start novamind-local-backend-1 novamind-local-frontend-1
```

**Effect:** Starts existing containers with their saved settings. **Success:** Container names printed; health eventually returns `healthy`. This does **not** apply newly changed Compose environment or rebuilt images.

### Remove Compose containers/networks, but keep historical SQLite data

```powershell
# In the same PowerShell session after defining $composeArgs in Section 6:
docker compose @composeArgs down
```

**Effect:** Removes only the project containers and network; **named volumes remain**. **Success:** Containers removed, historical data available when recreated with the persistence overlay. Do **not** append `-v`.

---

## 17. SQLite disaster recovery — separate manual operation

> **DANGER: Restoring an old backup REPLACES the current live database contents and can lose newer cases, approved communications and audit records. Recovery is NOT part of normal deployment. Do not run the restore commands unless there is a genuine recovery incident, you have made a fresh copy of the current data, and you intentionally approve that rollback. The steps below are a guarded, standard SQLite/Docker restore procedure; unlike the backup and migration checks above, a full restore was NOT executed on your live project in our conversation. Test it on a disposable copy before relying on it.**

### 17.1 Verify the proposed backup independently — read-only

Select your known-good backup explicitly. Example:

```powershell
$restoreFile = Join-Path $env:LOCALAPPDATA "NovaMindAI\backups\novamind-before-v17-20261021-151319.sqlite"

if (-not (Test-Path -LiteralPath $restoreFile -PathType Leaf)) {
    throw "Restore backup does not exist"
}
if ((Get-Item -LiteralPath $restoreFile).Length -lt 4096) {
    throw "Restore backup is empty/too small"
}

py -3.12 -c "import sqlite3,sys,pathlib; uri=pathlib.Path(sys.argv[1]).resolve().as_uri()+'?mode=ro'; db=sqlite3.connect(uri,uri=True); check=db.execute('PRAGMA integrity_check').fetchone()[0]; table=db.execute('SELECT name FROM sqlite_master WHERE name=?',('support_cases',)).fetchone(); print('Integrity:',check); print('Support cases table:',bool(table)); print('Cases:',db.execute('SELECT COUNT(*) FROM support_cases').fetchone()[0] if table else 'MISSING'); db.close(); sys.exit(0 if check=='ok' and table else 1)" "$restoreFile"

if ($LASTEXITCODE -ne 0) { throw "Restore source verification FAILED" }
```

**Effect:** Read-only; does not create a missing backup. **Success:** `Integrity: ok`, `Support cases table: True`, and case count as expected. This uses the same read-only SQLite verification pattern that passed in Section 4.3.

### 17.2 Discover the exact mounted SQLite volume while backend still exists — read-only

**Do this BEFORE bringing Compose down.** It does not assume that the Docker Engine volume name is exactly `novamind_sqlite` or that its mount destination has a fixed parent.

```powershell
$databasePath = "/srv/state/novamind/novamind.sqlite"
$inspect = docker inspect novamind-local-backend-1 | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $inspect) { throw "Backend container not found" }

$mount = $inspect[0].Mounts |
    Where-Object {
        $_.Type -eq "volume" -and
        $databasePath.StartsWith($_.Destination.TrimEnd('/') + '/')
    } |
    Sort-Object @{Expression={$_.Destination.Length};Descending=$true} |
    Select-Object -First 1

if (-not $mount) { throw "SQLite database is not in an expected named volume; STOP" }

$sqliteVolume = $mount.Name
$sqliteRelativePath = $databasePath.Substring($mount.Destination.TrimEnd('/').Length).TrimStart('/')

Write-Host "Volume:" $sqliteVolume
Write-Host "Database relative path:" $sqliteRelativePath

docker volume inspect $sqliteVolume
```

**Why:** Derives the *actual* named volume and the path of the database *inside that volume* from Docker metadata. **Effect:** Read-only. **Success:** Real named volume + sensible relative database path shown. If this does not match expectations, **stop**.

### 17.3 Make a new verified backup of the current database

Before overwriting anything, run **all of Section 4** again and confirm a **fresh, different Windows backup** exists. This preserves records created *after* the older backup date. If you cannot make and verify the current backup, do not attempt a restore without a documented disaster-recovery decision.

### 17.4 Stop all writers and confirm volume is no longer used

```powershell
# Requires Section 5 and 6 configuration in this PowerShell session.
docker compose @composeArgs down
if ($LASTEXITCODE -ne 0) { throw "Failed to stop app; STOP" }

# Must have NO running containers attached to this volume.
docker ps --filter "volume=$sqliteVolume"
```

**Why:** SQLite must not be overwritten while a writer is active. **Effect:** Stops/removes NovaMind containers but preserves volumes. **Success:** `docker ps --filter volume=...` lists no running containers. If anything is listed, stop and investigate; do not force a restore.

### 17.5 Guarded volume restoration — only after an explicit separate decision

This uses the existing local backend image as a temporary Python runtime. It mounts the chosen Windows backup directory **read-only** and your actual SQLite volume read/write. It **refuses** to run if the existing DB is missing or if active-looking `-wal`/`-shm` sidecar files exist, rather than silently deleting journal data. No other volume is touched.

```powershell
# Confirm the exact backup and volume displayed above before running.
$restoreDir = Split-Path -Parent $restoreFile
$restoreName = Split-Path -Leaf $restoreFile

@'
from pathlib import Path
import sqlite3, sys

relative_db, backup_name = sys.argv[1:3]
source = Path('/restore-backup') / backup_name
target = Path('/restore-volume') / relative_db

if not source.is_file() or not target.is_file():
    raise SystemExit('STOP: backup or existing target database missing')
if any(Path(str(target) + suffix).exists() for suffix in ('-wal', '-shm')):
    raise SystemExit('STOP: SQLite WAL/SHM sidecars found; review before restoring')

with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
    if src.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit('STOP: invalid source backup')
    with sqlite3.connect(str(target)) as dst:
        src.backup(dst)

with sqlite3.connect(target.as_uri() + '?mode=ro', uri=True) as db:
    check = db.execute('PRAGMA integrity_check').fetchone()[0]
    print('Restored database integrity:', check)
    if check != 'ok':
        raise SystemExit('STOP: restored database integrity failed')
print('RESTORE CONTENTS VERIFIED; review schema before restarting')
'@ | docker run -i --rm `
  --user 0:0 `
  --mount "type=volume,source=$sqliteVolume,target=/restore-volume" `
  --mount "type=bind,source=$restoreDir,target=/restore-backup,readonly" `
  --entrypoint python `
  novamind-ai-customer-resolution-backend:local `
  - $sqliteRelativePath $restoreName

if ($LASTEXITCODE -ne 0) { throw "Restore aborted or failed; DO NOT start the app yet" }
```

**Effect:** **Destructively rewrites the existing SQLite DB's contents with the selected older snapshot.** It does not delete the volume, but newer database records will no longer be present there. This exact recovery path has **not** been live-tested on this project; do not treat it as a verified operational runbook until exercised against a disposable cloned volume. If the sidecar check fails, stop and inspect instead of manually deleting WAL files. The image and volume must already exist; this is not a first-install import recipe.

**After an approved restore:** Reapply Section 10 to start the app, then Section 11 to verify integrity, migrations and expected historical cases. An older pre-v17 backup may be migrated forward to v17 at startup. If startup/migrations fail, preserve logs and both backup copies before taking another action.

**Alternative when uncertain:** Ask for guided, case-by-case restore assistance rather than forcing a database write. Never copy a raw `.sqlite` over an actively running database.

---

## 18. Troubleshooting, expected states and deployment checklist

| Symptom | Safe first action | What NOT to do |
|---|---|---|
| Docker engine unavailable | Launch Docker Desktop; run `docker info` | Delete images/volumes |
| Missing private auth/dispatcher/AWS override | Check paths in Section 5; restore from your private backup | Print/commit credentials or invent new ones |
| `docker compose config` fails | Verify six file paths and PowerShell variables | Remove database to fix YAML |
| Build fails | Read full Docker build error; inspect disk/network | Delete unrelated project images |
| Backend unhealthy | `docker logs --tail 100 novamind-local-backend-1` | Repeatedly restart active workflows |
| Frontend page stale after rebuild | `Ctrl+Shift+R`; verify frontend was **recreated**, not merely built | Modify SQLite |
| Frontend loads but `/api/health` fails | Check backend and nginx/Compose health/logs | Send a new paid AI request as a test |
| API returns `401` anonymously | Check whether route is private; auth boundary may be correct | Disable authorization |
| Wrong CUSTOMER case inaccessible | Confirm signed-in customer owns the case | Bypass ownership checks |
| `schema_migrations` not at expected version | Check startup migration logs; verify volume attachment | Hand-edit schema/version |
| Case count decreased unexpectedly | Stop and inspect actual volume, earlier verified backups | Continue with additional writes |
| Backend says provider `bedrock` but inference fails | Check IAM, container-mounted credentials, region/model, logs | Re-submit complaints repeatedly |
| ADMIN Reviews queue empty after informational result | Check case and AI run status separately; may be expected | Force HITL proposals |
| Old draft mentions shipping/delivery for malfunction | Recognize persisted **historical** draft; ADMIN edits guidance before sending | Automatically send obsolete draft |
| CUSTOMER guidance in one paragraph | Confirm new frontend image/container, hard refresh | Resend immutable communication unnecessarily |
| Dispatcher job `CLAIMED` | Inspect associated workflow status/checkpoint; status semantics differ | Assume it failed or replay it automatically |
| Backup copies as 0 bytes | Use Section 4 snapshot/copy/read-only validation | Trust `Integrity: ok` on an empty newly-created file |

### Definition of success

- [ ] Correct Windows project directory and prerequisites.
- [ ] Existing SQLite backed up **outside Docker**, nonzero and `Integrity: ok` (or confirmed brand-new install).
- [ ] Private auth and AWS/dispatcher overlay files exist; secret values never printed/committed.
- [ ] AWS profile `novamind` and region `us-east-1` checked without model invocation.
- [ ] Six Compose overrides validated in the same order.
- [ ] Only NovaMind containers removed, **without** `-v`; optional removal of only two NovaMind image tags.
- [ ] Both images built from local source successfully.
- [ ] Both containers report `Healthy`.
- [ ] Frontend and `/api/health` respond.
- [ ] SQLite integrity `ok`; expected case count and migration version confirmed.
- [ ] `NOVAMIND_AUTOMATIC_DISPATCHER=true`, provider `bedrock`, model `us.amazon.nova-lite-v1:0`.
- [ ] Offline troubleshooting and LangGraph final-response checks pass.
- [ ] ADMIN and CUSTOMER authenticate with correct permissions.
- [ ] Existing ADMIN-approved guidance appears on CUSTOMER's **Latest Support Update** as paragraphs and point-wise numbered items.
- [ ] Existing case status is unchanged by sending guidance; no unauthorized financial/business action is performed.
- [ ] No new Bedrock inference occurred unless **explicitly** approved.

### Final operational notes

- **Local only:** This procedure does **not** deploy to AWS ECS/Fargate or create AWS infrastructure. AWS usage here is limited to optional Bedrock inference from local Docker.
- **Data durability:** A named SQLite volume persists through ordinary container recreation and `docker compose down` without `-v`; it is not a substitute for tested, off-Docker backups.
- **Different endpoints/duties:** `Send guidance` appends immutable ADMIN-approved customer communication and **does not** change the formal case decision/status. Do not confuse it with `Request more evidence`, refund/return/replacement actions or HITL proposal approval.
- **First-time install limitation:** The exact private authentication JSON and private AWS/dispatcher YAML contain user-specific secret/configuration details not included in this document. The guide correctly points to the verified existing private files; it cannot securely recreate those files from scratch without their trusted originals or separate initialization instructions.
- **Verification scope:** The deployment, v17 migration, 10-case preservation, offline LangGraph checks, ADMIN-to-CUSTOMER communication and frontend list formatting were actually observed in the conversation. Optional AWS describe and manual disaster restoration are documented as **additional/untested diagnostics**, not falsely claimed as executed.

**End of guide.**

#-----------------------------------------------------------------------------------------------

- Step 1 — Check the Actual Bedrock and Dispatcher Configuration

```powershell
docker exec novamind-local-backend-1 python -c "import os; keys=['NOVAMIND_AUTOMATIC_DISPATCHER','NOVAMIND_LLM_PROVIDER','AWS_REGION','BEDROCK_MODEL_ID','NOVAMIND_OFFLINE_TESTING']; [print(k,'=',os.getenv(k,'Not set')) for k in keys]"
```

- Step 2 — Verify AWS Credentials Inside Docker

```powershell
docker exec novamind-local-backend-1 python -c "import boto3; sts=boto3.client('sts',region_name='us-east-1'); identity=sts.get_caller_identity(); print('AWS Connection: SUCCESS'); print('AWS Account:',identity['Account']); print('IAM Identity:',identity['Arn'])"
```
- Step 3 — Verify Amazon Nova Lite Inference Profile

```powershell
docker exec novamind-local-backend-1 python -c "import boto3; c=boto3.client('bedrock',region_name='us-east-1'); r=c.get_inference_profile(inferenceProfileIdentifier='us.amazon.nova-lite-v1:0'); print('Inference Profile:',r['inferenceProfileName']); print('Status:',r['status']); print('Model ID:',r['inferenceProfileId'])"
```
- Step 4 — Verify Authentication (Auth Is Present)

```powershell
$private = Join-Path $env:LOCALAPPDATA "NovaMindAI\private"

$authFile = Join-Path $private "novamind-auth.json"

Write-Host "Authentication file exists:" (Test-Path $authFile)

Write-Host "Authentication Compose file exists:" (Test-Path "E:\codex-projetc\NovaMind-local-smoke-test\compose.auth.yaml")
```

-

```powershell

```


```powershell

```


```powershell

```


```powershell

```


```powershell

```


```powershell

```



```powershell

```


```powershell

```


```powershell

```

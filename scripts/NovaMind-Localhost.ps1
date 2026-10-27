# NovaMind AI — Localhost Docker Compose helper
# Built by Aamir | Windows PowerShell 5.1+ / PowerShell 7+
# DEFAULT IS READ-ONLY. No AWS API calls and no automatic model invocations.
# Only explicit -Action Start or -Action Stop change container state.
[CmdletBinding()]
param(
    [ValidateSet('Check','Start','Status','Smoke','Logs','Stop')]
    [string]$Action = 'Check',

    [ValidateSet('Offline','Bedrock')]
    [string]$Mode = 'Offline',

    [string]$ProjectRoot = 'E:\codex-projetc\NovaMind-local-smoke-test',

    [ValidateRange(1024,65535)]
    [int]$Port = 8080,

    [switch]$Demo,
    [switch]$Stateless,
    [string]$AuthFile = '',
    # Adds only the server-side capability. The durable control remains MANUAL
    # until an ADMIN changes it through the authenticated API/UI.
    [switch]$EnableAutomaticDispatcher,
    [string]$DispatcherUserId = '',
    [string]$Region = 'us-east-1',
    [string]$BedrockModelId = 'us.amazon.nova-lite-v1:0',

    # Explicit approval switches for disruptive or billable modes.
    [switch]$AllowRecreate,
    [switch]$AllowPaidAI,
    [switch]$BedrockRuntimeReady
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Say([string]$Message) { Write-Host ('[NovaMind] ' + $Message) -ForegroundColor Cyan }
function Require-File([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required file is missing: $Path"
    }
}
function Require-LocalDispatcherIdentity([string]$Path, [string]$UserId) {
    # Read only the selected identity fields. Tokens are never emitted or retained.
    try {
        $records = @(Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json)
        $matches = @($records | Where-Object {
            $_.identity -and [string]$_.identity.user_id -ceq $UserId -and
            (@('ADMIN', 'REVIEWER') -ccontains [string]$_.identity.role)
        })
        if ($matches.Count -ne 1) { throw 'invalid dispatcher identity' }
    } catch {
        throw 'AUTO dispatcher requires DispatcherUserId to identify exactly one ADMIN or REVIEWER in the local auth file.'
    }
}
function Run-Compose([string[]]$Arguments) {
    & docker compose @script:ComposeArgs @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose command failed (exit $LASTEXITCODE)." }
}
function Run-Smoke([int]$ListenPort) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -B scripts/container_smoke.py --port $ListenPort
        if ($LASTEXITCODE -ne 0) { throw 'Localhost smoke tests failed. Check container health and logs.' }
        return
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        & python -B scripts/container_smoke.py --port $ListenPort
        if ($LASTEXITCODE -ne 0) { throw 'Localhost smoke tests failed. Check container health and logs.' }
        return
    }
    Say 'Python is not available; running basic HTTP health checks instead.'
    $front = Invoke-WebRequest -UseBasicParsing -TimeoutSec 6 "http://127.0.0.1:$ListenPort/healthz"
    $api = Invoke-RestMethod -TimeoutSec 6 "http://127.0.0.1:$ListenPort/api/health"
    if ($front.StatusCode -ne 200 -or $api.status -ne 'ok') { throw 'HTTP health checks failed.' }
    Say 'Basic frontend and backend HTTP health checks passed.'
}

$oldPort = [Environment]::GetEnvironmentVariable('NOVAMIND_PORT', 'Process')
$oldAuth = [Environment]::GetEnvironmentVariable('NOVAMIND_AUTH_SOURCE', 'Process')
$oldRegion = [Environment]::GetEnvironmentVariable('AWS_REGION', 'Process')
$oldModel = [Environment]::GetEnvironmentVariable('BEDROCK_MODEL_ID', 'Process')
$oldDispatcherUserId = [Environment]::GetEnvironmentVariable('NOVAMIND_DISPATCHER_USER_ID', 'Process')
$pushed = $false
try {
    if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
        throw "Project folder not found: $ProjectRoot"
    }
    Push-Location -LiteralPath $ProjectRoot
    $pushed = $true

    Require-File 'compose.yaml'
    Require-File '.env.container.example'
    Require-File 'backend/Dockerfile'
    Require-File 'frontend/Dockerfile'
    Require-File 'scripts/container_smoke.py'

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker command not found. Install/start Docker Desktop first.'
    }
    & docker info --format '{{.ServerVersion}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker engine is not running. Start Docker Desktop.' }
    & docker compose version
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose V2 is unavailable.' }

    # Environment assignments exist only in this PowerShell process and its children.
    $env:NOVAMIND_PORT = [string]$Port
    $fileList = @('compose.yaml')
    if (-not $Stateless) { $fileList += 'compose.persistence.yaml' }

    $authSource = [Environment]::GetEnvironmentVariable('NOVAMIND_AUTH_SOURCE', 'Process')
    if ($AuthFile) { $authSource = $AuthFile }
    if ($authSource) {
        if (-not [IO.Path]::IsPathRooted($authSource)) {
            throw 'AuthFile/NOVAMIND_AUTH_SOURCE must be an absolute file path.'
        }
        if (-not (Test-Path -LiteralPath $authSource -PathType Leaf)) {
            throw 'Auth JSON file does not exist. Do not add bearer credentials to Git.'
        }
        $env:NOVAMIND_AUTH_SOURCE = $authSource
        $fileList += 'compose.auth.yaml'
    }
    if ($EnableAutomaticDispatcher) {
        if ($Mode -ne 'Offline') {
            throw 'The localhost AUTO helper supports only -Mode Offline. Bedrock AUTO requires a separately reviewed configuration.'
        }
        if ($Stateless) {
            throw 'AUTO dispatcher requires durable SQLite persistence; remove -Stateless.'
        }
        if (-not $authSource) {
            throw 'AUTO dispatcher requires -AuthFile or NOVAMIND_AUTH_SOURCE for local staff identity verification.'
        }
        if ($DispatcherUserId -cnotmatch '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') {
            throw 'DispatcherUserId must be a canonical lowercase UUID.'
        }
        Require-LocalDispatcherIdentity $authSource $DispatcherUserId
        $env:NOVAMIND_DISPATCHER_USER_ID = $DispatcherUserId
        $fileList += 'compose.auto.yaml'
    }

    if ($Mode -eq 'Offline') {
        $fileList += 'compose.offline.yaml'
        if ($Demo) {
            if (-not $authSource) { throw 'Offline demo needs a private auth JSON with exactly one CUSTOMER identity.' }
            $fileList += 'compose.demo.yaml'
        }
    }
    else {
        $env:AWS_REGION = $Region
        $env:BEDROCK_MODEL_ID = $BedrockModelId
        $fileList += 'compose.bedrock.yaml'
        if ($Demo) {
            if (-not $authSource) { throw 'Bedrock demo needs a private auth JSON with exactly one CUSTOMER identity.' }
            $fileList += 'compose.bedrock.demo.yaml'
        }
    }
    foreach ($file in $fileList) { Require-File $file }
    $script:ComposeArgs = @('--env-file', '.env.container.example')
    foreach ($file in $fileList) { $script:ComposeArgs += @('-f', $file) }

    Say "Action: $Action | Provider: $Mode | localhost port: $Port"
    Say ('Compose files: ' + ($fileList -join ', '))
    Say ('SQLite persistence: ' + $(if ($Stateless) { 'OFF (process state may be lost)' } else { 'ON; named volume novamind_sqlite by default' }))
    Say ('Host authentication: ' + $(if ($authSource) { 'configured via private file' } else { 'NOT configured; protected API routes return 401' }))
    Say ('Automatic dispatcher capability: ' + $(if ($EnableAutomaticDispatcher) { 'ON; persisted mode remains MANUAL' } else { 'OFF' }))
    Run-Compose @('config','--quiet')
    Say 'Compose configuration valid.'

    switch ($Action) {
        'Check' {
            Say 'CHECK complete: no containers were built, started, stopped or recreated.'
            Say 'For a new offline start, use -Action Start. Existing containers need -AllowRecreate.'
        }
        'Status' {
            Run-Compose @('ps','-a')
        }
        'Smoke' {
            Run-Smoke $Port
        }
        'Logs' {
            Say 'Showing latest 80 backend/frontend log lines; redact any personal data before sharing.'
            Run-Compose @('logs','--tail','80','backend','frontend')
        }
        'Stop' {
            Say 'Explicit stop requested. Volumes are NOT deleted.'
            Run-Compose @('stop')
        }
        'Start' {
            if ($Mode -eq 'Bedrock') {
                if (-not $AllowPaidAI -or -not $BedrockRuntimeReady) {
                    throw 'Bedrock start requires BOTH -AllowPaidAI and -BedrockRuntimeReady. Compose does not automatically share host AWS profile credentials with the container.'
                }
                Say 'WARNING: future Bedrock model requests can incur AWS charges. No model request is part of this script.'
            }
            if ($Stateless) {
                Say 'WARNING: stateless startup may lose process-held workflows after recreation.'
            }
            # Block unintended restarts of a project that already has containers.
            $existing = @(& docker ps -a --filter 'label=com.docker.compose.project=novamind-local' --format '{{.ID}}')
            if ($LASTEXITCODE -ne 0) { throw 'Could not inspect existing project containers.' }
            if ($existing.Count -gt 0 -and $existing[0] -and -not $AllowRecreate) {
                throw 'Existing NovaMind containers found. Nothing changed. Review backups/state, then explicitly use -AllowRecreate if you intend to update/restart them.'
            }
            Say 'Building and starting local project. Docker may download base images and dependencies.'
            Run-Compose @('up','-d','--build','--wait','--wait-timeout','180')
            Say "Local URL: http://127.0.0.1:$Port"
            Run-Smoke $Port
            Say 'Container startup and non-model localhost smoke checks passed.'
        }
    }
}
finally {
    if ($pushed) { Pop-Location }
    [Environment]::SetEnvironmentVariable('NOVAMIND_PORT', $oldPort, 'Process')
    [Environment]::SetEnvironmentVariable('NOVAMIND_AUTH_SOURCE', $oldAuth, 'Process')
    [Environment]::SetEnvironmentVariable('AWS_REGION', $oldRegion, 'Process')
    [Environment]::SetEnvironmentVariable('BEDROCK_MODEL_ID', $oldModel, 'Process')
    [Environment]::SetEnvironmentVariable('NOVAMIND_DISPATCHER_USER_ID', $oldDispatcherUserId, 'Process')
}

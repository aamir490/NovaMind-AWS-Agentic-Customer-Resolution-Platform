# Offline regression tests for the Windows PowerShell 5.1 bootstrap execution-role workflow.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspace = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $workspace 'scripts/New-NovaMindBootstrapExecutionRoleChangeSet.ps1'
$templatePath = Join-Path $workspace 'infrastructure/novamind-rds-bootstrap-support.template.json'
$ecrArn = 'arn:aws:ecr:us-east-1:357001292388:repository/novamind-crp-backend-dev'

$tokens = $null
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors) > $null
if ($parseErrors.Count -ne 0) { throw 'Bootstrap execution-role script did not parse.' }
$source = Get-Content -LiteralPath $scriptPath -Raw
if ($source -match 'System\.Text\.Json') { throw 'Workflow must not depend on System.Text.Json.' }

function Invoke-Plan([string]$json) {
    $file = New-TemporaryFile
    try {
        [IO.File]::WriteAllText($file, $json, [Text.UTF8Encoding]::new($false))
        return @(& $scriptPath -StackName novamind-rds-bootstrap-dev `
            -ChangeSetName test-bootstrap-execution -ExistingParametersPath $file `
            -BootstrapEcrRepositoryArn $ecrArn -TemplatePath $templatePath 2>&1)
    } finally {
        Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue
    }
}

$single = Invoke-Plan '[{"ParameterKey":"ProjectPrefix","ParameterValue":"novamind-crp"}]'
if (($single -join "`n") -notmatch 'PARAMETER_JSON_COUNT=5') { throw 'Single-item JSON was not expanded into individual parameters.' }
if (($single -join "`n") -notmatch 'PARAMETER_USE_PREVIOUS_COUNT=1') { throw 'Single existing parameter was not preserved with UsePreviousValue=true.' }
if (($single -join "`n") -notmatch 'PARAMETER_JSON_KEYS=.*ProjectPrefix.*CreateBootstrapExecutionRole.*BootstrapEcrRepositoryArn') {
    throw 'Single-item parameter comparison did not preserve and append individual keys.'
}

$multi = Invoke-Plan '[{"ParameterKey":"ProjectPrefix","ParameterValue":"novamind-crp"},{"ParameterKey":"Environment","ParameterValue":"dev"}]'
if (($multi -join "`n") -notmatch 'PARAMETER_JSON_COUNT=6') { throw 'Multi-item JSON parameter count was incorrect.' }
if (($multi -join "`n") -notmatch 'PARAMETER_USE_PREVIOUS_COUNT=2') { throw 'Multiple existing parameters were not preserved individually.' }
if (($multi -join "`n") -match 'ProjectPrefix,Environment') { throw 'Parameter names were treated as one combined key.' }

$replacement = Invoke-Plan '[{"ParameterKey":"ProjectPrefix","ParameterValue":"novamind-crp"},{"ParameterKey":"CreateBootstrapExecutionRole","ParameterValue":"false"}]'
if (($replacement -join "`n") -notmatch 'PARAMETER_JSON_COUNT=5') { throw 'Existing execution-role parameter was duplicated instead of replaced.' }
if ((($replacement -join "`n") -split 'CreateBootstrapExecutionRole').Count -ne 2) { throw 'Execution-role parameter appeared more than once.' }

$invalidRootRejected = $false
try { Invoke-Plan '{"ParameterKey":"ProjectPrefix","ParameterValue":"novamind-crp"}' | Out-Null } catch { $invalidRootRejected = $true }
if (-not $invalidRootRejected) { throw 'A non-array parameter root was not rejected.' }

Write-Output 'BOOTSTRAP_EXECUTION_ROLE_CHANGESET_TEST_PASS'

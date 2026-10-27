<#
.SYNOPSIS
  Updates an existing public Cognito SPA app client for Hosted UI PKCE.
.DESCRIPTION
  This never creates a user pool or app client. The selected client must already
  be public (no client secret); Cognito cannot safely convert a confidential
  client into a public SPA client in place. Default mode performs no AWS call.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[a-z]{2}-[a-z]+-[0-9]_[A-Za-z0-9]+$')] [string]$UserPoolId,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_-]{1,128}$')] [string]$ClientId,
    [Parameter(Mandatory)] [ValidatePattern('^https://[^/?#]+(?:/[^?#]*)?$')] [string]$CallbackUri,
    [Parameter(Mandatory)] [ValidatePattern('^https://[^/?#]+(?:/[^?#]*)?$')] [string]$LogoutUri,
    [ValidatePattern('^[a-z]{2}(?:-[a-z0-9]+){1,3}-[0-9]{1,2}$')] [string]$Region = 'us-east-1',
    [ValidateNotNullOrEmpty()] [string]$ProfileName = 'novamind',
    [switch]$Apply
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$env:AWS_CLI_AUTO_PROMPT = 'off'
$env:AWS_EC2_METADATA_DISABLED = 'true'

function Test-CanonicalHttpsUri {
    param([string]$Value, [string]$Name)
    try { $uri = [Uri]$Value } catch { throw "Invalid $Name." }
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne 'https' -or -not $uri.Host -or $uri.UserInfo -or
        $uri.Query -or $uri.Fragment -or $uri.Port -ne 443) {
        throw "Invalid $Name."
    }
}

Test-CanonicalHttpsUri $CallbackUri 'callback URI'
Test-CanonicalHttpsUri $LogoutUri 'logout URI'
if ($CallbackUri -ne $LogoutUri) {
    throw 'Use the same canonical CloudFront HTTPS callback and logout URI for this SPA release.'
}
if (-not $Apply) {
    Write-Output "Plan only: would configure authorization-code + PKCE on existing Cognito client $ClientId."
    Write-Output 'No Cognito API call was made. Confirm the client has no secret before -Apply.'
    return
}
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw 'AWS CLI is required for -Apply.' }
$client = (& aws --profile $ProfileName --region $Region cognito-idp describe-user-pool-client `
    --user-pool-id $UserPoolId --client-id $ClientId --query 'UserPoolClient.{Secret:ClientSecret,Allowed:AllowedOAuthFlowsUserPoolClient}' `
    --output json --no-cli-pager 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the selected Cognito app client.' }
try { $clientInfo = $client | ConvertFrom-Json -ErrorAction Stop } catch { throw 'Cognito client metadata was invalid.' }
if ($clientInfo.Secret) { throw 'The selected Cognito client has a secret; create and explicitly approve a separate public SPA client.' }

& aws --profile $ProfileName --region $Region cognito-idp update-user-pool-client `
    --user-pool-id $UserPoolId --client-id $ClientId --allowed-o-auth-flows-user-pool-client `
    --allowed-o-auth-flows code --allowed-o-auth-scopes openid `
    --supported-identity-providers COGNITO --callback-urls $CallbackUri --logout-urls $LogoutUri `
    --prevent-user-existence-errors ENABLED --no-cli-pager 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Cognito SPA client update failed.' }
Write-Output 'Cognito public SPA client updated for authorization code plus PKCE.'

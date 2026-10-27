<#
.SYNOPSIS
  NovaMind CRP - safe AWS VPC/subnet route audit, read-only.
.DESCRIPTION
  Companion to NovaMind-AWS-Preflight.ps1, focused on the next networking step.
  Queries STS and EC2 describe APIs only. No local file writes, no Docker
  commands, no IAM/Secrets Manager reads, no model inference, no AWS changes.
  Explicit subnet-to-route-table associations take priority; otherwise the VPC
  main route table applies. A default route is not proof of end-to-end access.
.EXAMPLE
  & "$env:USERPROFILE\Downloads\NovaMind-CRP-Network-Readiness-ReadOnly.ps1"
#>
[CmdletBinding()]
param(
    [string]$ProfileName = 'novamind',
    [string]$Region = 'us-east-1',
    [ValidatePattern('^[0-9]{12}$')]
    [string]$ExpectedAccountId = '357001292388',
    [ValidatePattern('^[a-z0-9-]+$')]
    [string]$ProjectPrefix = 'novamind-crp',
    [ValidatePattern('^[a-z0-9-]+$')]
    [string]$Environment = 'dev'
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Get-PropertyValue {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Read-AwsJson {
    param([string[]]$ServiceArgs)
    $cliArgs = @('--profile', $ProfileName, '--region', $Region) + $ServiceArgs +
        @('--output', 'json', '--no-cli-pager', '--cli-connect-timeout', '5', '--cli-read-timeout', '20')
    # Discard raw errors; never accidentally print credentials or local paths.
    $raw = & aws @cliArgs 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw ('AWS read failed: {0}. Check profile, permissions and network.' -f ($ServiceArgs -join ' '))
    }
    $json = ($raw | Out-String).Trim()
    if ([string]::IsNullOrWhiteSpace($json)) {
        throw ('AWS returned an empty response: {0}.' -f ($ServiceArgs -join ' '))
    }
    return (ConvertFrom-Json -InputObject $json -ErrorAction Stop)
}

function Get-TagValue {
    param($Tags, [string]$Key)
    foreach ($tag in @($Tags)) {
        if ($null -ne $tag -and (Get-PropertyValue $tag 'Key') -eq $Key) {
            return [string](Get-PropertyValue $tag 'Value')
        }
    }
    return ''
}

try {
    if ($null -eq (Get-Command aws -ErrorAction SilentlyContinue)) {
        throw 'AWS CLI not found. Install/configure it before running this read-only check.'
    }

    Write-Host 'NOVAMIND CRP - NETWORK READINESS (READ-ONLY)' -ForegroundColor Cyan
    Write-Host ("Profile: {0} | Region: {1} | Naming: {2}-<resource>-{3}" -f $ProfileName,$Region,$ProjectPrefix,$Environment)

    $identity = Read-AwsJson @('sts', 'get-caller-identity')
    if ([string]$identity.Account -ne $ExpectedAccountId) {
        throw 'ACCOUNT MISMATCH. No further AWS inventory queries performed.'
    }
    Write-Host ('Account match: ********{0}' -f $ExpectedAccountId.Substring(8)) -ForegroundColor Green

    $vpcResponse    = Read-AwsJson @('ec2', 'describe-vpcs')
    $subnetResponse = Read-AwsJson @('ec2', 'describe-subnets')
    $routeResponse  = Read-AwsJson @('ec2', 'describe-route-tables')
    $igwResponse    = Read-AwsJson @('ec2', 'describe-internet-gateways')
    $natResponse    = Read-AwsJson @('ec2', 'describe-nat-gateways')
    $endResponse    = Read-AwsJson @('ec2', 'describe-vpc-endpoints')

    $vpcs = @($vpcResponse.Vpcs)
    $subnets = @($subnetResponse.Subnets)
    $routeTables = @($routeResponse.RouteTables)
    $igws = @($igwResponse.InternetGateways)
    $nats = @($natResponse.NatGateways)
    $endpoints = @($endResponse.VpcEndpoints)

    $explicit = @{}
    $mainForVpc = @{}
    foreach ($table in $routeTables) {
        foreach ($assoc in @($table.Associations)) {
            $subnetId = Get-PropertyValue $assoc 'SubnetId'
            if ($subnetId) { $explicit[[string]$subnetId] = $table }
            if ((Get-PropertyValue $assoc 'Main') -eq $true) {
                $mainForVpc[[string]$table.VpcId] = $table
            }
        }
    }

    $attachedIgw = @{}
    foreach ($igw in $igws) {
        foreach ($attach in @($igw.Attachments)) {
            if ((Get-PropertyValue $attach 'State') -eq 'available') {
                $attachedIgw[[string]$igw.InternetGatewayId] = [string](Get-PropertyValue $attach 'VpcId')
            }
        }
    }

    $results = @()
    foreach ($subnet in $subnets) {
        $sid = [string]$subnet.SubnetId
        $vid = [string]$subnet.VpcId
        $association = 'implicit main'
        $table = $null
        if ($explicit.ContainsKey($sid)) {
            $table = $explicit[$sid]
            $association = 'explicit'
        } elseif ($mainForVpc.ContainsKey($vid)) {
            $table = $mainForVpc[$vid]
        }

        $routeKind = 'UNKNOWN: no matching table'
        $routeTarget = '-'
        $routeTableId = '-'
        if ($null -ne $table) {
            $routeTableId = [string]$table.RouteTableId
            $defaults = @($table.Routes | Where-Object {
                (Get-PropertyValue $_ 'DestinationCidrBlock') -eq '0.0.0.0/0' -and
                (Get-PropertyValue $_ 'State') -eq 'active'
            })
            if ($defaults.Count -eq 0) {
                $routeKind = 'NO ACTIVE IPv4 DEFAULT'
            } else {
                $route = $defaults[0]
                $gateway = [string](Get-PropertyValue $route 'GatewayId')
                $natId = [string](Get-PropertyValue $route 'NatGatewayId')
                if ($gateway -match '^igw-') {
                    $routeTarget = $gateway
                    if ($attachedIgw.ContainsKey($gateway) -and $attachedIgw[$gateway] -eq $vid) {
                        $routeKind = 'IGW ROUTE (public candidate)'
                    } else {
                        $routeKind = 'IGW ROUTE (attachment unverified)'
                    }
                } elseif ($natId -match '^nat-') {
                    $routeKind = 'NAT EGRESS (private candidate)'
                    $routeTarget = $natId
                } else {
                    $routeKind = 'OTHER IPv4 DEFAULT'
                    $routeTarget = [string](Get-PropertyValue $route 'TransitGatewayId')
                    if (-not $routeTarget) { $routeTarget = [string](Get-PropertyValue $route 'NetworkInterfaceId') }
                    if (-not $routeTarget) { $routeTarget = $gateway }
                    if (-not $routeTarget) { $routeTarget = 'unknown' }
                }
            }
        }
        $results += [pscustomobject]@{
            Name = Get-TagValue (Get-PropertyValue $subnet 'Tags') 'Name'
            Subnet = $sid
            VPC = $vid
            AZ = [string]$subnet.AvailabilityZone
            AutoPublicIP = [bool]$subnet.MapPublicIpOnLaunch
            RouteTable = $routeTableId
            Association = $association
            Route = $routeKind
            Target = $routeTarget
        }
    }

    Write-Host ''
    Write-Host ('Inventory: VPC={0}; Subnets={1}; RouteTables={2}; IGW={3}; NAT={4}; Endpoints={5}' -f $vpcs.Count,$subnets.Count,$routeTables.Count,$igws.Count,$nats.Count,$endpoints.Count)
    Write-Host 'Subnet routes (default IPv4):' -ForegroundColor Cyan
    if ($results.Count -gt 0) {
        $results | Sort-Object VPC,AZ,Subnet | Format-Table Subnet,AZ,AutoPublicIP,RouteTable,Association,Route,Target -AutoSize -Wrap
    } else {
        Write-Host 'No subnets found.' -ForegroundColor Yellow
    }

    $publicCandidates = @($results | Where-Object { $_.Route -eq 'IGW ROUTE (public candidate)' })
    $publicAzs = @($publicCandidates | Select-Object -ExpandProperty AZ -Unique)
    $natEgress = @($results | Where-Object { $_.Route -eq 'NAT EGRESS (private candidate)' })
    Write-Host ('Public-route candidates: {0} subnets across {1} AZ(s).' -f $publicCandidates.Count,$publicAzs.Count)
    Write-Host ('Private NAT-route candidates: {0}.' -f $natEgress.Count)
    if ($publicAzs.Count -lt 2) {
        Write-Host 'REVIEW: No verified pair of public-route subnets across two AZs for a conventional internet-facing ALB.' -ForegroundColor Yellow
    }
    Write-Host 'REVIEW: IGW routes do not prove reachability; security groups, NACLs, public IP and ALB listeners still need review.' -ForegroundColor Yellow
    Write-Host 'REVIEW: Fargate outbound needs an approved path to ECR, logs, secrets and Bedrock (internet or applicable VPC endpoints).' -ForegroundColor Yellow
    Write-Host 'COST HOLD: Budget/credits not verified. No resources deployed, no model invoked, no files written.' -ForegroundColor Yellow
    Write-Host 'Existing legacy Secrets Manager entries were NOT accessed.' -ForegroundColor Green
} catch {
    Write-Host ('[STOP] {0}' -f $_.Exception.Message) -ForegroundColor Red
    Write-Host 'No AWS resources changed and no local files written.' -ForegroundColor Yellow
    return
}

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$integrationRoot = Join-Path $env:USERPROFILE ".hindsight\codex"
$installedPolicyPath = Join-Path $integrationRoot "cortex-bank-policy.json"
$policyMarkerPath = Join-Path $integrationRoot "state\policy-ready.json"
$installMetadataPath = Join-Path $integrationRoot "cortex-install.json"
$hooksPath = Join-Path $env:USERPROFILE ".codex\hooks.json"
$codexConfigPath = Join-Path $env:USERPROFILE ".codex\config.toml"
$agentsPath = Join-Path $env:USERPROFILE ".codex\AGENTS.md"
$gatewayUrl = "http://127.0.0.1:8877"
$mcpUrl = "$gatewayUrl/mcp/cortex/"

foreach ($required in @(
    $installedPolicyPath,
    $policyMarkerPath,
    $hooksPath,
    $codexConfigPath,
    $agentsPath
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required Codex memory component is missing: $required"
    }
}

$policyMarker = Get-Content -Raw -LiteralPath $policyMarkerPath | ConvertFrom-Json
$actualPolicyHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installedPolicyPath).Hash.ToLowerInvariant()
if ([int]$policyMarker.schema_version -ne 1 -or
    [string]$policyMarker.bank -ne "cortex" -or
    [string]$policyMarker.policy_sha256 -ne $actualPolicyHash) {
    throw "The local Cortex bank-policy readiness marker is absent or stale."
}

if (Test-Path -LiteralPath $installMetadataPath) {
    $metadata = Get-Content -Raw -LiteralPath $installMetadataPath | ConvertFrom-Json
    $modeProperty = $metadata.PSObject.Properties["integration_mode"]
    if ($null -ne $modeProperty -and [string]$modeProperty.Value -ne "persistent-mcp") {
        throw "Unexpected Cortex integration mode: $($modeProperty.Value)"
    }
}

function Test-CortexManagedHook {
    param($Hook)

    if ($null -eq $Hook) {
        return $false
    }
    foreach ($propertyName in @("command", "commandWindows")) {
        $property = $Hook.PSObject.Properties[$propertyName]
        if ($null -ne $property -and
            [string]$property.Value -match '(?i)\.hindsight[\\/]codex[\\/]scripts[\\/](CortexCodexHookBridge|session_start|recall|retain)\.py') {
            return $true
        }
    }
    return $false
}

$hooksConfig = Get-Content -Raw -LiteralPath $hooksPath | ConvertFrom-Json
$cortexHookCount = 0
$hooksProperty = $hooksConfig.PSObject.Properties["hooks"]
if ($null -ne $hooksProperty) {
    foreach ($eventProperty in $hooksProperty.Value.PSObject.Properties) {
        foreach ($group in @($eventProperty.Value)) {
            $handlersProperty = if ($null -ne $group) { $group.PSObject.Properties["hooks"] } else { $null }
            if ($null -eq $handlersProperty) {
                continue
            }
            $cortexHookCount += @($handlersProperty.Value | Where-Object { Test-CortexManagedHook -Hook $_ }).Count
        }
    }
}
if ($cortexHookCount -ne 0) {
    throw "Found $cortexHookCount legacy Cortex command hook(s); they can flash terminal windows."
}

$codexConfig = Get-Content -Raw -LiteralPath $codexConfigPath
if ($codexConfig -notmatch '(?m)^\[mcp_servers\.hindsight\]\s*$' -or
    $codexConfig -notmatch '(?m)^url\s*=\s*"http://127\.0\.0\.1:8877/mcp/cortex/"\s*$' -or
    $codexConfig -notmatch '(?m)^bearer_token_env_var\s*=\s*"HINDSIGHT_MCP_API_KEY"\s*$') {
    throw "The persistent Hindsight MCP configuration is missing or incorrect."
}
if ($codexConfig -notmatch '(?m)^hooks\s*=\s*false\s*$') {
    throw "Codex lifecycle command hooks are not disabled on this installation."
}

$agentsPolicy = Get-Content -Raw -LiteralPath $agentsPath
if ($agentsPolicy -notmatch 'persistent Hindsight MCP connection' -or
    $agentsPolicy -notmatch 'Do not launch shell commands, Python scripts, or lifecycle command hooks') {
    throw "The global Codex windowless-memory policy is not installed."
}

$health = Invoke-RestMethod -Method Get -Uri "$gatewayUrl/health" -TimeoutSec 10
if (-not [bool]$health.gateway_ready -or -not [bool]$health.upstream_healthy) {
    throw "The Cortex gateway is reachable, but Hindsight is not healthy."
}

$token = [Environment]::GetEnvironmentVariable("HINDSIGHT_MCP_API_KEY", "User")
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "The HINDSIGHT_MCP_API_KEY user environment variable is missing."
}

$headers = @{
    Authorization = "Bearer $token"
    Accept = "application/json, text/event-stream"
}
$initializeBody = @{
    jsonrpc = "2.0"
    id = 1
    method = "initialize"
    params = @{
        protocolVersion = "2025-03-26"
        capabilities = @{}
        clientInfo = @{ name = "cortex-integration-test"; version = "1.0" }
    }
} | ConvertTo-Json -Depth 8 -Compress

$initialize = Invoke-WebRequest `
    -UseBasicParsing `
    -Method Post `
    -Uri $mcpUrl `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $initializeBody `
    -TimeoutSec 30
$sessionId = [string]$initialize.Headers["Mcp-Session-Id"]
if ($initialize.StatusCode -ne 200 -or [string]::IsNullOrWhiteSpace($sessionId)) {
    throw "Hindsight MCP initialization did not return a usable session."
}

$headers["Mcp-Session-Id"] = $sessionId
$initializedBody = @{
    jsonrpc = "2.0"
    method = "notifications/initialized"
    params = @{}
} | ConvertTo-Json -Depth 4 -Compress
$null = Invoke-WebRequest `
    -UseBasicParsing `
    -Method Post `
    -Uri $mcpUrl `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $initializedBody `
    -TimeoutSec 30

$toolsBody = @{
    jsonrpc = "2.0"
    id = 2
    method = "tools/list"
    params = @{}
} | ConvertTo-Json -Depth 4 -Compress
$toolsResponse = Invoke-WebRequest `
    -UseBasicParsing `
    -Method Post `
    -Uri $mcpUrl `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $toolsBody `
    -TimeoutSec 30

foreach ($toolName in @("recall", "retain", "sync_retain")) {
    if ($toolsResponse.Content -notmatch ('"name"\s*:\s*"' + [regex]::Escape($toolName) + '"')) {
        throw "Hindsight MCP did not expose the required '$toolName' tool."
    }
}

$recallBody = @{
    jsonrpc = "2.0"
    id = 3
    method = "tools/call"
    params = @{
        name = "recall"
        arguments = @{ query = "Cortex integration health check" }
    }
} | ConvertTo-Json -Depth 8 -Compress
$recallResponse = Invoke-WebRequest `
    -UseBasicParsing `
    -Method Post `
    -Uri $mcpUrl `
    -Headers $headers `
    -ContentType "application/json" `
    -Body $recallBody `
    -TimeoutSec 300
$recallJson = $recallResponse.Content
if ([string]$recallResponse.Headers["Content-Type"] -match 'text/event-stream') {
    $dataLine = @($recallJson -split "`r?`n" | Where-Object { $_ -match '^data:\s*' } | Select-Object -Last 1)
    if ($dataLine.Count -eq 0) {
        throw "Hindsight MCP recall returned an SSE response without a data event."
    }
    $recallJson = $dataLine[0] -replace '^data:\s*', ''
}
$recallPayload = $recallJson | ConvertFrom-Json
$errorProperty = $recallPayload.PSObject.Properties["error"]
$resultProperty = $recallPayload.PSObject.Properties["result"]
if (($null -ne $errorProperty -and $null -ne $errorProperty.Value) -or
    $null -eq $resultProperty -or $null -eq $resultProperty.Value) {
    throw "Hindsight MCP recall tool did not complete successfully through the Cortex gateway."
}

Write-Output "Codex windowless memory test passed: MCP authenticated, recall executed, retain tools are available, and command hooks are absent."

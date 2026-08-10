Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "Cortex-Common.ps1")
Initialize-CortexToolEnvironment

$codexMemoryConfigPath = Join-Path $env:USERPROFILE ".hindsight\codex.json"
$codexMemoryConfig = if (Test-Path -LiteralPath $codexMemoryConfigPath) {
    Get-Content -Raw -LiteralPath $codexMemoryConfigPath | ConvertFrom-Json
}
else {
    [pscustomobject]@{}
}
$apiUrlProperty = $codexMemoryConfig.PSObject.Properties["hindsightApiUrl"]
$apiUrl = if ($null -ne $apiUrlProperty -and -not [string]::IsNullOrWhiteSpace([string]$apiUrlProperty.Value)) {
    ([string]$apiUrlProperty.Value).TrimEnd("/")
}
else {
    "http://127.0.0.1:8888"
}
$apiTokenProperty = $codexMemoryConfig.PSObject.Properties["hindsightApiToken"]
$apiHeaders = @{}
if ($null -ne $apiTokenProperty -and -not [string]::IsNullOrWhiteSpace([string]$apiTokenProperty.Value)) {
    $apiHeaders["Authorization"] = "Bearer $($apiTokenProperty.Value)"
}

$hindsightPath = Join-Path $env:USERPROFILE ".local\bin\hindsight-embed.exe"
if (-not (Test-Path -LiteralPath $hindsightPath)) {
    throw "hindsight-embed.exe was not found."
}

& (Join-Path $PSScriptRoot "Start-CortexBrain.ps1") | Out-Null

$health = Invoke-RestMethod -Uri "$apiUrl/health" -Headers $apiHeaders -TimeoutSec 10
if ($health.status -ne "healthy") {
    throw "Unexpected Hindsight health response."
}

$bankConfigResponse = Invoke-RestMethod -Uri "$apiUrl/v1/default/banks/cortex/config" -Headers $apiHeaders -TimeoutSec 10
$bankConfig = $bankConfigResponse.config
$defenseRules = @($bankConfig.memory_defense.rules | ForEach-Object { "$($_.on):$($_.action)" })
if (-not $bankConfig.memory_defense.enabled -or
    $defenseRules -notcontains "sensitive_data:block" -or
    $defenseRules -notcontains "prompt_injection:block" -or
    $bankConfig.recall_include_chunks) {
    throw "The Cortex bank memory-defense/recall policy is not active."
}

& (Join-Path $PSScriptRoot "Test-CortexCodexIntegration.ps1") | Out-Null

$stamp = Get-Date -Format "yyyyMMddHHmmss"
$fact = "The exact Cortex smoke test marker is $stamp."
$query = "What is the Cortex smoke test marker?"
$baseUrl = "$apiUrl/v1/default/banks/cortex"

$retainBody = @{
    items = @(@{
        content = $fact
        context = "Automated Cortex installation smoke test"
        document_id = "cortex-installation-smoke-test"
        tags = @("cortex-smoke-test")
    })
    async = $false
} | ConvertTo-Json -Depth 6
$retain = Invoke-RestMethod -Method Post -Uri "$baseUrl/memories" -Headers $apiHeaders -ContentType "application/json" -Body $retainBody -TimeoutSec 300
if (-not $retain.success) {
    throw "Retain smoke test failed."
}

$recallBody = @{
    query = $query
    budget = "high"
    max_tokens = 2048
    tags = @("cortex-smoke-test")
    tags_match = "all_strict"
} | ConvertTo-Json -Depth 5
$recall = Invoke-RestMethod -Method Post -Uri "$baseUrl/memories/recall" -Headers $apiHeaders -ContentType "application/json" -Body $recallBody -TimeoutSec 120
$recallJson = $recall | ConvertTo-Json -Depth 12
if ($recallJson -notmatch [Regex]::Escape($stamp)) {
    throw "Recall completed but did not return the expected marker $stamp."
}

$matchingMemories = @($recall.results | Where-Object { $_.text -match [Regex]::Escape($stamp) })
foreach ($memory in $matchingMemories) {
    $cleanupBody = @{
        state = "invalidated"
        reason = "Automatic cleanup after successful Cortex installation smoke test"
    } | ConvertTo-Json
    $null = Invoke-RestMethod -Method Patch -Uri "$baseUrl/memories/$($memory.id)" -Headers $apiHeaders -ContentType "application/json" -Body $cleanupBody -TimeoutSec 30
}

try {
    & (Join-Path $PSScriptRoot "Start-CortexMemoryBrowser.ps1") -NoOpen | Out-Null
    $browserHealth = Invoke-RestMethod -Uri "http://localhost:9999/api/health" -TimeoutSec 10
    if ([string]$browserHealth.service -ne "hindsight-control-plane" -or [string]$browserHealth.dataplane.status -ne "connected") {
        throw "Unexpected Memory Browser health response."
    }
    $browserPage = Invoke-WebRequest -UseBasicParsing -Uri "http://localhost:9999/banks/cortex?view=data&subTab=world" -TimeoutSec 30
    if ($browserPage.StatusCode -ne 200) {
        throw "The Cortex memory page returned HTTP $($browserPage.StatusCode)."
    }
}
finally {
    & (Join-Path $PSScriptRoot "Stop-CortexMemoryBrowser.ps1") -Quiet
}

Write-Output "Cortex Brain and Memory Browser smoke tests completed; the temporary memory was retired. Expected marker: $stamp"

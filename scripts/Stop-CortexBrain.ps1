Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$config = Get-Content -Raw -LiteralPath (Join-Path $projectRoot "config\brain.json") | ConvertFrom-Json
$gatewayUrl = if ($null -ne $config.PSObject.Properties["gatewayUrl"]) {
    ([string]$config.gatewayUrl).TrimEnd("/")
}
else {
    "http://127.0.0.1:8877"
}
$token = [Environment]::GetEnvironmentVariable("HINDSIGHT_MCP_API_KEY", "User")
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "HINDSIGHT_MCP_API_KEY is missing. Run Install-CortexBrain.ps1 first."
}

$null = Invoke-RestMethod `
    -Method Post `
    -Uri "$gatewayUrl/control/stop" `
    -Headers @{ Authorization = "Bearer $token" } `
    -ContentType "application/json" `
    -Body '{"manual":true}' `
    -TimeoutSec 240

Write-Output "Cortex Brain entered manual deep sleep; the gateway service remains ready."

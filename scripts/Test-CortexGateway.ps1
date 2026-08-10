param(
    [switch]$StartTemporaryGateway
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$gatewayUrl = "http://127.0.0.1:8877"
$gatewayProcess = $null

try {
    if ($StartTemporaryGateway) {
        $pythonw = Join-Path $env:APPDATA "uv\tools\hindsight-embed\Scripts\pythonw.exe"
        $gatewayScript = Join-Path $projectRoot "src\CortexMcpGateway.py"
        $gatewayConfig = Join-Path $projectRoot "config\gateway.json"
        $gatewayProcess = Start-Process `
            -FilePath $pythonw `
            -ArgumentList @($gatewayScript, "--config", $gatewayConfig) `
            -WindowStyle Hidden `
            -PassThru
    }

    $health = $null
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline -and $null -eq $health) {
        try {
            $health = Invoke-RestMethod -Uri "$gatewayUrl/health" -TimeoutSec 2
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if ($null -eq $health -or -not [bool]$health.gateway_ready) {
        throw "The Cortex gateway did not become ready."
    }

    $unauthorizedBlocked = $false
    try {
        $null = Invoke-WebRequest -UseBasicParsing -Uri "$gatewayUrl/status" -TimeoutSec 5
    }
    catch {
        if ($null -ne $_.Exception.Response) {
            $unauthorizedBlocked = [int]$_.Exception.Response.StatusCode -eq 401
        }
    }
    if (-not $unauthorizedBlocked) {
        throw "The authenticated gateway status endpoint accepted an anonymous request."
    }

    $token = [Environment]::GetEnvironmentVariable("HINDSIGHT_MCP_API_KEY", "User")
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw "HINDSIGHT_MCP_API_KEY is missing."
    }
    $headers = @{
        Authorization = "Bearer $token"
        Accept = "application/json, text/event-stream"
    }
    $status = Invoke-RestMethod -Uri "$gatewayUrl/status" -Headers $headers -TimeoutSec 5

    $initializeBody = @{
        jsonrpc = "2.0"
        id = 1
        method = "initialize"
        params = @{
            protocolVersion = "2025-03-26"
            capabilities = @{}
            clientInfo = @{ name = "cortex-gateway-smoke"; version = "1.0" }
        }
    } | ConvertTo-Json -Depth 8 -Compress
    $initialize = Invoke-WebRequest `
        -UseBasicParsing `
        -Method Post `
        -Uri "$gatewayUrl/mcp/cortex/" `
        -Headers $headers `
        -ContentType "application/json" `
        -Body $initializeBody `
        -TimeoutSec 30
    $sessionId = [string]$initialize.Headers["Mcp-Session-Id"]
    if ($initialize.StatusCode -ne 200 -or [string]::IsNullOrWhiteSpace($sessionId)) {
        throw "MCP initialization did not return a usable session."
    }

    [pscustomobject]@{
        GatewayReady = [bool]$health.gateway_ready
        UpstreamHealthy = [bool]$status.upstream_healthy
        UnauthorizedBlocked = $unauthorizedBlocked
        McpInitialized = $true
        SessionHeaderPresent = $true
        GatewayPid = if ($null -ne $gatewayProcess) { $gatewayProcess.Id } else { $null }
    }
}
finally {
    if ($null -ne $gatewayProcess -and -not $gatewayProcess.HasExited) {
        Stop-Process -Id $gatewayProcess.Id -Force
    }
}

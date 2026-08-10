[CmdletBinding()]
param(
    [Parameter()]
    [string] $BridgePath,

    [Parameter()]
    [string] $GatewayUrl = 'http://127.0.0.1:8877/mcp/cortex/',

    [Parameter()]
    [ValidateRange(5, 1800)]
    [int] $TimeoutSeconds = 900
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($BridgePath)) {
    $BridgePath = Join-Path $env:LOCALAPPDATA 'Cortex\bin\CortexMcpStdioBridge.exe'
}
if (-not (Test-Path -LiteralPath $BridgePath -PathType Leaf)) {
    throw "The Cortex Claude bridge was not found: $BridgePath"
}

$gatewayUri = $null
if (-not [Uri]::TryCreate($GatewayUrl, [UriKind]::Absolute, [ref] $gatewayUri) -or
    -not $gatewayUri.IsLoopback -or
    $gatewayUri.Scheme -ne [Uri]::UriSchemeHttp) {
    throw 'GatewayUrl must be an http:// loopback URL.'
}
if ([string]::IsNullOrWhiteSpace(
    [Environment]::GetEnvironmentVariable('HINDSIGHT_MCP_API_KEY', 'User'))) {
    throw 'HINDSIGHT_MCP_API_KEY is missing from the Windows user environment.'
}

function ConvertTo-QuotedProcessArgument {
    param(
        [Parameter(Mandatory)]
        [string] $Value
    )

    if ($Value.IndexOf('"') -ge 0 -or $Value.IndexOf("`r") -ge 0 -or $Value.IndexOf("`n") -ge 0) {
        throw 'A bridge argument contains an unsupported quote or newline.'
    }

    return '"' + $Value + '"'
}

function Read-BridgeResponse {
    param(
        [Parameter(Mandatory)]
        [Diagnostics.Process] $Process,

        [Parameter(Mandatory)]
        [int] $ExpectedId,

        [Parameter(Mandatory)]
        [DateTime] $Deadline
    )

    while ((Get-Date) -lt $Deadline) {
        $remaining = [int] [Math]::Max(1, ($Deadline - (Get-Date)).TotalMilliseconds)
        $readTask = $Process.StandardOutput.ReadLineAsync()
        if (-not $readTask.Wait($remaining)) {
            throw "Timed out waiting for bridge response id $ExpectedId."
        }

        $line = $readTask.Result
        if ($null -eq $line) {
            $stderr = $Process.StandardError.ReadToEnd().Trim()
            if (-not [string]::IsNullOrWhiteSpace($stderr)) {
                throw "The bridge closed before response id $ExpectedId. $stderr"
            }
            throw "The bridge closed before response id $ExpectedId."
        }
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }

        try {
            $message = $line | ConvertFrom-Json
        }
        catch {
            throw "The bridge returned invalid newline-delimited JSON: $line"
        }

        $idProperty = $message.PSObject.Properties['id']
        $errorProperty = $message.PSObject.Properties['error']
        if ($null -eq $idProperty -or $null -eq $idProperty.Value) {
            if ($null -ne $errorProperty -and $null -ne $errorProperty.Value) {
                throw "The bridge returned a JSON-RPC error: $($errorProperty.Value.message)"
            }
            continue
        }

        if ([string] $idProperty.Value -ne [string] $ExpectedId) {
            continue
        }
        if ($null -ne $errorProperty -and $null -ne $errorProperty.Value) {
            throw "The bridge returned a JSON-RPC error for id $ExpectedId`: $($errorProperty.Value.message)"
        }

        return $message
    }

    throw "Timed out waiting for bridge response id $ExpectedId."
}

$startInfo = [Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = (Resolve-Path -LiteralPath $BridgePath).Path
$startInfo.Arguments = '--url ' + (ConvertTo-QuotedProcessArgument -Value $gatewayUri.AbsoluteUri) +
    ' --timeout-seconds ' + [string] $TimeoutSeconds
$startInfo.WorkingDirectory = Split-Path -Parent $startInfo.FileName
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true

$process = [Diagnostics.Process]::new()
$process.StartInfo = $startInfo
try {
    if (-not $process.Start()) {
        throw 'Failed to start CortexMcpStdioBridge.'
    }

    Start-Sleep -Milliseconds 100
    $process.Refresh()
    if ($process.MainWindowHandle -ne [IntPtr]::Zero) {
        throw 'The Cortex Claude bridge unexpectedly created a visible window.'
    }

    $process.StandardInput.AutoFlush = $true
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $initializeRequest = [ordered]@{
        jsonrpc = '2.0'
        id = 1
        method = 'initialize'
        params = [ordered]@{
            protocolVersion = '2025-03-26'
            capabilities = @{}
            clientInfo = [ordered]@{
                name = 'cortex-claude-bridge-test'
                version = '1.0.0'
            }
        }
    } | ConvertTo-Json -Depth 8 -Compress
    $process.StandardInput.WriteLine($initializeRequest)
    $initializeResponse = Read-BridgeResponse -Process $process -ExpectedId 1 -Deadline $deadline
    if ($null -eq $initializeResponse.PSObject.Properties['result']) {
        throw 'The bridge initialize response did not include a result.'
    }

    $initializedNotification = [ordered]@{
        jsonrpc = '2.0'
        method = 'notifications/initialized'
    } | ConvertTo-Json -Compress
    $process.StandardInput.WriteLine($initializedNotification)

    $toolsRequest = [ordered]@{
        jsonrpc = '2.0'
        id = 2
        method = 'tools/list'
        params = @{}
    } | ConvertTo-Json -Compress
    $process.StandardInput.WriteLine($toolsRequest)
    $toolsResponse = Read-BridgeResponse -Process $process -ExpectedId 2 -Deadline $deadline
    $tools = @($toolsResponse.result.tools)
    if ($tools.Count -lt 1) {
        throw 'The bridge reached Cortex, but Cortex returned no MCP tools.'
    }

    [pscustomobject]@{
        BridgeStartedHidden = $true
        InitializeSucceeded = $true
        ToolsListSucceeded = $true
        ToolCount = $tools.Count
        BridgePid = $process.Id
    }
}
finally {
    try {
        if ($null -ne $process -and $process.StartInfo.RedirectStandardInput) {
            $process.StandardInput.Close()
        }
    }
    catch {
    }

    if ($null -ne $process -and -not $process.HasExited) {
        if (-not $process.WaitForExit(10000)) {
            $process.Kill()
            $process.WaitForExit()
        }
    }
    if ($null -ne $process) {
        $process.Dispose()
    }
}

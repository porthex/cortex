param(
    [switch]$NoOpen,
    [int]$TimeoutSeconds = 90
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "Cortex-Common.ps1")
Initialize-CortexToolEnvironment

$config = Get-Content -Raw -LiteralPath (Join-Path $projectRoot "config\brain.json") | ConvertFrom-Json
$browserBaseUrl = if ($null -ne $config.PSObject.Properties["memoryBrowserUrl"]) {
    ([string]$config.memoryBrowserUrl).TrimEnd("/")
}
else {
    "http://localhost:9999"
}
$browserPort = if ($null -ne $config.PSObject.Properties["memoryBrowserPort"]) {
    [int]$config.memoryBrowserPort
}
else {
    9999
}
$bankId = [Uri]::EscapeDataString([string]$config.bankId)
$memoryUrl = "$browserBaseUrl/banks/$bankId`?view=data&subTab=world"
$healthUrl = "$browserBaseUrl/api/health"
$runtimeDirectory = Join-Path $projectRoot "runtime"
$pidPath = Join-Path $runtimeDirectory "memory-browser.json"
$standardOutputPath = Join-Path $runtimeDirectory "memory-browser.log"
$standardErrorPath = Join-Path $runtimeDirectory "memory-browser-error.log"
$null = New-Item -ItemType Directory -Path $runtimeDirectory -Force

function Get-Cortex-MemoryBrowserHealth {
    try {
        return Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3
    }
    catch {
        return $null
    }
}

function Test-Cortex-MemoryBrowserHealth {
    param($Health)

    if ($null -eq $Health -or $null -eq $Health.PSObject.Properties["service"]) {
        return $false
    }
    return [string]$Health.service -eq "hindsight-control-plane"
}

function Open-Cortex-MemoryBrowserUrl {
    if (-not $NoOpen) {
        Start-Process -FilePath $memoryUrl
    }
}

function Resolve-CortexControlPlaneRuntime {
    $nodeCommand = Get-Command "node.exe" -ErrorAction SilentlyContinue
    if ($null -ne $nodeCommand) {
        $nodeRoot = Split-Path -Parent $nodeCommand.Source
        $cliPath = Join-Path $nodeRoot "node_modules\@vectorize-io\hindsight-control-plane\bin\cli.js"
        if (Test-Path -LiteralPath $cliPath) {
            return [pscustomobject]@{ NodePath = $nodeCommand.Source; CliPath = $cliPath }
        }
    }

    $programsDirectory = Join-Path $env:LOCALAPPDATA "Programs"
    if (Test-Path -LiteralPath $programsDirectory) {
        foreach ($nodeDirectory in @(Get-ChildItem -LiteralPath $programsDirectory -Directory -Filter "node-*" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)) {
            $nodePath = Join-Path $nodeDirectory.FullName "node.exe"
            $cliPath = Join-Path $nodeDirectory.FullName "node_modules\@vectorize-io\hindsight-control-plane\bin\cli.js"
            if ((Test-Path -LiteralPath $nodePath) -and (Test-Path -LiteralPath $cliPath)) {
                return [pscustomobject]@{ NodePath = $nodePath; CliPath = $cliPath }
            }
        }
    }

    return $null
}

# The browser reads memories from the local API, so wake the brain first.
& (Join-Path $PSScriptRoot "Start-CortexBrain.ps1") | Out-Null

$existingHealth = Get-Cortex-MemoryBrowserHealth
if (Test-Cortex-MemoryBrowserHealth -Health $existingHealth) {
    Open-Cortex-MemoryBrowserUrl
    Write-Output "Cortex Memory Browser is ready at $memoryUrl"
    exit 0
}

$listener = Get-NetTCPConnection -LocalPort $browserPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($null -ne $listener) {
    throw "Port $browserPort is already in use by process $($listener.OwningProcess), but it is not the Cortex Memory Browser."
}

$controlPlaneRuntime = Resolve-CortexControlPlaneRuntime
if ($null -eq $controlPlaneRuntime) {
    throw "The Hindsight Memory Browser is not installed. Run scripts\Install-CortexBrain.ps1 first."
}

# Launch Node directly. Calling the package's .cmd shim leaves a persistent
# cmd.exe/conhost process in the interactive session even when PowerShell asks
# for a hidden window.
$process = Start-Process `
    -FilePath $controlPlaneRuntime.NodePath `
    -ArgumentList @($controlPlaneRuntime.CliPath, "--port", [string]$browserPort, "--hostname", "localhost", "--api-url", [string]$config.apiUrl) `
    -WindowStyle Hidden `
    -RedirectStandardOutput $standardOutputPath `
    -RedirectStandardError $standardErrorPath `
    -PassThru

[ordered]@{
    processId = $process.Id
    processStartTime = $process.StartTime.ToString("o")
    startedAt = (Get-Date).ToString("o")
    commandPath = $controlPlaneRuntime.NodePath
    scriptPath = $controlPlaneRuntime.CliPath
    browserUrl = $browserBaseUrl
} | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8

$deadline = (Get-Date).AddSeconds([Math]::Max(10, $TimeoutSeconds))
while ((Get-Date) -lt $deadline) {
    $health = Get-Cortex-MemoryBrowserHealth
    if (Test-Cortex-MemoryBrowserHealth -Health $health) {
        Open-Cortex-MemoryBrowserUrl
        Write-Output "Cortex Memory Browser is ready at $memoryUrl"
        exit 0
    }

    if ($process.HasExited) {
        break
    }
    Start-Sleep -Milliseconds 500
}

if (-not $process.HasExited) {
    & taskkill.exe /PID $process.Id /T /F 2>$null | Out-Null
}
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
$errorTail = if (Test-Path -LiteralPath $standardErrorPath) {
    (Get-Content -LiteralPath $standardErrorPath -Tail 20 -ErrorAction SilentlyContinue) -join " "
}
else {
    ""
}
throw "The Cortex Memory Browser did not become ready. $errorTail"

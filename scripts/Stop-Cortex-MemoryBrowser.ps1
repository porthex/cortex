param(
    [switch]$Quiet
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$config = Get-Content -Raw -LiteralPath (Join-Path $projectRoot "config\brain.json") | ConvertFrom-Json
$browserPort = if ($null -ne $config.PSObject.Properties["memoryBrowserPort"]) {
    [int]$config.memoryBrowserPort
}
else {
    9999
}
$runtimeDirectory = Join-Path $projectRoot "runtime"
$pidPath = Join-Path $runtimeDirectory "memory-browser.json"
$stopped = $false

function Test-CortexControlPlaneProcess {
    param([Parameter(Mandatory = $true)]$ProcessRecord)

    $commandLine = [string]$ProcessRecord.CommandLine
    return $commandLine -match "hindsight-control-plane" -or $commandLine -match "standalone[\\/]server\.js"
}

function Stop-CortexValidatedProcessTree {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $processRecord = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
    if ($null -eq $processRecord -or -not (Test-CortexControlPlaneProcess -ProcessRecord $processRecord)) {
        return $false
    }

    & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
    return $true
}

if (Test-Path -LiteralPath $pidPath) {
    try {
        $record = Get-Content -Raw -LiteralPath $pidPath | ConvertFrom-Json
        if ([string]$record.processId -match "^\d+$") {
            $candidate = Get-Process -Id ([int]$record.processId) -ErrorAction SilentlyContinue
            if ($null -ne $candidate) {
                $recordedStart = [DateTime]::Parse([string]$record.processStartTime).ToLocalTime()
                if ([Math]::Abs(($candidate.StartTime - $recordedStart).TotalSeconds) -lt 10) {
                    $stopped = Stop-CortexValidatedProcessTree -ProcessId $candidate.Id
                }
            }
        }
    }
    catch {
        # A stale or partial PID record is safe to ignore; the listener check below is authoritative.
    }
}

# Recover cleanly if the launcher PID file was lost but the Node listener survived.
$listeners = @(Get-NetTCPConnection -LocalPort $browserPort -State Listen -ErrorAction SilentlyContinue)
foreach ($listener in $listeners) {
    if (Stop-CortexValidatedProcessTree -ProcessId ([int]$listener.OwningProcess)) {
        $stopped = $true
    }
}

$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    if ($null -eq (Get-NetTCPConnection -LocalPort $browserPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        break
    }
    Start-Sleep -Milliseconds 250
}

Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
if (-not $Quiet) {
    if ($stopped) {
        Write-Output "Cortex Memory Browser closed."
    }
    else {
        Write-Output "Cortex Memory Browser was not running."
    }
}

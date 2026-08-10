function Initialize-CortexToolEnvironment {
    $env:UV_PYTHON = "3.12"

    $uvDirectory = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe"
    if (Test-Path -LiteralPath $uvDirectory) {
        if (($env:PATH -split ";") -notcontains $uvDirectory) {
            $env:PATH = "$uvDirectory;$env:PATH"
        }
    }
}

function Test-CortexControllerProcess {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $runtimeDirectory = Join-Path $ProjectRoot "runtime"
    $pidPath = Join-Path $runtimeDirectory "controller.pid"
    $statePath = Join-Path $runtimeDirectory "state.json"
    if (-not (Test-Path -LiteralPath $pidPath) -or -not (Test-Path -LiteralPath $statePath)) {
        return $false
    }

    try {
        $controllerPidText = (Get-Content -LiteralPath $pidPath -ErrorAction Stop | Select-Object -First 1).Trim()
        if ($controllerPidText -notmatch "^\d+$") {
            return $false
        }

        $state = Get-Content -Raw -LiteralPath $statePath -ErrorAction Stop | ConvertFrom-Json
        $controllerPid = [int]$controllerPidText
        if ([int]$state.controllerPid -ne $controllerPid) {
            return $false
        }

        $process = Get-Process -Id $controllerPid -ErrorAction Stop
        if ($process.ProcessName -notin @("powershell", "pwsh")) {
            return $false
        }

        $recordedStart = [DateTime]::Parse([string]$state.controllerStartedAt).ToLocalTime()
        return [Math]::Abs(($process.StartTime - $recordedStart).TotalSeconds) -lt 10
    }
    catch {
        return $false
    }
}

function Remove-CortexStaleControllerPid {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    if (Test-CortexControllerProcess -ProjectRoot $ProjectRoot) {
        return $false
    }

    $pidPath = Join-Path (Join-Path $ProjectRoot "runtime") "controller.pid"
    if (Test-Path -LiteralPath $pidPath) {
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        return $true
    }

    return $false
}

function Send-CortexControllerRequest {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][ValidateSet("start", "stop", "exit")][string]$Action
    )

    $runtimeDirectory = Join-Path $ProjectRoot "runtime"
    $requestPath = Join-Path $runtimeDirectory "command.json"
    $null = New-Item -ItemType Directory -Path $runtimeDirectory -Force
    $temporaryPath = Join-Path $runtimeDirectory ("command.{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
    [ordered]@{
        action = $Action
        requestedAt = (Get-Date).ToString("o")
        requestId = [Guid]::NewGuid().ToString("N")
    } | ConvertTo-Json | Set-Content -LiteralPath $temporaryPath -Encoding UTF8

    if (Test-Path -LiteralPath $requestPath) {
        [System.IO.File]::Replace($temporaryPath, $requestPath, $null)
    }
    else {
        [System.IO.File]::Move($temporaryPath, $requestPath)
    }
}

function Set-CortexPersistedPause {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][bool]$Paused,
        [bool]$RearmSeenNoClients = $false
    )

    $runtimeDirectory = Join-Path $ProjectRoot "runtime"
    $statePath = Join-Path $runtimeDirectory "state.json"
    $null = New-Item -ItemType Directory -Path $runtimeDirectory -Force
    $state = [ordered]@{
        autoWakeEnabled = $true
        manualPaused = $Paused
        rearmSeenNoClients = $RearmSeenNoClients
        controllerPid = 0
        controllerStartedAt = ""
    }

    if (Test-Path -LiteralPath $statePath) {
        try {
            $saved = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
            foreach ($name in @("autoWakeEnabled", "controllerPid", "controllerStartedAt")) {
                if ($null -ne $saved.$name) {
                    $state[$name] = $saved.$name
                }
            }
        }
        catch {
            # Replace unreadable state with safe defaults.
        }
    }

    $state.manualPaused = $Paused
    $state.rearmSeenNoClients = $RearmSeenNoClients
    $temporaryPath = Join-Path $runtimeDirectory ("state.{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
    $state | ConvertTo-Json | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    if (Test-Path -LiteralPath $statePath) {
        [System.IO.File]::Replace($temporaryPath, $statePath, $null)
    }
    else {
        [System.IO.File]::Move($temporaryPath, $statePath)
    }
}

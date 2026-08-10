param(
    [switch]$RemoveHindsightTool,
    [switch]$RemoveMemoryData
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Continue"

$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "Cortex-Common.ps1")
Initialize-CortexToolEnvironment
$runtimeDirectory = Join-Path $projectRoot "runtime"
$pidPath = Join-Path $runtimeDirectory "controller.pid"
$hindsightPath = Join-Path $env:USERPROFILE ".local\bin\hindsight-embed.exe"
$ollamaPath = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
$taskName = "Cortex Brain Controller"

$memoryBrowserStopPath = Join-Path $PSScriptRoot "Stop-CortexMemoryBrowser.ps1"
if (Test-Path -LiteralPath $memoryBrowserStopPath) {
    & $memoryBrowserStopPath -Quiet
}

$scheduledTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($null -ne $scheduledTask) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
}

if (Test-CortexControllerProcess -ProjectRoot $projectRoot) {
    $controllerPid = [int](Get-Content -LiteralPath $pidPath | Select-Object -First 1)
    Stop-Process -Id $controllerPid -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue

$gatewayService = Get-Service -Name "CortexBrainGateway" -ErrorAction SilentlyContinue
if ($null -ne $gatewayService) {
    $gatewayUninstaller = Join-Path $PSScriptRoot "Uninstall-CortexGatewayService.ps1"
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        & $gatewayUninstaller
    }
    else {
        Write-Output "Windows will request administrator approval once to remove the Cortex service."
        $elevated = Start-Process `
            -FilePath powershell.exe `
            -Verb RunAs `
            -WindowStyle Hidden `
            -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $gatewayUninstaller) `
            -Wait `
            -PassThru
        if ($elevated.ExitCode -ne 0) {
            throw "CortexBrainGateway could not be removed; Hindsight profile cleanup was stopped to avoid a service race."
        }
    }
}

if (Test-Path -LiteralPath $hindsightPath) {
    & $hindsightPath -p cortex daemon stop
    & $hindsightPath -p cortex ui stop --port 18888
    & $hindsightPath control stop
    & $hindsightPath profile delete cortex
}
if (Test-Path -LiteralPath $ollamaPath) {
    & $ollamaPath stop "gpt-oss:20b"
}

$startupDirectory = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDirectory "Cortex Brain Controller.lnk"
if (Test-Path -LiteralPath $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath -Force
}
$programShortcutPath = Join-Path ([Environment]::GetFolderPath("Programs")) "Cortex Brain.lnk"
if (Test-Path -LiteralPath $programShortcutPath) {
    Remove-Item -LiteralPath $programShortcutPath -Force
}

$codexIntegrationUninstaller = Join-Path $PSScriptRoot "Uninstall-CortexCodexIntegration.ps1"
if (Test-Path -LiteralPath $codexIntegrationUninstaller) {
    & $codexIntegrationUninstaller
}
$claudeIntegrationUninstaller = Join-Path $PSScriptRoot "Uninstall-CortexClaudeIntegration.ps1"
if (Test-Path -LiteralPath $claudeIntegrationUninstaller) {
    & $claudeIntegrationUninstaller
}

if ($RemoveHindsightTool) {
    $uvPath = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe\uv.exe"
    if (Test-Path -LiteralPath $uvPath) {
        & $uvPath tool uninstall hindsight-embed
    }
}

if ($RemoveMemoryData) {
    $memoryPath = Join-Path $env:USERPROFILE ".pg0\instances\hindsight-embed-cortex"
    $resolvedParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $memoryPath))
    $expectedParent = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".pg0\instances"))
    if ($resolvedParent -ne $expectedParent) {
        throw "Refusing to remove an unexpected memory path."
    }
    $postgresListener = Get-NetTCPConnection -State Listen -LocalPort 5432 -ErrorAction SilentlyContinue
    if ($null -ne $postgresListener) {
        throw "Refusing to remove Cortex memory data while PostgreSQL is still listening on port 5432."
    }
    $postmasterPidPath = Join-Path $memoryPath "data\postmaster.pid"
    if (Test-Path -LiteralPath $postmasterPidPath) {
        throw "Refusing to remove Cortex memory data while postmaster.pid still exists. Stop PostgreSQL cleanly first."
    }
    if (Test-Path -LiteralPath $memoryPath) {
        Remove-Item -LiteralPath $memoryPath -Recurse -Force
    }
}

Write-Output "Cortex integration removed. Memory data was preserved unless -RemoveMemoryData was explicitly supplied."

[CmdletBinding()]
param(
    [Parameter()]
    [ValidateRange(10, 180)]
    [int] $StopTimeoutSeconds = 75
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$serviceName = 'CortexBrainGateway'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Removing CortexBrainGateway requires an elevated PowerShell window (Run as administrator).'
}

$service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($null -eq $service) {
    Write-Output "$serviceName is not installed. No files or memory data were changed."
    return
}

if ($service.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
    Stop-Service -Name $serviceName
    $service.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds($StopTimeoutSeconds))
    $service.Refresh()
    if ($service.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
        throw "$serviceName did not stop within $StopTimeoutSeconds seconds; it was not removed."
    }
}

$scPath = Join-Path $env:SystemRoot 'System32\sc.exe'
$output = @(& $scPath delete $serviceName 2>&1)
if ($LASTEXITCODE -ne 0) {
    throw "sc.exe delete failed with exit code $LASTEXITCODE. $(($output | Out-String).Trim())"
}

Write-Output "$serviceName was stopped and unregistered. Its executable, logs, configuration, and all Hindsight memory data were preserved."

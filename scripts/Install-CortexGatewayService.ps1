[CmdletBinding()]
param(
    [Parameter()]
    [string] $ConfigPath,

    [Parameter()]
    [string] $PythonwPath,

    [Parameter()]
    [string] $GatewayScriptPath,

    [Parameter()]
    [ValidateRange(10, 300)]
    [int] $StartupTimeoutSeconds = 90,

    [Parameter()]
    [ValidateRange(5, 120)]
    [int] $StopGraceSeconds = 120
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$serviceName = 'CortexBrainGateway'
$displayName = 'Cortex Brain Gateway'
$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $projectRoot 'config\gateway.json'
}
if ([string]::IsNullOrWhiteSpace($PythonwPath)) {
    $PythonwPath = Join-Path $env:APPDATA 'uv\tools\hindsight-embed\Scripts\pythonw.exe'
}
if ([string]::IsNullOrWhiteSpace($GatewayScriptPath)) {
    $GatewayScriptPath = Join-Path $projectRoot 'src\CortexMcpGateway.py'
}
$serviceSourcePath = Join-Path $projectRoot 'src\CortexGatewayService.cs'
$runtimeDirectory = Join-Path $projectRoot 'runtime'
$logDirectory = Join-Path $runtimeDirectory 'logs'
$serviceExecutablePath = Join-Path $runtimeDirectory 'CortexGatewayService.exe'
$serviceLogPath = Join-Path $logDirectory 'gateway-service.log'

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Installing CortexBrainGateway requires an elevated PowerShell window (Run as administrator).'
    }
}

function Resolve-RequiredFile {
    param(
        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter(Mandatory)]
        [string] $Description
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description was not found: $Path"
    }

    return (Resolve-Path -LiteralPath $Path).Path
}

function ConvertTo-ServiceCommandArgument {
    param(
        [Parameter(Mandatory)]
        [string] $Value
    )

    if ($Value.IndexOf('"') -ge 0 -or $Value.IndexOf("`r") -ge 0 -or $Value.IndexOf("`n") -ge 0) {
        throw 'A service command path contains an unsupported quote or newline character.'
    }

    return '"' + $Value + '"'
}

function Invoke-ServiceControl {
    param(
        [Parameter(Mandatory)]
        [string[]] $Arguments
    )

    $scPath = Join-Path $env:SystemRoot 'System32\sc.exe'
    $output = @(& $scPath @Arguments 2>&1)
    if ($LASTEXITCODE -ne 0) {
        $renderedOutput = ($output | Out-String).Trim()
        throw "sc.exe $($Arguments[0]) failed with exit code $LASTEXITCODE. $renderedOutput"
    }

    return $output
}

function Get-CSharpCompiler {
    $candidates = @(
        (Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
        (Join-Path $env:SystemRoot 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }

    throw 'The .NET Framework 4 C# compiler was not found. Enable/install .NET Framework 4.x, then retry.'
}

function Get-GatewayHealthUrl {
    param(
        [Parameter(Mandatory)]
        [string] $GatewayConfigPath
    )

    $config = Get-Content -LiteralPath $GatewayConfigPath -Raw | ConvertFrom-Json
    $listenHostProperty = $config.PSObject.Properties['listen_host']
    $listenPortProperty = $config.PSObject.Properties['listen_port']
    $listenHost = if ($null -ne $listenHostProperty -and -not [string]::IsNullOrWhiteSpace([string] $listenHostProperty.Value)) {
        [string] $listenHostProperty.Value
    }
    else {
        '127.0.0.1'
    }

    if ($listenHost -in @('0.0.0.0', '::', 'localhost')) {
        $listenHost = '127.0.0.1'
    }

    $listenPort = if ($null -ne $listenPortProperty) { [int] $listenPortProperty.Value } else { 8877 }
    if ($listenPort -lt 1 -or $listenPort -gt 65535) {
        throw "Gateway config contains an invalid listen_port: $listenPort"
    }

    $uriBuilder = [UriBuilder]::new('http', $listenHost, $listenPort, '/health')
    return $uriBuilder.Uri.AbsoluteUri
}

function Wait-GatewayHealthy {
    param(
        [Parameter(Mandatory)]
        [string] $HealthUrl,

        [Parameter(Mandatory)]
        [int] $TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastError = $null

    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $request = [Net.HttpWebRequest]::Create($HealthUrl)
            $request.Method = 'GET'
            $request.Proxy = $null
            $request.Timeout = 3000
            $request.ReadWriteTimeout = 3000
            $response = $request.GetResponse()
            try {
                $statusCode = [int] ([Net.HttpWebResponse] $response).StatusCode
                if ($statusCode -ge 200 -and $statusCode -lt 300) {
                    return
                }
                $lastError = "HTTP $statusCode"
            }
            finally {
                $response.Dispose()
            }
        }
        catch {
            $lastError = $_.Exception.Message
        }

        Start-Sleep -Milliseconds 500
    }

    throw "The gateway service did not become healthy at $HealthUrl within $TimeoutSeconds seconds. Last check: $lastError. See $serviceLogPath."
}

Assert-Administrator

$resolvedConfigPath = Resolve-RequiredFile -Path $ConfigPath -Description 'Gateway config'
$resolvedPythonwPath = Resolve-RequiredFile -Path $PythonwPath -Description 'Python windowless interpreter'
$resolvedGatewayScriptPath = Resolve-RequiredFile -Path $GatewayScriptPath -Description 'Cortex MCP gateway script'
$resolvedServiceSourcePath = Resolve-RequiredFile -Path $serviceSourcePath -Description 'Cortex gateway service source'
$healthUrl = Get-GatewayHealthUrl -GatewayConfigPath $resolvedConfigPath

New-Item -Path $runtimeDirectory -ItemType Directory -Force | Out-Null
New-Item -Path $logDirectory -ItemType Directory -Force | Out-Null

$compilerPath = Get-CSharpCompiler
$serviceProcessReference = Join-Path (Split-Path -Parent $compilerPath) 'System.ServiceProcess.dll'
$null = Resolve-RequiredFile -Path $serviceProcessReference -Description 'System.ServiceProcess assembly'
$temporaryExecutablePath = Join-Path $runtimeDirectory ('.CortexGatewayService.{0}.exe' -f [Guid]::NewGuid().ToString('N'))

try {
    $compilerOutput = @(
        & $compilerPath /nologo /optimize+ /target:winexe "/out:$temporaryExecutablePath" "/reference:$serviceProcessReference" $resolvedServiceSourcePath 2>&1
    )
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $temporaryExecutablePath -PathType Leaf)) {
        throw "Cortex gateway service compilation failed. $(($compilerOutput | Out-String).Trim())"
    }

    $installedService = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($null -ne $installedService -and $installedService.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
        Stop-Service -Name $serviceName
        $installedService.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds($StopGraceSeconds + 20))
        $installedService.Refresh()
        if ($installedService.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
            throw "The existing $serviceName service did not stop cleanly. Its executable was not replaced."
        }
    }

    Move-Item -LiteralPath $temporaryExecutablePath -Destination $serviceExecutablePath -Force
}
finally {
    if (Test-Path -LiteralPath $temporaryExecutablePath -PathType Leaf) {
        Remove-Item -LiteralPath $temporaryExecutablePath -Force
    }
}

$serviceCommand = @(
    (ConvertTo-ServiceCommandArgument -Value $serviceExecutablePath),
    '--python',
    (ConvertTo-ServiceCommandArgument -Value $resolvedPythonwPath),
    '--script',
    (ConvertTo-ServiceCommandArgument -Value $resolvedGatewayScriptPath),
    '--config',
    (ConvertTo-ServiceCommandArgument -Value $resolvedConfigPath),
    '--log',
    (ConvertTo-ServiceCommandArgument -Value $serviceLogPath),
    '--stop-grace-seconds',
    [string] $StopGraceSeconds
) -join ' '

$serviceRecord = Get-CimInstance -ClassName Win32_Service -Filter "Name='$serviceName'" -ErrorAction SilentlyContinue
if ($null -eq $serviceRecord) {
    $registrationResult = Invoke-CimMethod -ClassName Win32_Service -MethodName Create -Arguments @{
        Name = $serviceName
        DisplayName = $displayName
        PathName = $serviceCommand
        ServiceType = [byte] 16
        ErrorControl = [byte] 1
        StartMode = 'Automatic'
        DesktopInteract = $false
        StartName = 'LocalSystem'
    }
}
else {
    # CIM passes PathName as one literal value, avoiding native-command quote loss.
    $registrationResult = Invoke-CimMethod -InputObject $serviceRecord -MethodName Change -Arguments @{
        DisplayName = $displayName
        PathName = $serviceCommand
        ServiceType = [byte] 16
        ErrorControl = [byte] 1
        StartMode = 'Automatic'
        DesktopInteract = $false
    }
}

if ([uint32] $registrationResult.ReturnValue -ne 0) {
    throw "Windows service registration failed with Win32_Service return code $($registrationResult.ReturnValue)."
}

# Delayed auto-start avoids competing with Windows logon while still starting without a user session.
$null = Invoke-ServiceControl -Arguments @('config', $serviceName, 'start=', 'delayed-auto')
$null = Invoke-ServiceControl -Arguments @('description', $serviceName, 'Cortex MCP gateway and Hindsight lifecycle owner (windowless Windows service).')
$null = Invoke-ServiceControl -Arguments @('failure', $serviceName, 'reset=', '86400', 'actions=', 'restart/5000/restart/15000/restart/30000')
$null = Invoke-ServiceControl -Arguments @('failureflag', $serviceName, '1')

Start-Service -Name $serviceName
$installedService = Get-Service -Name $serviceName
$installedService.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Running, [TimeSpan]::FromSeconds(30))
$installedService.Refresh()
if ($installedService.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Running) {
    throw "$serviceName did not enter the Running state. See Windows Event Viewer and $serviceLogPath."
}

Wait-GatewayHealthy -HealthUrl $healthUrl -TimeoutSeconds $StartupTimeoutSeconds

$serviceRecord = Get-CimInstance -ClassName Win32_Service -Filter "Name='$serviceName'"
[pscustomobject]@{
    ServiceName = $serviceName
    Status = $installedService.Status.ToString()
    StartMode = $serviceRecord.StartMode
    ServicePid = $serviceRecord.ProcessId
    HealthUrl = $healthUrl
    ServiceExecutable = $serviceExecutablePath
    GatewayConfig = $resolvedConfigPath
    ServiceLog = $serviceLogPath
}

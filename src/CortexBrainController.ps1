param(
    [string]$ConfigPath = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$script:ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $script:ProjectRoot "config\brain.json"
}

if (-not (Test-Path -LiteralPath $ConfigPath)) {
    throw "Cortex Brain configuration not found: $ConfigPath"
}

$script:Config = Get-Content -Raw -LiteralPath $ConfigPath | ConvertFrom-Json
$script:GatewayUrl = if ($null -ne $script:Config.PSObject.Properties["gatewayUrl"]) {
    ([string]$script:Config.gatewayUrl).TrimEnd("/")
}
else {
    "http://127.0.0.1:8877"
}
$gatewayToken = [Environment]::GetEnvironmentVariable("HINDSIGHT_MCP_API_KEY", "User")
if ([string]::IsNullOrWhiteSpace($gatewayToken)) {
    throw "HINDSIGHT_MCP_API_KEY is missing. Run scripts\Install-CortexBrain.ps1 first."
}
$script:GatewayHeaders = @{ Authorization = "Bearer $gatewayToken" }
$script:RuntimeDir = Join-Path $script:ProjectRoot "runtime"
$script:LogPath = Join-Path $script:RuntimeDir "controller.log"
$script:StatePath = Join-Path $script:RuntimeDir "state.json"
$script:PidPath = Join-Path $script:RuntimeDir "controller.pid"
$script:CommandPath = Join-Path $script:RuntimeDir "command.json"
$script:MemoryBrowserStartPath = Join-Path $script:ProjectRoot "scripts\Start-Cortex-MemoryBrowser.ps1"
$script:MemoryBrowserStopPath = Join-Path $script:ProjectRoot "scripts\Stop-Cortex-MemoryBrowser.ps1"
$null = New-Item -ItemType Directory -Path $script:RuntimeDir -Force

$createdNew = $false
$script:Mutex = New-Object System.Threading.Mutex($true, "Local\Cortex.Hindsight.Controller", [ref]$createdNew)
if (-not $createdNew) {
    exit 0
}

function Write-CortexLog {
    param(
        [string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR")]
        [string]$Level = "INFO"
    )

    $line = "{0:o} [{1}] {2}" -f (Get-Date), $Level, $Message
    Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
}

$script:State = [ordered]@{
    autoWakeEnabled = $true
    manualPaused = $false
    rearmSeenNoClients = $false
    controllerPid = $PID
    controllerStartedAt = (Get-Date).ToString("o")
}

if (Test-Path -LiteralPath $script:StatePath) {
    try {
        $saved = Get-Content -Raw -LiteralPath $script:StatePath | ConvertFrom-Json
        if ($null -ne $saved.autoWakeEnabled) {
            $script:State.autoWakeEnabled = [bool]$saved.autoWakeEnabled
        }
        if ($null -ne $saved.manualPaused) {
            $script:State.manualPaused = [bool]$saved.manualPaused
        }
        if ($null -ne $saved.rearmSeenNoClients) {
            $script:State.rearmSeenNoClients = [bool]$saved.rearmSeenNoClients
        }
    }
    catch {
        Write-CortexLog -Level "WARN" -Message "Ignoring unreadable runtime state: $($_.Exception.Message)"
    }
}

function Save-CortexState {
    $script:State.controllerPid = $PID
    $temporaryPath = "$($script:StatePath).tmp"
    $script:State | ConvertTo-Json | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    Move-Item -LiteralPath $temporaryPath -Destination $script:StatePath -Force
    Set-Content -LiteralPath $script:PidPath -Value $PID -Encoding ASCII
}

function Get-CortexPendingCommand {
    if (-not (Test-Path -LiteralPath $script:CommandPath)) {
        return $null
    }

    try {
        $request = Get-Content -Raw -LiteralPath $script:CommandPath | ConvertFrom-Json
        Remove-Item -LiteralPath $script:CommandPath -Force
        return $request
    }
    catch {
        Write-CortexLog -Level "WARN" -Message "Ignoring unreadable controller request: $($_.Exception.Message)"
        Remove-Item -LiteralPath $script:CommandPath -Force -ErrorAction SilentlyContinue
        return $null
    }
}

function Test-CortexHealth {
    try {
        $response = Invoke-RestMethod -Method Get -Uri "$($script:GatewayUrl)/health" -TimeoutSec 5
        return [bool]$response.upstream_healthy
    }
    catch {
        return $false
    }
}

function Test-CortexApiPortListening {
    try {
        $response = Invoke-RestMethod -Method Get -Uri "$($script:GatewayUrl)/health" -TimeoutSec 5
        return [bool]$response.upstream_listening
    }
    catch {
        return $false
    }
}

function Invoke-CortexGatewayRequest {
    param(
        [Parameter(Mandatory = $true)][ValidateSet("GET", "POST")][string]$Method,
        [Parameter(Mandatory = $true)][string]$Path,
        $Body = $null,
        [int]$TimeoutSeconds = 30
    )

    $parameters = @{
        Method = $Method
        Uri = "$($script:GatewayUrl)$Path"
        Headers = $script:GatewayHeaders
        TimeoutSec = $TimeoutSeconds
    }
    if ($null -ne $Body) {
        $parameters.ContentType = "application/json"
        $parameters.Body = ($Body | ConvertTo-Json -Compress -Depth 6)
    }
    return Invoke-RestMethod @parameters
}

function Get-CortexGatewayStatus {
    return Invoke-CortexGatewayRequest -Method GET -Path "/status" -TimeoutSeconds 5
}

function Get-CortexClientProcesses {
    $found = @()
    foreach ($name in $script:Config.processNames) {
        $found += @(Get-Process -Name $name -ErrorAction SilentlyContinue)
    }
    return @($found | Sort-Object -Property Id -Unique)
}

function Start-CortexBrain {
    param([switch]$Manual)

    Write-CortexLog -Message "Requesting Cortex wake through the Windows service."
    try {
        $result = Invoke-CortexGatewayRequest `
            -Method POST `
            -Path "/control/start" `
            -Body @{ manual = [bool]$Manual } `
            -TimeoutSeconds ([int]$script:Config.healthTimeoutSeconds + 30)
        $script:State.manualPaused = $false
        $script:State.rearmSeenNoClients = $false
        Save-CortexState
        Write-CortexLog -Message "Cortex service wake completed."
        return [bool]$result.upstream_healthy
    }
    catch {
        Write-CortexLog -Level "ERROR" -Message "Service wake failed: $($_.Exception.Message)"
        return $false
    }
}

function Stop-CortexBrain {
    param([switch]$Manual)

    Close-Cortex-MemoryBrowser
    Write-CortexLog -Message "Requesting deep sleep through the Windows service."
    try {
        $null = Invoke-CortexGatewayRequest `
            -Method POST `
            -Path "/control/stop" `
            -Body @{ manual = [bool]$Manual } `
            -TimeoutSeconds 60
    }
    catch {
        Write-CortexLog -Level "WARN" -Message "Service deep sleep reported: $($_.Exception.Message)"
    }

    if ($Manual) {
        $clients = @(Get-CortexClientProcesses)
        $script:State.manualPaused = $true
        $script:State.rearmSeenNoClients = ($clients.Count -eq 0)
    }
    Save-CortexState
}

function Open-CortexControlCenter {
    # The installed memory browser exposes the same banks and memories without
    # launching Hindsight's console-based control process.
    Open-Cortex-MemoryBrowser
}

function Open-Cortex-MemoryBrowser {
    try {
        if (-not (Test-Path -LiteralPath $script:MemoryBrowserStartPath)) {
            throw "Memory Browser launcher not found: $($script:MemoryBrowserStartPath)"
        }

        $powerShellPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
        $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$($script:MemoryBrowserStartPath)`""
        Start-Process -FilePath $powerShellPath -ArgumentList $arguments -WindowStyle Hidden
        Write-CortexLog -Message "Memory Browser launch requested."
    }
    catch {
        Write-CortexLog -Level "ERROR" -Message "Memory Browser failed: $($_.Exception.Message)"
        [System.Windows.Forms.MessageBox]::Show(
            "Could not open the Cortex Memory Browser. See controller.log.",
            "Cortex Brain",
            "OK",
            "Error"
        ) | Out-Null
    }
}

function Close-Cortex-MemoryBrowser {
    if (-not (Test-Path -LiteralPath $script:MemoryBrowserStopPath)) {
        return
    }

    try {
        & $script:MemoryBrowserStopPath -Quiet
    }
    catch {
        Write-CortexLog -Level "WARN" -Message "Memory Browser stop reported: $($_.Exception.Message)"
    }
}

function Open-CortexLogs {
    $profileLog = Join-Path $env:USERPROFILE ".hindsight\profiles\$($script:Config.profile).log"
    if (Test-Path -LiteralPath $profileLog) {
        Start-Process notepad.exe -ArgumentList @($profileLog)
    }
    else {
        Start-Process notepad.exe -ArgumentList @($script:LogPath)
    }
}

$script:CortexIcon = $null
$iconPath = Join-Path $script:ProjectRoot "assets\cortex.ico"
if (Test-Path -LiteralPath $iconPath) {
    try {
        $sourceIcon = New-Object System.Drawing.Icon($iconPath)
        try {
            $script:CortexIcon = $sourceIcon.Clone()
        }
        finally {
            $sourceIcon.Dispose()
        }
    }
    catch {
        Write-CortexLog -Level "WARN" -Message "Custom icon could not be loaded: $($_.Exception.Message)"
    }
}
else {
    Write-CortexLog -Level "WARN" -Message "Custom icon not found: $iconPath"
}

$script:NotifyIcon = New-Object System.Windows.Forms.NotifyIcon
if ($null -ne $script:CortexIcon) {
    $script:NotifyIcon.Icon = $script:CortexIcon
}
else {
    $script:NotifyIcon.Icon = [System.Drawing.SystemIcons]::Application
}
$script:NotifyIcon.Text = "Cortex Brain"
$script:NotifyIcon.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip
$script:StatusItem = New-Object System.Windows.Forms.ToolStripMenuItem
$script:StatusItem.Enabled = $false
$menu.Items.Add($script:StatusItem) | Out-Null
$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null

$startItem = New-Object System.Windows.Forms.ToolStripMenuItem "Start Brain"
$startItem.Add_Click({
    $script:State.manualPaused = $false
    $script:State.rearmSeenNoClients = $false
    Save-CortexState
    $null = Start-CortexBrain -Manual
})
$menu.Items.Add($startItem) | Out-Null

$stopItem = New-Object System.Windows.Forms.ToolStripMenuItem "Stop Brain (Deep Sleep)"
$stopItem.Add_Click({ Stop-CortexBrain -Manual })
$menu.Items.Add($stopItem) | Out-Null

$autoWakeItem = New-Object System.Windows.Forms.ToolStripMenuItem "Automatic Wake"
$autoWakeItem.CheckOnClick = $true
$autoWakeItem.Checked = [bool]$script:State.autoWakeEnabled
$autoWakeItem.Add_Click({
    try {
        $result = Invoke-CortexGatewayRequest `
            -Method POST `
            -Path "/control/auto-wake" `
            -Body @{ enabled = [bool]$autoWakeItem.Checked } `
            -TimeoutSeconds 10
        $script:State.autoWakeEnabled = [bool]$result.auto_wake_enabled
        $autoWakeItem.Checked = $script:State.autoWakeEnabled
        Save-CortexState
        Write-CortexLog -Message "Automatic wake set to $($script:State.autoWakeEnabled)."
    }
    catch {
        $autoWakeItem.Checked = -not [bool]$autoWakeItem.Checked
        Write-CortexLog -Level "ERROR" -Message "Automatic wake change failed: $($_.Exception.Message)"
    }
})
$menu.Items.Add($autoWakeItem) | Out-Null

$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
$memoryBrowserItem = New-Object System.Windows.Forms.ToolStripMenuItem "Open Memory Browser"
$memoryBrowserItem.Add_Click({ Open-Cortex-MemoryBrowser })
$menu.Items.Add($memoryBrowserItem) | Out-Null

$closeMemoryBrowserItem = New-Object System.Windows.Forms.ToolStripMenuItem "Close Memory Browser"
$closeMemoryBrowserItem.Add_Click({ Close-Cortex-MemoryBrowser })
$menu.Items.Add($closeMemoryBrowserItem) | Out-Null

$logsItem = New-Object System.Windows.Forms.ToolStripMenuItem "Open Logs"
$logsItem.Add_Click({ Open-CortexLogs })
$menu.Items.Add($logsItem) | Out-Null

$folderItem = New-Object System.Windows.Forms.ToolStripMenuItem "Open Cortex Folder"
$folderItem.Add_Click({ Start-Process explorer.exe -ArgumentList @($script:ProjectRoot) })
$menu.Items.Add($folderItem) | Out-Null

$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator)) | Out-Null
$exitItem = New-Object System.Windows.Forms.ToolStripMenuItem "Exit Tray (Brain Keeps Running)"
$exitItem.Add_Click({
    $script:NotifyIcon.Visible = $false
    [System.Windows.Forms.Application]::Exit()
})
$menu.Items.Add($exitItem) | Out-Null

$script:NotifyIcon.ContextMenuStrip = $menu
$script:NotifyIcon.Add_DoubleClick({ Open-Cortex-MemoryBrowser })

$script:NoClientSince = $null
$script:TickBusy = $false
$script:LastStartFailure = $null
$script:IdleCleanupAttempted = $false
$script:Timer = New-Object System.Windows.Forms.Timer
$script:Timer.Interval = [Math]::Max(1000, ([int]$script:Config.pollSeconds * 1000))
$script:Timer.Add_Tick({
    if ($script:TickBusy) {
        return
    }

    $script:TickBusy = $true
    try {
        $request = Get-CortexPendingCommand
        if ($null -ne $request) {
            if ([string]$request.action -eq "stop") {
                Stop-CortexBrain -Manual
            }
            elseif ([string]$request.action -eq "start") {
                $script:State.manualPaused = $false
                $script:State.rearmSeenNoClients = $false
                Save-CortexState
                $null = Start-CortexBrain -Manual
            }
            elseif ([string]$request.action -eq "exit") {
                Write-CortexLog -Message "Controller shutdown requested for maintenance."
                $script:Timer.Stop()
                [System.Windows.Forms.Application]::ExitThread()
                return
            }
        }

        # The service is the sole lifecycle owner. The tray only renders status
        # and sends explicit control requests, so exiting it cannot kill Cortex.
        $gatewayStatus = Get-CortexGatewayStatus
        $healthy = [bool]$gatewayStatus.upstream_healthy
        $apiListening = $healthy -or [bool]$gatewayStatus.upstream_listening
        $script:State.manualPaused = [bool]$gatewayStatus.manual_paused
        $script:State.autoWakeEnabled = [bool]$gatewayStatus.auto_wake_enabled
        if ($autoWakeItem.Checked -ne $script:State.autoWakeEnabled) {
            $autoWakeItem.Checked = $script:State.autoWakeEnabled
        }

        if ($script:State.manualPaused) {
            $script:StatusItem.Text = "Status: Paused"
            $script:NotifyIcon.Text = "Cortex Brain - Paused"
        }
        elseif ($healthy) {
            $script:StatusItem.Text = "Status: Ready"
            $script:NotifyIcon.Text = "Cortex Brain - Ready"
        }
        elseif ([string]$gatewayStatus.lifecycle_state -in @("starting", "waking")) {
            $script:StatusItem.Text = "Status: Waking"
            $script:NotifyIcon.Text = "Cortex Brain - Waking"
        }
        elseif ($apiListening) {
            $script:StatusItem.Text = "Status: Busy"
            $script:NotifyIcon.Text = "Cortex Brain - Busy"
        }
        else {
            $script:StatusItem.Text = "Status: Deep Sleep"
            $script:NotifyIcon.Text = "Cortex Brain - Deep Sleep"
        }
    }
    catch {
        Write-CortexLog -Level "ERROR" -Message "Controller tick failed: $($_.Exception.Message)"
        $script:StatusItem.Text = "Status: Faulted"
        $script:NotifyIcon.Text = "Cortex Brain - Faulted"
    }
    finally {
        $script:TickBusy = $false
    }
})

$exitCode = 0
try {
    Save-CortexState
    Write-CortexLog -Message "Controller started. Auto-wake=$($script:State.autoWakeEnabled). Icon=$iconPath"
    $script:Timer.Start()
    [System.Windows.Forms.Application]::Run()
    Write-CortexLog -Message "Controller exited normally."
}
catch {
    $exitCode = 1
    try {
        Write-CortexLog -Level "ERROR" -Message "Fatal controller error: $($_.Exception.ToString())"
    }
    catch {
        # Preserve the original fatal error even if logging also fails.
    }
}
finally {
    Close-Cortex-MemoryBrowser
    $script:Timer.Stop()
    $script:NotifyIcon.Visible = $false
    $script:NotifyIcon.Dispose()
    $menu.Dispose()
    if ($null -ne $script:CortexIcon) {
        $script:CortexIcon.Dispose()
    }
    Remove-Item -LiteralPath $script:PidPath -Force -ErrorAction SilentlyContinue
    $script:Mutex.ReleaseMutex()
    $script:Mutex.Dispose()
}

exit $exitCode

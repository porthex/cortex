param(
    [string]$ConfigPath = ""
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $ConfigPath = Join-Path $projectRoot "config\gateway.json"
}

$brainPath = Join-Path $projectRoot "config\brain.json"
if (-not (Test-Path -LiteralPath $brainPath)) {
    throw "Cortex Brain configuration not found: $brainPath"
}
$brain = Get-Content -Raw -LiteralPath $brainPath | ConvertFrom-Json

$userProfile = [Environment]::GetFolderPath("UserProfile")
$appData = [Environment]::GetFolderPath("ApplicationData")
$localAppData = [Environment]::GetFolderPath("LocalApplicationData")
$hindsightExe = Join-Path $userProfile ".local\bin\hindsight-embed.exe"
$pythonwExe = Join-Path $appData "uv\tools\hindsight-embed\Scripts\pythonw.exe"
$ollamaExe = Join-Path $localAppData "Programs\Ollama\ollama.exe"
$pgCtlExe = Get-ChildItem -LiteralPath (Join-Path $userProfile ".pg0\installation") -Directory -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending |
    ForEach-Object { Join-Path $_.FullName "bin\pg_ctl.exe" } |
    Where-Object { Test-Path -LiteralPath $_ } |
    Select-Object -First 1
$postgresDataDir = Join-Path $userProfile ".pg0\instances\hindsight-embed-$($brain.profile)\data"

foreach ($requiredPath in @($hindsightExe, $pythonwExe, $ollamaExe, $pgCtlExe, $postgresDataDir)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required Cortex runtime was not found: $requiredPath"
    }
}

$token = [Environment]::GetEnvironmentVariable("HINDSIGHT_MCP_API_KEY", "User")
if ([string]::IsNullOrWhiteSpace($token)) {
    $profileEnvPath = Join-Path $userProfile ".hindsight\profiles\$($brain.profile).env"
    if (Test-Path -LiteralPath $profileEnvPath) {
        $tokenLine = Get-Content -LiteralPath $profileEnvPath | Where-Object {
            $_ -match '^HINDSIGHT_API_MCP_AUTH_TOKEN='
        } | Select-Object -First 1
        if (-not [string]::IsNullOrWhiteSpace($tokenLine)) {
            $token = $tokenLine.Substring($tokenLine.IndexOf('=') + 1).Trim().Trim('"').Trim("'")
        }
    }
}
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "HINDSIGHT_MCP_API_KEY is missing. Run Install-CortexBrain.ps1 first."
}

$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $hashBytes = $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($token))
}
finally {
    $sha256.Dispose()
}
$tokenHash = -join ($hashBytes | ForEach-Object { $_.ToString("x2") })

$runtimeDir = Join-Path $projectRoot "runtime"
$logDir = Join-Path $projectRoot "logs"
$null = New-Item -ItemType Directory -Path $runtimeDir -Force
$null = New-Item -ItemType Directory -Path $logDir -Force
$postgresLogPath = Join-Path $logDir "postgres.log"
$postgresDatabaseUrl = "postgresql://hindsight:hindsight@127.0.0.1:5432/hindsight"

$nodeRoot = Get-ChildItem -LiteralPath (Join-Path $localAppData "Programs") -Directory -Filter "node-*" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 -ExpandProperty FullName
$pathPrepend = @(
    (Split-Path -Parent $hindsightExe),
    (Split-Path -Parent $pythonwExe),
    (Split-Path -Parent $ollamaExe),
    $nodeRoot
) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

$homeDrive = [IO.Path]::GetPathRoot($userProfile).TrimEnd('\')
$homePath = $userProfile.Substring($homeDrive.Length)
$config = [ordered]@{
    schema_version = 1
    listen_host = "127.0.0.1"
    listen_port = 8877
    upstream_url = ([string]$brain.apiUrl).TrimEnd('/')
    upstream_health_url = (([string]$brain.apiUrl).TrimEnd('/') + "/health")
    profile = [string]$brain.profile
    bank_id = [string]$brain.bankId
    model = [string]$brain.model
    hindsight_exe = $hindsightExe
    ollama_exe = $ollamaExe
    # Hindsight runs as LocalSystem for a fully windowless Session-0 lifecycle,
    # but PostgreSQL must stay on the user's existing Cortex bank. Manage that
    # exact data directory explicitly instead of allowing pg0 to choose the
    # service account's home directory.
    postgres_host = "127.0.0.1"
    postgres_port = 5432
    postgres_data_dir = $postgresDataDir
    postgres_pg_ctl_exe = $pgCtlExe
    postgres_start_command = @($pgCtlExe, "start", "-D", $postgresDataDir, "-l", $postgresLogPath, "-o", "-p 5432", "-w", "-t", "120")
    postgres_stop_command = @($pgCtlExe, "stop", "-D", $postgresDataDir, "-m", "fast", "-w", "-t", "120")
    working_directory = $projectRoot
    user_profile = $userProfile
    appdata = $appData
    localappdata = $localAppData
    home_drive = $homeDrive
    home_path = $homePath
    path_prepend = @($pathPrepend)
    command_environment = [ordered]@{
        HINDSIGHT_EMBED_API_DATABASE_URL = $postgresDatabaseUrl
    }
    auth_token_sha256 = $tokenHash
    log_path = (Join-Path $logDir "gateway.log")
    startup_log_path = (Join-Path $logDir "hindsight-service.log")
    state_path = (Join-Path $runtimeDir "gateway-state.json")
    poll_seconds = [Math]::Max(1, [int]$brain.pollSeconds)
    deep_sleep_delay_seconds = [Math]::Max(30, [int]$brain.deepSleepDelaySeconds)
    # A first cold start may need to initialize PostgreSQL and the local model.
    health_timeout_seconds = [Math]::Max(720, [int]$brain.healthTimeoutSeconds)
    health_request_timeout_seconds = 1.5
    postgres_timeout_seconds = 150
    stop_timeout_seconds = 60
    start_retry_delay_seconds = [Math]::Max(15, [int]$brain.startRetryDelaySeconds)
    monitor_processes = $true
    process_names = @($brain.processNames)
    auto_wake_enabled = $true
}

$parent = Split-Path -Parent $ConfigPath
$null = New-Item -ItemType Directory -Path $parent -Force
$temporaryPath = Join-Path $parent ("gateway.{0}.tmp" -f [Guid]::NewGuid().ToString("N"))
$utf8NoBom = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText($temporaryPath, ($config | ConvertTo-Json -Depth 8), $utf8NoBom)
Move-Item -LiteralPath $temporaryPath -Destination $ConfigPath -Force

Write-Output "Cortex gateway configuration written to $ConfigPath (token stored as SHA-256 only)."

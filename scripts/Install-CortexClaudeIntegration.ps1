[CmdletBinding()]
param(
    [Parameter()]
    [string] $ClaudeConfigPath,

    [Parameter()]
    [string] $InstallDirectory,

    [Parameter()]
    [string] $GatewayUrl = 'http://127.0.0.1:8877/mcp/cortex/',

    [Parameter()]
    [ValidateRange(5, 1800)]
    [int] $LiveTestTimeoutSeconds = 900,

    [Parameter()]
    [switch] $SkipLiveTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$bridgeSourcePath = Join-Path $projectRoot 'src\CortexMcpStdioBridge.cs'
$testScriptPath = Join-Path $PSScriptRoot 'Test-CortexClaudeIntegration.ps1'
if ([string]::IsNullOrWhiteSpace($InstallDirectory)) {
    $InstallDirectory = Join-Path $env:LOCALAPPDATA 'Cortex\bin'
}

function Get-CSharpCompiler {
    $candidates = @(
        (Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
        (Join-Path $env:SystemRoot 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw 'The .NET Framework 4 C# compiler was not found. Enable/install .NET Framework 4.x, then retry.'
}

function Assert-LocalGatewayUrl {
    param(
        [Parameter(Mandatory)]
        [string] $Url
    )

    $uri = $null
    if (-not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref] $uri) -or
        -not $uri.IsLoopback -or
        $uri.Scheme -ne [Uri]::UriSchemeHttp) {
        throw 'GatewayUrl must be an http:// loopback URL.'
    }

    if (-not $uri.AbsolutePath.EndsWith('/')) {
        $builder = [UriBuilder]::new($uri)
        $builder.Path += '/'
        $uri = $builder.Uri
    }

    return $uri.AbsoluteUri
}

function Get-RunningClaudePaths {
    $paths = New-Object 'System.Collections.Generic.List[string]'
    foreach ($process in @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -match '(?i)claude'
    })) {
        try {
            if (-not [string]::IsNullOrWhiteSpace([string] $process.Path)) {
                $paths.Add([IO.Path]::GetFullPath([string] $process.Path))
            }
        }
        catch {
            # Some packaged processes do not expose Path without elevation.
        }
    }

    return $paths.ToArray()
}

function Get-ClaudePackages {
    $command = Get-Command -Name Get-AppxPackage -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return @()
    }

    try {
        return @(Get-AppxPackage -ErrorAction Stop | Where-Object {
            ([string] $_.Name) -match '(?i)claude|anthropic' -or
            ([string] $_.PackageFullName) -match '(?i)claude|anthropic'
        })
    }
    catch {
        return @()
    }
}

function Test-PathUnderDirectory {
    param(
        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter(Mandatory)]
        [string] $Directory
    )

    if ([string]::IsNullOrWhiteSpace($Path) -or [string]::IsNullOrWhiteSpace($Directory)) {
        return $false
    }

    $directoryPrefix = [IO.Path]::GetFullPath($Directory).TrimEnd('\') + '\'
    return [IO.Path]::GetFullPath($Path).StartsWith(
        $directoryPrefix, [StringComparison]::OrdinalIgnoreCase)
}

function Resolve-ClaudeConfigPath {
    param(
        [Parameter()]
        [string] $ExplicitPath
    )

    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        return [IO.Path]::GetFullPath($ExplicitPath)
    }

    $standardPath = Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'
    $packages = @(Get-ClaudePackages)
    $runningPaths = @(Get-RunningClaudePaths)
    $packageCandidates = New-Object 'System.Collections.Generic.List[object]'

    foreach ($package in $packages) {
        $installLocation = [string] $package.InstallLocation
        $active = $false
        foreach ($runningPath in $runningPaths) {
            if (-not [string]::IsNullOrWhiteSpace($installLocation) -and
                (Test-PathUnderDirectory -Path $runningPath -Directory $installLocation)) {
                $active = $true
                break
            }
        }

        $familyName = [string] $package.PackageFamilyName
        if ([string]::IsNullOrWhiteSpace($familyName)) {
            continue
        }

        $packageRoot = Join-Path (Join-Path $env:LOCALAPPDATA 'Packages') $familyName
        $paths = @(
            (Join-Path $packageRoot 'LocalCache\Roaming\Claude\claude_desktop_config.json'),
            (Join-Path $packageRoot 'LocalState\Claude\claude_desktop_config.json'),
            (Join-Path $packageRoot 'LocalState\claude_desktop_config.json')
        )

        foreach ($path in $paths) {
            $packageCandidates.Add([pscustomobject]@{
                Path = $path
                Active = $active
                Exists = Test-Path -LiteralPath $path -PathType Leaf
                Primary = $path -like '*LocalCache\Roaming\Claude\claude_desktop_config.json'
            })
        }
    }

    $activeExisting = @($packageCandidates | Where-Object { $_.Active -and $_.Exists } |
        Sort-Object { (Get-Item -LiteralPath $_.Path).LastWriteTimeUtc } -Descending)
    if ($activeExisting.Count -gt 0) {
        return [IO.Path]::GetFullPath([string] $activeExisting[0].Path)
    }

    $anyExisting = @($packageCandidates | Where-Object { $_.Exists } |
        Sort-Object { (Get-Item -LiteralPath $_.Path).LastWriteTimeUtc } -Descending)
    if ($anyExisting.Count -gt 0) {
        return [IO.Path]::GetFullPath([string] $anyExisting[0].Path)
    }

    if (Test-Path -LiteralPath $standardPath -PathType Leaf) {
        return [IO.Path]::GetFullPath($standardPath)
    }

    $activePrimary = @($packageCandidates | Where-Object { $_.Active -and $_.Primary })
    if ($activePrimary.Count -gt 0) {
        return [IO.Path]::GetFullPath([string] $activePrimary[0].Path)
    }

    return [IO.Path]::GetFullPath($standardPath)
}

function Set-ObjectProperty {
    param(
        [Parameter(Mandatory)]
        [object] $InputObject,

        [Parameter(Mandatory)]
        [string] $Name,

        [Parameter()]
        [AllowNull()]
        [object] $Value
    )

    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property) {
        $InputObject | Add-Member -MemberType NoteProperty -Name $Name -Value $Value
    }
    else {
        $property.Value = $Value
    }
}

function Install-BridgeExecutable {
    param(
        [Parameter(Mandatory)]
        [string] $SourcePath,

        [Parameter(Mandatory)]
        [string] $DestinationDirectory
    )

    if (-not (Test-Path -LiteralPath $SourcePath -PathType Leaf)) {
        throw "The Cortex bridge source was not found: $SourcePath"
    }

    $null = New-Item -ItemType Directory -Path $DestinationDirectory -Force
    $compiler = Get-CSharpCompiler
    $frameworkDirectory = Split-Path -Parent $compiler
    $httpAssembly = Join-Path $frameworkDirectory 'System.Net.Http.dll'
    $webExtensionsAssembly = Join-Path $frameworkDirectory 'System.Web.Extensions.dll'
    if (-not (Test-Path -LiteralPath $httpAssembly -PathType Leaf) -or
        -not (Test-Path -LiteralPath $webExtensionsAssembly -PathType Leaf)) {
        throw 'Required .NET Framework assemblies (System.Net.Http and System.Web.Extensions) were not found.'
    }

    $destination = Join-Path $DestinationDirectory 'CortexMcpStdioBridge.exe'
    $temporaryExecutable = Join-Path $DestinationDirectory (
        '.CortexMcpStdioBridge.' + [Guid]::NewGuid().ToString('N') + '.exe')
    try {
        $arguments = @(
            '/nologo',
            '/target:winexe',
            '/optimize+',
            '/platform:anycpu',
            "/out:$temporaryExecutable",
            "/reference:$httpAssembly",
            "/reference:$webExtensionsAssembly",
            $SourcePath
        )
        $compilerOutput = @(& $compiler @arguments 2>&1)
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $temporaryExecutable -PathType Leaf)) {
            $details = ($compilerOutput | Out-String).Trim()
            throw "Compiling CortexMcpStdioBridge failed with exit code $LASTEXITCODE. $details"
        }

        if (Test-Path -LiteralPath $destination -PathType Leaf) {
            $executableBackup = $destination + '.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmssfff')
            [IO.File]::Replace($temporaryExecutable, $destination, $executableBackup, $true)
        }
        else {
            [IO.File]::Move($temporaryExecutable, $destination)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryExecutable -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryExecutable -Force
        }
    }

    return (Resolve-Path -LiteralPath $destination).Path
}

function Update-ClaudeConfig {
    param(
        [Parameter(Mandatory)]
        [string] $Path,

        [Parameter(Mandatory)]
        [string] $BridgePath,

        [Parameter(Mandatory)]
        [string] $Url,

        [Parameter(Mandatory)]
        [int] $TimeoutSeconds
    )

    $directory = Split-Path -Parent $Path
    $null = New-Item -ItemType Directory -Path $directory -Force
    $exists = Test-Path -LiteralPath $Path -PathType Leaf

    if ($exists) {
        $rawJson = [IO.File]::ReadAllText($Path)
        if ([string]::IsNullOrWhiteSpace($rawJson)) {
            $config = [pscustomobject] [ordered]@{}
        }
        else {
            try {
                $config = $rawJson | ConvertFrom-Json
            }
            catch {
                throw "Claude's existing config is not valid JSON and was left unchanged: $Path"
            }
        }
    }
    else {
        $config = [pscustomobject] [ordered]@{}
    }

    if ($null -eq $config -or $config -is [Array] -or $config -is [ValueType] -or $config -is [string]) {
        throw "Claude's config root must be a JSON object and was left unchanged: $Path"
    }

    $mcpServersProperty = $config.PSObject.Properties['mcpServers']
    if ($null -eq $mcpServersProperty -or $null -eq $mcpServersProperty.Value) {
        $mcpServers = [pscustomobject] [ordered]@{}
        Set-ObjectProperty -InputObject $config -Name 'mcpServers' -Value $mcpServers
    }
    else {
        $mcpServers = $mcpServersProperty.Value
        if ($mcpServers -is [Array] -or $mcpServers -is [ValueType] -or $mcpServers -is [string]) {
            throw "Claude's mcpServers value must be a JSON object and was left unchanged: $Path"
        }
    }

    $cortexDefinition = [pscustomobject] [ordered]@{
        command = $BridgePath
        args = @('--url', $Url, '--timeout-seconds', [string] $TimeoutSeconds)
    }
    Set-ObjectProperty -InputObject $mcpServers -Name 'cortex' -Value $cortexDefinition

    $updatedJson = $config | ConvertTo-Json -Depth 100
    $temporaryConfig = Join-Path $directory (
        '.' + [IO.Path]::GetFileName($Path) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backupPath = $null
    try {
        [IO.File]::WriteAllText($temporaryConfig, $updatedJson + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false))

        # ReplaceFile is atomic on the same NTFS volume and preserves the exact old file as a backup.
        if ($exists) {
            $backupPath = $Path + '.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmssfff')
            [IO.File]::Replace($temporaryConfig, $Path, $backupPath, $true)
        }
        else {
            [IO.File]::Move($temporaryConfig, $Path)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryConfig -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryConfig -Force
        }
    }

    return $backupPath
}

$GatewayUrl = Assert-LocalGatewayUrl -Url $GatewayUrl
$token = [Environment]::GetEnvironmentVariable('HINDSIGHT_MCP_API_KEY', 'User')
if ([string]::IsNullOrWhiteSpace($token)) {
    throw 'HINDSIGHT_MCP_API_KEY is missing from the Windows user environment. Install Cortex first.'
}
$token = $null

$resolvedConfigPath = Resolve-ClaudeConfigPath -ExplicitPath $ClaudeConfigPath
$resolvedInstallDirectory = [IO.Path]::GetFullPath($InstallDirectory)
$bridgePath = Install-BridgeExecutable -SourcePath $bridgeSourcePath `
    -DestinationDirectory $resolvedInstallDirectory

$liveTestPassed = $false
if (-not $SkipLiveTest) {
    if (-not (Test-Path -LiteralPath $testScriptPath -PathType Leaf)) {
        throw "The Claude bridge test script was not found: $testScriptPath"
    }

    $null = & $testScriptPath -BridgePath $bridgePath -GatewayUrl $GatewayUrl `
        -TimeoutSeconds $LiveTestTimeoutSeconds
    $liveTestPassed = $true
}

$backupPath = Update-ClaudeConfig -Path $resolvedConfigPath -BridgePath $bridgePath `
    -Url $GatewayUrl -TimeoutSeconds $LiveTestTimeoutSeconds
$claudeRunning = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -match '(?i)claude'
}).Count -gt 0

[pscustomobject]@{
    BridgePath = $bridgePath
    ClaudeConfigPath = $resolvedConfigPath
    ConfigBackupPath = $backupPath
    LiveTestPassed = $liveTestPassed
    ClaudeWasRunning = $claudeRunning
    RestartClaudeRequired = $claudeRunning
    NextClaudeLaunchLoadsIntegration = $true
    TokenStoredInClaudeConfig = $false
}

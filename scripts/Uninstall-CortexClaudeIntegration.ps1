[CmdletBinding()]
param(
    [Parameter()]
    [string] $ClaudeConfigPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ClaudeConfigCandidates {
    param(
        [Parameter()]
        [string] $ExplicitPath
    )

    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        return @([IO.Path]::GetFullPath($ExplicitPath))
    }

    $paths = New-Object 'System.Collections.Generic.List[string]'
    $paths.Add((Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'))

    if ($null -ne (Get-Command -Name Get-AppxPackage -ErrorAction SilentlyContinue)) {
        try {
            $packages = @(Get-AppxPackage -ErrorAction Stop | Where-Object {
                ([string] $_.Name) -match '(?i)claude|anthropic' -or
                ([string] $_.PackageFullName) -match '(?i)claude|anthropic'
            })
            foreach ($package in $packages) {
                $familyName = [string] $package.PackageFamilyName
                if ([string]::IsNullOrWhiteSpace($familyName)) {
                    continue
                }

                $packageRoot = Join-Path (Join-Path $env:LOCALAPPDATA 'Packages') $familyName
                $paths.Add((Join-Path $packageRoot 'LocalCache\Roaming\Claude\claude_desktop_config.json'))
                $paths.Add((Join-Path $packageRoot 'LocalState\Claude\claude_desktop_config.json'))
                $paths.Add((Join-Path $packageRoot 'LocalState\claude_desktop_config.json'))
            }
        }
        catch {
            # The standard desktop config can still be handled if Appx discovery is unavailable.
        }
    }

    $seen = New-Object 'System.Collections.Generic.HashSet[string]' (
        [StringComparer]::OrdinalIgnoreCase)
    $result = New-Object 'System.Collections.Generic.List[string]'
    foreach ($path in $paths) {
        $fullPath = [IO.Path]::GetFullPath($path)
        if ($seen.Add($fullPath)) {
            $result.Add($fullPath)
        }
    }

    return $result.ToArray()
}

function Remove-CortexServerFromConfig {
    param(
        [Parameter(Mandatory)]
        [string] $Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }

    $rawJson = [IO.File]::ReadAllText($Path)
    if ([string]::IsNullOrWhiteSpace($rawJson)) {
        return $null
    }

    try {
        $config = $rawJson | ConvertFrom-Json
    }
    catch {
        throw "Claude's config is not valid JSON and was left unchanged: $Path"
    }

    if ($null -eq $config -or $config -is [Array] -or $config -is [ValueType] -or $config -is [string]) {
        throw "Claude's config root is not a JSON object and was left unchanged: $Path"
    }

    $mcpServersProperty = $config.PSObject.Properties['mcpServers']
    if ($null -eq $mcpServersProperty -or $null -eq $mcpServersProperty.Value) {
        return $null
    }

    $cortexProperty = $mcpServersProperty.Value.PSObject.Properties['cortex']
    if ($null -eq $cortexProperty) {
        return $null
    }

    $mcpServersProperty.Value.PSObject.Properties.Remove('cortex')
    $updatedJson = $config | ConvertTo-Json -Depth 100
    $directory = Split-Path -Parent $Path
    $temporaryConfig = Join-Path $directory (
        '.' + [IO.Path]::GetFileName($Path) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $backupPath = $Path + '.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmssfff')

    try {
        [IO.File]::WriteAllText($temporaryConfig, $updatedJson + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false))
        [IO.File]::Replace($temporaryConfig, $Path, $backupPath, $true)
    }
    finally {
        if (Test-Path -LiteralPath $temporaryConfig -PathType Leaf) {
            Remove-Item -LiteralPath $temporaryConfig -Force
        }
    }

    return [pscustomobject]@{
        ConfigPath = $Path
        BackupPath = $backupPath
    }
}

$modified = New-Object 'System.Collections.Generic.List[object]'
foreach ($candidate in @(Get-ClaudeConfigCandidates -ExplicitPath $ClaudeConfigPath)) {
    $result = Remove-CortexServerFromConfig -Path $candidate
    if ($null -ne $result) {
        $modified.Add($result)
    }
}
$claudeRunning = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -match '(?i)claude'
}).Count -gt 0

[pscustomobject]@{
    IntegrationFound = $modified.Count -gt 0
    ModifiedConfigs = $modified.ToArray()
    BridgeBinaryPreserved = $true
    UserDataDeleted = $false
    RestartClaudeRequired = $modified.Count -gt 0 -and $claudeRunning
}

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$integrationRoot = Join-Path $env:USERPROFILE ".hindsight\codex"
$userHindsightConfig = Join-Path $env:USERPROFILE ".hindsight\codex.json"
$codexRoot = Join-Path $env:USERPROFILE ".codex"
$codexConfigPath = Join-Path $codexRoot "config.toml"
$hooksPath = Join-Path $codexRoot "hooks.json"
$agentsPath = Join-Path $codexRoot "AGENTS.md"
$beginMarker = "<!-- BEGIN CORTEX HINDSIGHT MEMORY -->"
$endMarker = "<!-- END CORTEX HINDSIGHT MEMORY -->"

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content
    )

    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Content, $utf8)
}

function Test-CortexHookGroup {
    param($Group)

    if ($null -eq $Group -or $null -eq $Group.hooks) {
        return $false
    }
    foreach ($hook in @($Group.hooks)) {
        $commandWindowsProperty = $hook.PSObject.Properties["commandWindows"]
        $commandWindows = if ($null -ne $commandWindowsProperty) { [string]$commandWindowsProperty.Value } else { "" }
        $commands = @([string]$hook.command, $commandWindows)
        if ($commands -match '(?i)\.hindsight[\\/]codex[\\/]scripts[\\/](CortexCodexHookBridge|session_start|recall|retain)\.py') {
            return $true
        }
    }
    return $false
}

if (Test-Path -LiteralPath $hooksPath) {
    $hooksConfig = Get-Content -Raw -LiteralPath $hooksPath | ConvertFrom-Json
    if ($null -ne $hooksConfig.PSObject.Properties["hooks"]) {
        foreach ($eventName in @("SessionStart", "UserPromptSubmit", "Stop")) {
            $eventProperty = $hooksConfig.hooks.PSObject.Properties[$eventName]
            if ($null -eq $eventProperty) {
                continue
            }
            $keptGroups = @(@($eventProperty.Value) | Where-Object { -not (Test-CortexHookGroup -Group $_) })
            if ($keptGroups.Count -gt 0) {
                $eventProperty.Value = $keptGroups
            }
            else {
                $hooksConfig.hooks.PSObject.Properties.Remove($eventName)
            }
        }
    }
    Write-Utf8NoBom -Path $hooksPath -Content ($hooksConfig | ConvertTo-Json -Depth 12)
}

if (Test-Path -LiteralPath $agentsPath) {
    $agentsContent = [IO.File]::ReadAllText($agentsPath)
    $blockPattern = [regex]::Escape($beginMarker) + '.*?' + [regex]::Escape($endMarker)
    $agentsContent = [regex]::Replace(
        $agentsContent,
        $blockPattern,
        "",
        [Text.RegularExpressions.RegexOptions]::Singleline
    ).Trim()
    if ($agentsContent.Length -gt 0) {
        $agentsContent += "`r`n"
    }
    Write-Utf8NoBom -Path $agentsPath -Content $agentsContent
}

if (Test-Path -LiteralPath $codexConfigPath) {
    $codexConfig = [IO.File]::ReadAllText($codexConfigPath)
    $codexConfig = [regex]::Replace(
        $codexConfig,
        '(?ms)^\[mcp_servers\.hindsight(?:\.[^\]]+)?\]\s*\r?\n.*?(?=^\[|\z)',
        ''
    )
    $codexConfig = [regex]::Replace($codexConfig.Trim(), '(\r?\n){3,}', "`r`n`r`n")
    if ($codexConfig.Length -gt 0) {
        $codexConfig += "`r`n"
    }
    Write-Utf8NoBom -Path $codexConfigPath -Content $codexConfig
}

$resolvedIntegration = [IO.Path]::GetFullPath($integrationRoot)
$expectedIntegration = [IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".hindsight\codex"))
if ($resolvedIntegration -ne $expectedIntegration) {
    throw "Refusing to remove an unexpected Hindsight Codex integration path."
}
if (Test-Path -LiteralPath $integrationRoot) {
    Remove-Item -LiteralPath $integrationRoot -Recurse -Force
}
# Preserve ~/.hindsight/codex.json because it may predate Cortex and is not
# needed by the persistent MCP transport. Removing it wholesale could destroy
# unrelated user Hindsight settings.

Write-Output "Cortex's Codex MCP server, managed hooks, and global memory guidance were removed."

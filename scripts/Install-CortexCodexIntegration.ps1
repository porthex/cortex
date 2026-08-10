param(
    [switch]$DoNotWakeBrain
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$integrationRoot = Join-Path $env:USERPROFILE ".hindsight\codex"
$integrationScripts = Join-Path $integrationRoot "scripts"
$integrationLib = Join-Path $integrationScripts "lib"
$userHindsightConfig = Join-Path $env:USERPROFILE ".hindsight\codex.json"
$codexRoot = Join-Path $env:USERPROFILE ".codex"
$hooksPath = Join-Path $codexRoot "hooks.json"
$codexConfigPath = Join-Path $codexRoot "config.toml"
$agentsPath = Join-Path $codexRoot "AGENTS.md"
$bridgeSource = Join-Path $projectRoot "src\CortexCodexHookBridge.py"
$bridgeDestination = Join-Path $integrationScripts "CortexCodexHookBridge.py"
$configTemplatePath = Join-Path $projectRoot "config\codex-hindsight.json"
$bankPolicyPath = Join-Path $projectRoot "config\hindsight-bank-policy.json"
$installedBankPolicyPath = Join-Path $integrationRoot "cortex-bank-policy.json"
$agentPolicyPath = Join-Path $projectRoot "config\codex-memory-policy.md"
$policyReadyPath = Join-Path $integrationRoot "state\policy-ready.json"
$stagingRoot = Join-Path ([IO.Path]::GetTempPath()) ("CortexCodexInstall-" + [Guid]::NewGuid().ToString("N"))
$stagingScripts = Join-Path $stagingRoot "scripts"
$stagingLib = Join-Path $stagingScripts "lib"
$sourceCommit = "327aa05e80c89e2f02e9122123469f8b0bd91d0c"
$sourceBase = "https://raw.githubusercontent.com/vectorize-io/hindsight/$sourceCommit/hindsight-integrations/codex"

$sourceFiles = [ordered]@{
    "settings.json" = "bca3bbaf207cb426bdd58622e41a06e79ed2c826da2ce60ebaf57a9560c27616"
    "scripts/session_start.py" = "4c1f6998169faf1f34e82c1fc5a894f6f7561f0c6a970922d590cc97d9788a7b"
    "scripts/recall.py" = "0168d80be6eca7b7a6f0fbff353e47abe60cada4ee7e46018e37dcfef55c1f08"
    "scripts/retain.py" = "94e4fb599b989b592fdee728d800abc6d34da3c997fd5f33454f6ab7a97eb530"
    "scripts/lib/__init__.py" = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    "scripts/lib/bank.py" = "816b17714e054cc98ae664ff0885a26ecf7d25315553a40bae3a6037f8b4b0e8"
    "scripts/lib/client.py" = "e2eaadf41ace7b5255ef597c70d0c5cf0b4faa9cd423dd491a3bbf2121d7c5c2"
    "scripts/lib/config.py" = "26816cba5add3effe0003fd703452c2fc52a36681024698d933f0fa2d3c997b0"
    "scripts/lib/content.py" = "0784612da72268528f4467bde2de746706d389cc44b6ae99d7adda448b18d0b6"
    "scripts/lib/daemon.py" = "ce37ecbf0eb3c58c279b23840abf12f7ee459bd38563073410b17e5b22a438c4"
    "scripts/lib/llm.py" = "0525c1ff526a7a45d41ac9e22c98185e08b4e8256745fc1f9ebcf888fcaa0f55"
    "scripts/lib/state.py" = "191fb6c1ed9d160f5456a3b1ea04e623350cc757189b461a159da003e1f3a818"
}

foreach ($required in @($bridgeSource, $configTemplatePath, $bankPolicyPath, $agentPolicyPath)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Cortex Codex integration file is missing: $required"
    }
}

$null = New-Item -ItemType Directory -Path $stagingLib -Force
$null = New-Item -ItemType Directory -Path $codexRoot -Force

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][byte[]]$Bytes)

    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return [BitConverter]::ToString($algorithm.ComputeHash($Bytes)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content
    )

    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $Content, $utf8)
}

Write-Output "Installing pinned Hindsight Codex hooks ($sourceCommit)..."
$previousProtocol = [Net.ServicePointManager]::SecurityProtocol
[Net.ServicePointManager]::SecurityProtocol = $previousProtocol -bor [Net.SecurityProtocolType]::Tls12
$webClient = New-Object Net.WebClient
try {
    foreach ($relativePath in $sourceFiles.Keys) {
        [byte[]]$bytes = $webClient.DownloadData("$sourceBase/$relativePath")
        $actualHash = Get-Sha256Hex -Bytes $bytes
        $expectedHash = $sourceFiles[$relativePath]
        if ($actualHash -ne $expectedHash) {
            throw "Hash mismatch for official Hindsight file '$relativePath'. Expected $expectedHash, received $actualHash."
        }

        $destination = Join-Path $stagingRoot ($relativePath.Replace("/", "\"))
        $destinationDirectory = Split-Path -Parent $destination
        $null = New-Item -ItemType Directory -Path $destinationDirectory -Force
        [IO.File]::WriteAllBytes($destination, $bytes)
    }
}
finally {
    $webClient.Dispose()
    [Net.ServicePointManager]::SecurityProtocol = $previousProtocol
}

# Upstream 0.3.1 treats chunked mode with N=1 as full-session mode. Cortex
# changes the two guarded branches so each Stop event processes only the
# newest user turn and creates its own document instead of reprocessing the
# complete growing transcript.
$retainPath = Join-Path $stagingScripts "retain.py"
$retainSource = [IO.File]::ReadAllText($retainPath)
$chunkCondition = 'if retain_mode == "chunked" and retain_every_n > 1:'
$conditionCount = ([regex]::Matches($retainSource, [regex]::Escape($chunkCondition))).Count
if ($conditionCount -ne 2) {
    throw "The pinned retain.py did not contain the two expected chunk-mode guards."
}
$retainSource = $retainSource.Replace($chunkCondition, 'if retain_mode == "chunked":')
$sourceNewLine = if ($retainSource.Contains("`r`n")) { "`r`n" } else { "`n" }
$documentComment = '    # In chunked mode, append timestamp to create distinct documents per chunk.'
$documentLine = '        document_id = f"{session_id}-{int(time.time() * 1000)}"'
if (([regex]::Matches($retainSource, [regex]::Escape($documentComment))).Count -ne 1 -or
    ([regex]::Matches($retainSource, [regex]::Escape($documentLine))).Count -ne 1) {
    throw "The pinned retain.py did not contain the expected chunk document-ID block."
}
$retainSource = $retainSource.Replace(
    $documentComment,
    '    # Cortex supplies a stable turn_id so queued delivery is idempotent.' + $sourceNewLine +
    '    turn_id = str(hook_input.get("turn_id") or "").strip()'
)
$retainSource = $retainSource.Replace(
    $documentLine,
    '        document_id = f"{session_id}-{turn_id}" if turn_id else f"{session_id}-{int(time.time() * 1000)}"'
)
Write-Utf8NoBom -Path $retainPath -Content $retainSource

# Memory text is untrusted data. Escape angle brackets before the official
# hook wraps results in its context delimiter so a stored fact cannot close
# that delimiter and smuggle hook-shaped instructions into the prompt.
$recallPath = Join-Path $stagingScripts "recall.py"
$recallSource = [IO.File]::ReadAllText($recallPath)
$recallLine = '    memories_formatted = format_memories(results)'
if (([regex]::Matches($recallSource, [regex]::Escape($recallLine))).Count -ne 1) {
    throw "The pinned recall.py did not contain the expected formatting line."
}
$recallNewLine = if ($recallSource.Contains("`r`n")) { "`r`n" } else { "`n" }
$recallSource = $recallSource.Replace(
    $recallLine,
    $recallLine + $recallNewLine + '    memories_formatted = memories_formatted.replace("<", "&lt;").replace(">", "&gt;")'
)
Write-Utf8NoBom -Path $recallPath -Content $recallSource

$stagingBridge = Join-Path $stagingScripts "CortexCodexHookBridge.py"
Copy-Item -LiteralPath $bridgeSource -Destination $stagingBridge -Force

$pythonCandidates = @(
    (Join-Path $env:APPDATA "uv\tools\hindsight-embed\Scripts\python.exe")
)
$uvPythonRoot = Join-Path $env:APPDATA "uv\python"
if (Test-Path -LiteralPath $uvPythonRoot) {
    $pythonCandidates += @(Get-ChildItem -LiteralPath $uvPythonRoot -Directory -Filter "cpython-3.12-windows-*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        ForEach-Object { Join-Path $_.FullName "python.exe" })
}
$pythonPath = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($pythonPath)) {
    throw "A Python 3.12 or newer runtime could not be found for the Codex memory hooks."
}
& $pythonPath -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "The selected Python runtime is older than 3.12: $pythonPath"
}

$compileTargets = @(
    $stagingBridge,
    (Join-Path $stagingScripts "session_start.py"),
    (Join-Path $stagingScripts "recall.py"),
    $retainPath
)
& $pythonPath -m py_compile @compileTargets
if ($LASTEXITCODE -ne 0) {
    throw "The installed Hindsight Codex hooks did not pass Python syntax validation."
}

# Activate only after every pinned file and local patch has validated. Clearing
# readiness first makes any partial copy fail closed: the old/new hooks will
# refuse recall and retention until a complete rerun verifies the live policy.
$null = New-Item -ItemType Directory -Path $integrationLib -Force
$null = New-Item -ItemType Directory -Path (Split-Path -Parent $policyReadyPath) -Force
if (Test-Path -LiteralPath $policyReadyPath) {
    Remove-Item -LiteralPath $policyReadyPath -Force
}
foreach ($sourceFile in Get-ChildItem -LiteralPath $stagingRoot -Recurse -File | Where-Object { $_.Extension -ne ".pyc" }) {
    $relativePath = $sourceFile.FullName.Substring($stagingRoot.Length).TrimStart("\")
    $destination = Join-Path $integrationRoot $relativePath
    $null = New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force
    Copy-Item -LiteralPath $sourceFile.FullName -Destination $destination -Force
}
Copy-Item -LiteralPath $bankPolicyPath -Destination $installedBankPolicyPath -Force

# Merge Cortex's managed values into the personal Hindsight config while
# preserving any unrelated future settings.
$mergedUserConfig = [ordered]@{}
if (Test-Path -LiteralPath $userHindsightConfig) {
    $existingUserConfig = Get-Content -Raw -LiteralPath $userHindsightConfig | ConvertFrom-Json
    foreach ($property in $existingUserConfig.PSObject.Properties) {
        $mergedUserConfig[$property.Name] = $property.Value
    }
}
$templateConfig = Get-Content -Raw -LiteralPath $configTemplatePath | ConvertFrom-Json
foreach ($property in $templateConfig.PSObject.Properties) {
    if ($property.Name -eq "hindsightApiToken" -and $null -eq $property.Value -and
        $mergedUserConfig.Contains("hindsightApiToken") -and
        -not [string]::IsNullOrWhiteSpace([string]$mergedUserConfig["hindsightApiToken"])) {
        continue
    }
    $mergedUserConfig[$property.Name] = $property.Value
}
Write-Utf8NoBom -Path $userHindsightConfig -Content ($mergedUserConfig | ConvertTo-Json -Depth 12)

function Test-CortexManagedHook {
    param($Hook)

    if ($null -eq $Hook) {
        return $false
    }
    $commandProperty = $Hook.PSObject.Properties["command"]
    $commandWindowsProperty = $Hook.PSObject.Properties["commandWindows"]
    $command = if ($null -ne $commandProperty) { [string]$commandProperty.Value } else { "" }
    $commandWindows = if ($null -ne $commandWindowsProperty) { [string]$commandWindowsProperty.Value } else { "" }
    foreach ($candidate in @($command, $commandWindows)) {
        if ($candidate -match '(?i)\.hindsight[\\/]codex[\\/]scripts[\\/](CortexCodexHookBridge|session_start|recall|retain)\.py') {
            return $true
        }
    }
    return $false
}

if (Test-Path -LiteralPath $hooksPath) {
    $hooksConfig = Get-Content -Raw -LiteralPath $hooksPath | ConvertFrom-Json
}
else {
    $hooksConfig = [pscustomobject]@{}
}
if ($null -eq $hooksConfig.PSObject.Properties["hooks"]) {
    $hooksConfig | Add-Member -NotePropertyName "hooks" -NotePropertyValue ([pscustomobject]@{})
}

# Current Codex for Windows launches every lifecycle command hook through a
# console-subsystem shell without CREATE_NO_WINDOW. Remove Cortex's legacy
# command hooks so routine memory never flashes a terminal. Hindsight remains
# available through its persistent HTTP MCP connection instead.
foreach ($eventName in @("SessionStart", "UserPromptSubmit", "Stop")) {
    $existingGroups = @()
    $eventProperty = $hooksConfig.hooks.PSObject.Properties[$eventName]
    if ($null -ne $eventProperty) {
        $existingGroups = @($eventProperty.Value)
    }
    $keptGroups = @()
    foreach ($group in $existingGroups) {
        $hooksProperty = if ($null -ne $group) { $group.PSObject.Properties["hooks"] } else { $null }
        if ($null -eq $hooksProperty) {
            $keptGroups += $group
            continue
        }

        $remainingHooks = @($hooksProperty.Value | Where-Object { -not (Test-CortexManagedHook -Hook $_) })
        if ($remainingHooks.Count -eq 0) {
            continue
        }

        $groupCopy = [ordered]@{}
        foreach ($property in $group.PSObject.Properties) {
            $groupCopy[$property.Name] = if ($property.Name -eq "hooks") { $remainingHooks } else { $property.Value }
        }
        $keptGroups += [pscustomobject]$groupCopy
    }
    $newGroups = @($keptGroups)
    if ($null -ne $eventProperty) {
        $eventProperty.Value = $newGroups
    }
    else {
        $hooksConfig.hooks | Add-Member -NotePropertyName $eventName -NotePropertyValue $newGroups
    }
}
Write-Utf8NoBom -Path $hooksPath -Content ($hooksConfig | ConvertTo-Json -Depth 12)

# Ensure the persistent Hindsight MCP connection exists. Preserve an existing
# section because users may have customized approval modes or timeouts.
$codexConfig = if (Test-Path -LiteralPath $codexConfigPath) {
    [IO.File]::ReadAllText($codexConfigPath)
}
else {
    ""
}
if ($codexConfig -notmatch '(?m)^\[mcp_servers\.hindsight\]\s*$') {
    $mcpBlock = @'
[mcp_servers.hindsight]
url = "http://127.0.0.1:8877/mcp/cortex/"
bearer_token_env_var = "HINDSIGHT_MCP_API_KEY"
enabled = true
required = false
startup_timeout_sec = 720
tool_timeout_sec = 300
default_tools_approval_mode = "writes"
disabled_tools = ["delete_bank", "clear_memories", "delete_document", "delete_directive", "delete_mental_model", "clear_mental_model"]

[mcp_servers.hindsight.tools.retain]
approval_mode = "approve"

[mcp_servers.hindsight.tools.sync_retain]
approval_mode = "approve"
'@
    $codexConfig = $codexConfig.TrimEnd() + "`r`n`r`n" + $mcpBlock.Trim() + "`r`n"
}
else {
    # Normalize the transport to the always-on service gateway while leaving
    # unrelated approval/tool customizations intact. This also repairs older
    # stdio or partially configured Hindsight sections.
    $sectionPattern = '(?ms)(^\[mcp_servers\.hindsight\]\s*\r?\n)(.*?)(?=^\[|\z)'
    $sectionMatch = [regex]::Match($codexConfig, $sectionPattern)
    if ($sectionMatch.Success) {
        $sectionBody = $sectionMatch.Groups[2].Value
        foreach ($legacyKey in @('command', 'args', 'bearer_token')) {
            $sectionBody = [regex]::Replace(
                $sectionBody,
                "(?m)^$legacyKey\s*=.*\r?\n?",
                ""
            )
        }
        $requiredValues = [ordered]@{
            url = '"http://127.0.0.1:8877/mcp/cortex/"'
            bearer_token_env_var = '"HINDSIGHT_MCP_API_KEY"'
            enabled = 'true'
            required = 'false'
            startup_timeout_sec = '720'
            tool_timeout_sec = '300'
        }
        foreach ($entry in $requiredValues.GetEnumerator()) {
            $keyPattern = "(?m)^$([regex]::Escape([string]$entry.Key))\s*=.*$"
            $replacementLine = "$($entry.Key) = $($entry.Value)"
            if ([regex]::IsMatch($sectionBody, $keyPattern)) {
                $sectionBody = [regex]::Replace($sectionBody, $keyPattern, $replacementLine)
            }
            else {
                $sectionBody = $sectionBody.TrimEnd() + "`r`n$replacementLine`r`n"
            }
        }
        $replacement = $sectionMatch.Groups[1].Value + $sectionBody
        $codexConfig = $codexConfig.Substring(0, $sectionMatch.Index) +
            $replacement +
            $codexConfig.Substring($sectionMatch.Index + $sectionMatch.Length)
    }
    # A legacy stdio server may also have a dedicated environment table. It is
    # transport state, not an approval customization, and must not survive the
    # migration to authenticated Streamable HTTP.
    $codexConfig = [regex]::Replace(
        $codexConfig,
        '(?ms)^\[mcp_servers\.hindsight\.env\]\s*\r?\n.*?(?=^\[|\z)',
        ''
    )
}
Write-Utf8NoBom -Path $codexConfigPath -Content $codexConfig

$policy = [IO.File]::ReadAllText($agentPolicyPath).Trim()
$beginMarker = "<!-- BEGIN CORTEX HINDSIGHT MEMORY -->"
$endMarker = "<!-- END CORTEX HINDSIGHT MEMORY -->"
$managedBlock = "$beginMarker`r`n$policy`r`n$endMarker"
$agentsContent = if (Test-Path -LiteralPath $agentsPath) { [IO.File]::ReadAllText($agentsPath) } else { "" }
$blockPattern = [regex]::Escape($beginMarker) + '.*?' + [regex]::Escape($endMarker)
if ([regex]::IsMatch($agentsContent, $blockPattern, [Text.RegularExpressions.RegexOptions]::Singleline)) {
    $agentsContent = [regex]::Replace(
        $agentsContent,
        $blockPattern,
        [Text.RegularExpressions.MatchEvaluator]{ param($match) $managedBlock },
        [Text.RegularExpressions.RegexOptions]::Singleline
    )
}
else {
    $prefix = $agentsContent.TrimEnd()
    $agentsContent = if ($prefix.Length -gt 0) { "$prefix`r`n`r`n$managedBlock`r`n" } else { "$managedBlock`r`n" }
}
Write-Utf8NoBom -Path $agentsPath -Content $agentsContent

$metadata = [ordered]@{
    source = "vectorize-io/hindsight"
    source_commit = $sourceCommit
    official_integration_version = "0.3.1"
    cortex_bridge_version = "1.3"
    integration_mode = "persistent-mcp"
    installed_at = (Get-Date).ToUniversalTime().ToString("o")
    python = $pythonPath
}
Write-Utf8NoBom -Path (Join-Path $integrationRoot "cortex-install.json") -Content ($metadata | ConvertTo-Json -Depth 5)

$activeApiUrl = ([string]$mergedUserConfig["hindsightApiUrl"]).TrimEnd("/")
$activeApiToken = if ($mergedUserConfig.Contains("hindsightApiToken")) { [string]$mergedUserConfig["hindsightApiToken"] } else { "" }
$apiHeaders = @{}
if (-not [string]::IsNullOrWhiteSpace($activeApiToken)) {
    $apiHeaders["Authorization"] = "Bearer $activeApiToken"
}

function Test-HindsightHealth {
    try {
        $health = Invoke-RestMethod -Method Get -Uri "$activeApiUrl/health" -Headers $apiHeaders -TimeoutSec 5
        return [string]$health.status -eq "healthy"
    }
    catch {
        return $false
    }
}

if (-not (Test-HindsightHealth) -and -not $DoNotWakeBrain) {
    $gatewayToken = [Environment]::GetEnvironmentVariable("HINDSIGHT_MCP_API_KEY", "User")
    if (-not [string]::IsNullOrWhiteSpace($gatewayToken)) {
        $null = Invoke-RestMethod `
            -Method Post `
            -Uri "http://127.0.0.1:8877/control/start" `
            -Headers @{ Authorization = "Bearer $gatewayToken" } `
            -ContentType "application/json" `
            -Body '{"manual":true}' `
            -TimeoutSec 750
    }
    $deadline = (Get-Date).AddSeconds(420)
    while ((Get-Date) -lt $deadline -and -not (Test-HindsightHealth)) {
        Start-Sleep -Seconds 2
    }
}

if (Test-HindsightHealth) {
    $bankPolicy = [IO.File]::ReadAllText($bankPolicyPath)
    $null = Invoke-RestMethod `
        -Method Patch `
        -Uri "$activeApiUrl/v1/default/banks/cortex/config" `
        -Headers $apiHeaders `
        -ContentType "application/json" `
        -Body $bankPolicy `
        -TimeoutSec 30
    $expectedPolicy = $bankPolicy | ConvertFrom-Json
    $liveResponse = Invoke-RestMethod `
        -Method Get `
        -Uri "$activeApiUrl/v1/default/banks/cortex/config" `
        -Headers $apiHeaders `
        -TimeoutSec 30
    $livePolicy = $liveResponse.config
    if (-not [bool]$livePolicy.memory_defense.enabled) {
        throw "Cortex bank policy verification failed: Memory Defense is disabled."
    }
    foreach ($expectedRule in @($expectedPolicy.updates.memory_defense.rules)) {
        $matchingRules = @($livePolicy.memory_defense.rules | Where-Object {
            [string]$_.on -eq [string]$expectedRule.on -and [string]$_.action -eq [string]$expectedRule.action
        })
        if ($matchingRules.Count -eq 0) {
            throw "Cortex bank policy verification failed: missing $($expectedRule.on):$($expectedRule.action)."
        }
    }
    foreach ($propertyName in @(
        "retain_extraction_mode",
        "retain_mission",
        "reflect_mission",
        "recall_include_chunks",
        "recall_max_tokens"
    )) {
        $expectedValue = $expectedPolicy.updates.PSObject.Properties[$propertyName].Value
        $liveValue = $livePolicy.PSObject.Properties[$propertyName].Value
        if (($expectedValue | ConvertTo-Json -Compress) -ne ($liveValue | ConvertTo-Json -Compress)) {
            throw "Cortex bank policy verification failed for '$propertyName'."
        }
    }
    $policyHash = Get-Sha256Hex -Bytes ([IO.File]::ReadAllBytes($installedBankPolicyPath))
    Write-Utf8NoBom -Path $policyReadyPath -Content (([ordered]@{
        schema_version = 1
        bank = "cortex"
        api_url = $activeApiUrl
        policy_sha256 = $policyHash
        applied_at = (Get-Date).ToUniversalTime().ToString("o")
    }) | ConvertTo-Json)
    Write-Output "Cortex bank privacy and selective-retention policy applied."
}
elseif ($DoNotWakeBrain) {
    Write-Warning "Cortex is asleep, so the bank policy was not refreshed. Run this installer once while Cortex is ready."
}
else {
    throw "Cortex did not become healthy, so its bank-level memory policy could not be applied."
}

$resolvedStagingRoot = [IO.Path]::GetFullPath($stagingRoot)
$resolvedTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd("\") + "\"
if (-not $resolvedStagingRoot.StartsWith($resolvedTempRoot, [StringComparison]::OrdinalIgnoreCase) -or
    -not ([IO.Path]::GetFileName($resolvedStagingRoot)).StartsWith("CortexCodexInstall-", [StringComparison]::Ordinal)) {
    throw "Refusing to remove an unexpected installer staging path: $resolvedStagingRoot"
}
Remove-Item -LiteralPath $resolvedStagingRoot -Recurse -Force

Write-Output "Codex windowless MCP memory is installed for the shared 'cortex' bank."
Write-Output "Restart Codex once so new tasks load the persistent Hindsight tools and memory policy."

# Cortex Brain

Cortex is a local shared-memory service for Codex/ChatGPT Desktop, Claude Desktop, and other MCP clients. Hindsight 0.8.4 stores the memories, PostgreSQL stores the bank, and Ollama runs `gpt-oss:20b` for extraction and reflection.

> **Current baseline and Corthex protocol policy:** This repository currently ships the Windows Cortex baseline documented below. The public Corthex server is under active development; its MCP implementation is required to use the official stateless `2026-07-28` architecture and exact Python SDK `mcp==2.0.0` pin. The accepted compatibility contract and executable regression fixture are in [`docs/adr/0001-mcp-2026-architecture.md`](docs/adr/0001-mcp-2026-architecture.md) and [`tests/fixtures/mcp-2026-07-28-contract.json`](tests/fixtures/mcp-2026-07-28-contract.json). This policy does not claim that the existing legacy gateway already conforms.

## How it works

```text
Codex / ChatGPT Desktop ─┐
Claude Desktop ──────────┼─> Cortex MCP gateway :8877 ─> Hindsight API :8888 ─> cortex bank
Other local MCP clients ─┘                                  │
                                                           ├─> embedded PostgreSQL :5432
                                                           └─> Ollama / gpt-oss:20b
```

`CortexBrainGateway` is a delayed-auto-start Windows service. Its small gateway remains available on `127.0.0.1:8877` even while the heavy brain is asleep. The service watches for ChatGPT, Codex, Claude, Cursor, Windsurf, OpenCode, and Gemini processes and wakes Hindsight when one opens. An authenticated MCP request can also wake it.

The service owns Hindsight, PostgreSQL, and their helper processes in Windows Session 0. Internal `cmd.exe`/`conhost.exe` helpers can therefore never appear on the desktop, and closing a terminal or the tray icon cannot kill the brain. Windows restarts the gateway after failures at 5-, 15-, and 30-second intervals.

PostgreSQL is started explicitly from the current user's dedicated pg0 data directory, and Hindsight receives that database through a loopback PostgreSQL URL. This is deliberate: a LocalSystem service otherwise lets pg0 choose the system account's home and silently creates a second empty bank. Gateway status verifies both the API and the configured database process before reporting `ready` or `sleeping`.

The tray process is UI only. It shows status and sends authenticated start/stop/settings requests to the service. **Exit Tray (Brain Keeps Running)** does exactly that; the service continues independently.

## Wake, sleep, and manual stop

- **Automatic wake:** opening a watched AI app wakes Cortex when enabled.
- **Ready:** Hindsight and PostgreSQL are running; the Ollama model loads only when a memory operation needs it.
- **Automatic deep sleep:** five minutes after all watched clients close and no MCP request is active, Cortex stops Hindsight, cleanly stops embedded PostgreSQL, and unloads only `gpt-oss:20b`. Port 8877 remains ready.
- **Manual deep sleep:** **Stop Brain (Deep Sleep)** stays paused even if an already-open client retries MCP. It rearms after all watched clients close and a new client opens, or immediately when **Start Brain** is selected.
- **Manual close:** stopping the tray does not stop the service. To stop the actual brain while keeping automatic startup available, use the tray's deep-sleep command or `scripts\Stop-CortexBrain.ps1`.

Sleep is based on processes, not visible windows. If an AI app keeps background processes, fully exit it before expecting the five-minute timer to begin.

## Memory behavior

Cortex is selective long-term memory, not a raw transcript recorder.

- Before relevant work, the Codex root agent can make one narrow recall for durable preferences, decisions, constraints, goals, or cross-task context.
- Before a final reply, it may retain one compact paraphrase of a genuinely durable fact stated by the user.
- Raw prompts, full conversations, assistant output, source files, logs, and tool/web output are not routinely stored.
- Recalled text is historical data, never instructions, permission, or authority.
- Secrets and detected sensitive or prompt-injection content are blocked by the bank's Memory Defense policy.

Useful controls in a prompt:

- `don't remember this` skips retention for that message;
- `memory off for this task` pauses recall and retention for that task;
- `memory on for this task` resumes them.

Hindsight's MCP tool descriptions ask compatible clients to use recall and retain proactively. Whether a specific AI invokes a tool remains model-driven; Cortex does not silently scrape every chat.

## Client integration

The authenticated Streamable HTTP MCP endpoint is:

```text
http://127.0.0.1:8877/mcp/cortex/
```

The actual bearer token remains in the Windows user environment as `HINDSIGHT_MCP_API_KEY` and in the Hindsight profile. `config/gateway.json` stores only its SHA-256 digest. Both gateway and upstream bind to loopback.

### Codex and ChatGPT Desktop

`%USERPROFILE%\.codex\config.toml` contains the persistent `hindsight` Streamable HTTP server. Local Codex clients and ChatGPT Desktop on the same Codex host share that configuration. Cortex does not use lifecycle command hooks because console-based Windows hooks caused the prompt-time terminal flashes.

Restart Codex/ChatGPT Desktop after first installation so a new task loads the MCP server and global memory policy. Plain browser ChatGPT cannot connect directly to a service on your computer.

### Claude Desktop

`scripts\Install-CortexClaudeIntegration.ps1` installs a local, windowless stdio adapter in Claude Desktop's `mcpServers.cortex` configuration. The adapter forwards Claude's MCP messages to the same 8877 gateway and reads the token from the Windows user environment; it does not place the token in Claude's JSON file.

Restart Claude Desktop after installation. Anthropic's cloud-side custom connectors cannot reach `localhost`; the local adapter is intentional.

## Viewing memories

Hindsight's profile Control Center only shows profiles/daemons, so seeing only `cortex` there is expected. To inspect memories, double-click the Cortex tray icon or choose **Open Memory Browser**.

The official Hindsight Memory Browser opens the `cortex` bank at `http://localhost:9999`. It launches Node directly with a hidden window instead of using a persistent `.cmd` wrapper. Browse **Memories** and then **World Facts**, **Experience**, **Observations**, or **Mental Models**. Use **Close Memory Browser** when finished.

## Resource modes measured on this PC

- **Gateway only:** the Windows service/gateway measured about 75 MB working set; the hidden tray measured about 41 MB.
- **Ready/warm:** the Session-0 Hindsight/PostgreSQL stack measured about 1.2 GB working set before counting Ollama.
- **Model loaded:** Ollama reports a 14 GB `gpt-oss:20b` model split about 56% CPU / 44% GPU. Previous tests reached roughly 6.1 GB VRAM and 7.8 GB additional system RAM.
- **Cold wake:** recent service-owned wake measured about 86 seconds; the slowest first initialization observed was roughly 7.5 minutes, so the configured safety timeout is 720 seconds.

Automatic observations and consolidation remain disabled to keep resource use predictable. Cortex does not self-modify code. A future improvement loop should use Hindsight mental models/reflection, scheduled evaluations, versioned changes, and explicit human approval rather than unsupervised self-editing.

## Install or repair

The setup is packaged in one idempotent installer. It preserves the existing `cortex` bank, registers the hidden service and tray task, generates the gateway configuration, and installs both MCP client integrations:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-CortexBrain.ps1
```

Restart Codex/ChatGPT Desktop and Claude Desktop once after an install or repair so they load the refreshed MCP configuration.

## Commands

Because PowerShell execution policy is Restricted on this PC, use `-ExecutionPolicy Bypass`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-CortexBrain.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Stop-CortexBrain.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-CortexMemoryBrowser.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Stop-CortexMemoryBrowser.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Test-CortexGateway.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Test-CortexCodexIntegration.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Backup-CortexBrain.ps1 -Type Bank
```

Service status:

```powershell
Get-Service CortexBrainGateway
Invoke-RestMethod http://127.0.0.1:8877/health
```

## Backups and removal

`Backup-CortexBrain.ps1` writes timestamped, unencrypted archives into `backups\`. Keep them private or move them to encrypted storage. The post-migration backup is `backups\cortex-bank-20260722-013747.zip` (44 documents / 37 facts). The accidental one-document service-account bank was also preserved as `backups\system-service-bank-20260722-012810.dump` before it was shut down.

The main uninstaller removes the service and integrations but preserves memory data unless `-RemoveMemoryData` is explicitly supplied:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Uninstall-CortexBrain.ps1
```

`-RemoveMemoryData` is irreversible; make and verify a backup first.

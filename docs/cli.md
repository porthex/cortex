# Cortex CLI

The `cortex` command is a dependency-free Python 3.10+ client for a local or remote Cortex Brain. It calls only the public `/v1` Cortex gateway and never imports or exposes Hindsight.

## Install

From a checkout:

```sh
python -m pip install .
# or, for an isolated application install
pipx install .
```

Verify the console entry point:

```sh
cortex --help
```

## Configure and authenticate

```sh
cortex configure --url https://brain.example.ts.net --bank my-bank
export CORTEX_TOKEN='replace-with-client-token'  # PowerShell: $env:CORTEX_TOKEN='...'
cortex connect
cortex doctor
```

For one-shot use without putting a token in command history:

```sh
printf '%s\n' "$CORTEX_TOKEN" | cortex connect --token-stdin
```

`configure` stores URL, default bank, and timeout only. Tokens are never written to the config. Set `CORTEX_CONFIG` to override the platform path:

- Windows: `%APPDATA%\Cortex\config.json`
- macOS: `~/Library/Application Support/Cortex/config.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/cortex/config.json`

Non-loopback URLs must use HTTPS. Tailscale/private routing is still required by the Remote Brain deployment; bearer authentication does not replace the private perimeter.

## Commands

```text
cortex configure --url URL --bank BANK [--timeout SECONDS]
cortex connect [--token-stdin]
cortex status
cortex doctor
cortex retain TEXT [--bank BANK]
cortex recall QUERY [--bank BANK] [--limit N]
cortex reflect QUERY [--bank BANK]
cortex banks
cortex start
cortex stop
```

`start` and `stop` call the configured gateway's public operator endpoints. Cortex server installation and `serve` lifecycle belong to the platform deployment package; this client does not guess at systemd, launchd, or Windows service commands.

Use `--json` before the subcommand for automation:

```sh
cortex --json recall "deployment decision" --limit 5
```

Success envelope:

```json
{"command":"status","data":{"state":"ready"},"error":null,"ok":true}
```

Error envelope:

```json
{"command":"status","data":null,"error":{"code":"authentication_failed","message":"Authentication failed","retryable":false},"ok":false}
```

Exit codes:

| Code | Meaning |
|---:|---|
| 0 | success |
| 1 | unexpected CLI failure |
| 2 | usage or invalid configuration |
| 3 | missing/invalid credentials or authorization |
| 4 | requested resource not found |
| 5 | request rejected by Cortex |
| 6 | timeout, disconnect, or unreachable gateway |
| 7 | malformed gateway response |

## Public gateway contract

The CLI uses these stable routes:

- `GET /v1/status`
- `GET /v1/banks`
- `POST /v1/memories/retain`
- `POST /v1/memories/recall`
- `POST /v1/memories/reflect`
- `POST /v1/control/start`
- `POST /v1/control/stop`

Every memory request carries an explicit `bank`. Every request carries `Authorization: Bearer …`. Gateways may return direct JSON or `{ "ok": true, "data": ... }` envelopes.

## Reproducible verification

```sh
uv run --locked --extra test pytest -q
python -m compileall -q src tests
uv build
```

The tests start an isolated loopback gateway and an isolated in-memory bank. They do not read or mutate any production memory bank.

## Rollback

```sh
python -m pip uninstall cortex
```

Remove the user config only if it is no longer needed. This does not remove any server or bank data.

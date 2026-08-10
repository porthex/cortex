# Corthex CLI

The `corthex` command is a dependency-free Python 3.10+ client for a local or remote Corthex Brain. It calls only the public `/v1` Corthex gateway and never imports or exposes Hindsight.

## Install

From a checkout:

```sh
python -m pip install .
# or, for an isolated application install
pipx install .
```

Verify the console entry point:

```sh
corthex --help
```

## Configure and authenticate

```sh
corthex configure --url https://brain.example.ts.net --bank my-bank
export CORTHEX_TOKEN='replace-with-client-token'  # PowerShell: $env:CORTHEX_TOKEN='...'
corthex connect
corthex doctor
```

For one-shot use without putting a token in command history:

```sh
printf '%s\n' "$CORTHEX_TOKEN" | corthex connect --token-stdin
```

`configure` stores URL, default bank, and timeout only. Tokens are never written to the config. Set `CORTHEX_CONFIG` to override the platform path:

- Windows: `%APPDATA%\Corthex\config.json`
- macOS: `~/Library/Application Support/Corthex/config.json`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/corthex/config.json`

Non-loopback URLs must use HTTPS. Tailscale/private routing is still required by the Remote Brain deployment; bearer authentication does not replace the private perimeter.

## Commands

```text
corthex configure --url URL --bank BANK [--timeout SECONDS]
corthex connect [--token-stdin]
corthex status
corthex doctor
corthex retain TEXT [--bank BANK]
corthex recall QUERY [--bank BANK] [--limit N]
corthex reflect QUERY [--bank BANK]
corthex banks
corthex start
corthex stop
```

`start` and `stop` call the configured gateway's public operator endpoints. Corthex server installation and `serve` lifecycle belong to the platform deployment package; this client does not guess at systemd, launchd, or Windows service commands.

Use `--json` before the subcommand for automation:

```sh
corthex --json recall "deployment decision" --limit 5
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
| 5 | request rejected by Corthex |
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
PYTHONPATH=src python -m unittest discover -s tests -v
python -m compileall -q src tests
uv build
```

The tests start an isolated loopback gateway and an isolated in-memory bank. They do not read or mutate any production memory bank.

## Rollback

```sh
python -m pip uninstall corthex
```

Remove the user config only if it is no longer needed. This does not remove any server or bank data.

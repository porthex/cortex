from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from typing import Mapping, Sequence, TextIO

from .client import Client, CortexError
from .config import Config, config_path, load_config, save_config


class UsageError(ValueError):
    pass


class CliArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = CliArgumentParser(prog="cortex", description="Cortex Remote Brain client")
    parser.add_argument("--json", action="store_true", dest="json_output")
    subcommands = parser.add_subparsers(dest="command", required=True)
    configure = subcommands.add_parser("configure", help="save a connection profile")
    configure.add_argument("--url", required=True)
    configure.add_argument("--bank", required=True)
    configure.add_argument("--timeout", type=float, default=30.0)
    connect = subcommands.add_parser("connect", help="verify an authenticated connection")
    connect.add_argument("--token-stdin", action="store_true")
    subcommands.add_parser("doctor", help="diagnose configuration and connectivity")
    subcommands.add_parser("status", help="show Remote Brain status")
    subcommands.add_parser("banks", help="list accessible banks")
    retain = subcommands.add_parser("retain", help="retain a durable memory")
    retain.add_argument("text")
    retain.add_argument("--bank")
    recall = subcommands.add_parser("recall", help="recall relevant memories")
    recall.add_argument("query")
    recall.add_argument("--bank")
    recall.add_argument("--limit", type=int, default=10)
    reflect = subcommands.add_parser("reflect", help="reflect over a bank")
    reflect.add_argument("query")
    reflect.add_argument("--bank")
    subcommands.add_parser("start", help="start the configured Remote Brain")
    subcommands.add_parser("stop", help="stop the configured Remote Brain")
    return parser


def _emit(payload: object, stdout: TextIO) -> None:
    stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def _success(command: str, data: object, json_output: bool, stdout: TextIO) -> None:
    if json_output:
        _emit({"ok": True, "command": command, "data": data, "error": None}, stdout)
    elif isinstance(data, dict):
        stdout.write("\n".join(f"{key}: {value}" for key, value in data.items()) + "\n")
    else:
        stdout.write(f"{data}\n")


def _selected_bank(config: Config, override: str | None) -> str:
    if override is None:
        return config.bank
    if not override.strip():
        raise ValueError("--bank must be non-empty")
    return override


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if environ is None else environ
    input_stream = sys.stdin if stdin is None else stdin
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    try:
        args = _parser().parse_args(raw_args)
        if args.command == "configure":
            config = Config(url=args.url.rstrip("/"), bank=args.bank, timeout=args.timeout)
            path = config_path(env)
            save_config(config, path)
            data = {**asdict(config), "path": str(path)}
            if args.json_output:
                _success("configure", data, True, out)
            else:
                out.write(f"Configured Cortex bank {config.bank} at {config.url}\n")
            return 0
        config = load_config(config_path(env))
        token = env.get("CORTEX_TOKEN", "")
        if args.command == "connect" and args.token_stdin:
            token = input_stream.readline().rstrip("\r\n")
        client = Client(config, token)
        if args.command == "connect":
            _success("connect", client.status(), args.json_output, out)
            return 0
        if args.command == "doctor":
            status = client.status()
            data = {
                "checks": {
                    "configuration": "ok",
                    "credentials": "ok",
                    "reachability": "ok",
                    "transport": "ok",
                },
                "status": status,
            }
            _success("doctor", data, args.json_output, out)
            return 0
        if args.command == "status":
            _success("status", client.status(), args.json_output, out)
            return 0
        if args.command == "banks":
            data = client.banks()
        elif args.command == "retain":
            data = client.retain(args.text, _selected_bank(config, args.bank))
        elif args.command == "recall":
            if args.limit < 1:
                raise ValueError("--limit must be positive")
            data = client.recall(args.query, _selected_bank(config, args.bank), args.limit)
        elif args.command == "reflect":
            data = client.reflect(args.query, _selected_bank(config, args.bank))
        elif args.command in {"start", "stop"}:
            data = client.control(args.command)
        else:
            return 1
        _success(args.command, data, args.json_output, out)
        return 0
    except UsageError as exc:
        command = next((item for item in raw_args if not item.startswith("-")), None)
        if "--json" in raw_args:
            _emit(
                {
                    "ok": False,
                    "command": command,
                    "data": None,
                    "error": {"code": "usage_error", "message": str(exc), "retryable": False},
                },
                out,
            )
        else:
            err.write(f"error: {exc}\n")
        return 2
    except CortexError as exc:
        if "args" in locals() and getattr(args, "json_output", False):
            _emit(
                {
                    "ok": False,
                    "command": getattr(args, "command", None),
                    "data": None,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "retryable": exc.retryable,
                    },
                },
                out,
            )
        else:
            err.write(f"error: {exc.message}\n")
        return exc.exit_code
    except (OSError, ValueError) as exc:
        if "args" in locals() and getattr(args, "json_output", False):
            _emit(
                {
                    "ok": False,
                    "command": getattr(args, "command", None),
                    "data": None,
                    "error": {"code": "invalid_configuration", "message": str(exc)},
                },
                out,
            )
        else:
            err.write(f"error: {exc}\n")
        return 2
    return 1


def entrypoint() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    entrypoint()

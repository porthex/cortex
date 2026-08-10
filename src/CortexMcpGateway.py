#!/usr/bin/env python3
"""Cortex localhost MCP gateway and Hindsight lifecycle manager.

This process is intended to run beneath the Cortex Windows service.  It keeps a
small HTTP listener available while the comparatively heavy Hindsight/Ollama
stack sleeps, wakes Hindsight when an MCP client needs it, and transparently
streams MCP HTTP/SSE traffic to the local Hindsight API.

The gateway never stores the bearer token itself.  Its JSON configuration holds
only the lowercase SHA-256 digest of that token; the original Authorization
header is forwarded to Hindsight after constant-time validation.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import hashlib
import hmac
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import socket
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
STARTF_USESHOWWINDOW = 0x00000001
SW_HIDE = 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _as_float(value: Any, default: float, minimum: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    return max(minimum, result)


def _expand_path(value: Any, base_dir: Path | None = None) -> str:
    if value is None:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(str(value).strip()))
    if expanded and base_dir is not None and not os.path.isabs(expanded):
        expanded = str(base_dir / expanded)
    return os.path.abspath(expanded) if expanded else ""


def _normalise_command(value: Any, base_dir: Path) -> list[str]:
    """Return a shell-free command array from a JSON list.

    String commands are deliberately rejected: accepting one would either need
    fragile quoting or a shell, and a shell is precisely what can create the
    unwanted terminal window this component is designed to avoid.
    """

    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise ValueError("Configured commands must be non-empty JSON arrays")
    command = [os.path.expandvars(os.path.expanduser(str(part))) for part in value]
    if command and command[0] and not os.path.isabs(command[0]):
        candidate = base_dir / command[0]
        if candidate.exists():
            command[0] = str(candidate.resolve())
    return command


def _connection_named_headers(headers: Mapping[str, str]) -> set[str]:
    named: set[str] = set()
    value = headers.get("Connection", "")
    for item in value.split(","):
        item = item.strip().casefold()
        if item:
            named.add(item)
    return named


def _forward_headers(headers: Mapping[str, str], *, request_side: bool) -> dict[str, str]:
    blocked = HOP_BY_HOP_HEADERS | _connection_named_headers(headers)
    if request_side:
        blocked.add("host")
        # aiohttp will select the correct framing for the streamed request body.
        blocked.add("content-length")
    result: dict[str, str] = {}
    for name, value in headers.items():
        if name.casefold() not in blocked:
            result[name] = value
    return result


def _is_loopback(remote: str | None) -> bool:
    if not remote:
        return True
    # aiohttp normally gives a bare address, but tolerate IPv4 host:port here.
    candidate = remote.strip("[]")
    if candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.rsplit(":", 1)[0]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return candidate.casefold() == "localhost"


class GatewayConfig:
    """Validated gateway settings loaded from one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.base_dir = self.path.parent
        with self.path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("Gateway configuration must be a JSON object")
        self.raw: dict[str, Any] = raw

        self.listen_host = str(raw.get("listen_host", "127.0.0.1")).strip()
        self.listen_port = int(raw.get("listen_port", 8877))
        try:
            if not ipaddress.ip_address(self.listen_host).is_loopback:
                raise ValueError("listen_host must be a numeric loopback address")
        except ValueError as exc:
            raise ValueError("listen_host must be 127.0.0.1 or ::1") from exc
        if not (1 <= self.listen_port <= 65535):
            raise ValueError("listen_port must be between 1 and 65535")

        self.upstream_url = str(raw.get("upstream_url", "http://127.0.0.1:8888")).rstrip("/")
        upstream = urlsplit(self.upstream_url)
        if upstream.scheme not in {"http", "https"} or not upstream.hostname:
            raise ValueError("upstream_url must be an absolute HTTP(S) URL")
        if not ipaddress.ip_address(upstream.hostname).is_loopback:
            raise ValueError("upstream_url must target a numeric loopback address")
        self.upstream_host = upstream.hostname
        self.upstream_port = upstream.port or (443 if upstream.scheme == "https" else 80)
        self.upstream_health_url = str(
            raw.get("upstream_health_url", f"{self.upstream_url}/health")
        )

        digest = str(raw.get("auth_token_sha256", "")).strip().casefold()
        self.allow_unauthenticated = _as_bool(raw.get("allow_unauthenticated"), False)
        if digest:
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("auth_token_sha256 must be a 64-character hexadecimal digest")
        elif not self.allow_unauthenticated:
            raise ValueError(
                "auth_token_sha256 is required unless allow_unauthenticated is explicitly true"
            )
        self.auth_token_sha256 = digest

        self.profile = str(raw.get("profile", "cortex"))
        self.model = str(raw.get("model", "gpt-oss:20b"))
        self.hindsight_exe = _expand_path(raw.get("hindsight_exe"), self.base_dir)
        self.ollama_exe = _expand_path(raw.get("ollama_exe"), self.base_dir)

        self.start_command = _normalise_command(raw.get("start_command"), self.base_dir)
        self.stop_command = _normalise_command(raw.get("stop_command"), self.base_dir)
        self.postgres_start_command = _normalise_command(
            raw.get("postgres_start_command"), self.base_dir
        )
        self.ollama_stop_command = _normalise_command(
            raw.get("ollama_stop_command"), self.base_dir
        )
        self.postgres_stop_command = _normalise_command(
            raw.get("postgres_stop_command"), self.base_dir
        )
        self.postgres_data_dir = _expand_path(raw.get("postgres_data_dir"), self.base_dir)
        self.postgres_pg_ctl_exe = _expand_path(
            raw.get("postgres_pg_ctl_exe"), self.base_dir
        )
        self.postgres_host = str(raw.get("postgres_host", "127.0.0.1")).strip()
        self.postgres_port = int(raw.get("postgres_port", 5432))
        try:
            if not ipaddress.ip_address(self.postgres_host).is_loopback:
                raise ValueError("postgres_host must be a numeric loopback address")
        except ValueError as exc:
            raise ValueError("postgres_host must be 127.0.0.1 or ::1") from exc
        if not (1 <= self.postgres_port <= 65535):
            raise ValueError("postgres_port must be between 1 and 65535")
        if not self.start_command:
            if not self.hindsight_exe:
                raise ValueError("hindsight_exe or start_command is required")
            self.start_command = [
                self.hindsight_exe,
                "-p",
                self.profile,
                "daemon",
                "start",
            ]
        if not self.stop_command:
            if not self.hindsight_exe:
                raise ValueError("hindsight_exe or stop_command is required")
            self.stop_command = [
                self.hindsight_exe,
                "-p",
                self.profile,
                "daemon",
                "stop",
            ]
        if not self.ollama_stop_command and self.ollama_exe and self.model:
            self.ollama_stop_command = [self.ollama_exe, "stop", self.model]

        default_log_dir = self.base_dir.parent / "runtime"
        self.log_path = _expand_path(raw.get("log_path", default_log_dir / "gateway.log"))
        self.startup_log_path = _expand_path(
            raw.get("startup_log_path", default_log_dir / "gateway-child.log")
        )
        self.state_path = _expand_path(raw.get("state_path", default_log_dir / "gateway-state.json"))

        self.user_profile = _expand_path(raw.get("user_profile"))
        self.appdata = _expand_path(raw.get("appdata"))
        self.localappdata = _expand_path(raw.get("localappdata"))
        configured_path = raw.get("path_prepend", [])
        if not isinstance(configured_path, list):
            raise ValueError("path_prepend must be a JSON array")
        self.path_prepend = [
            _expand_path(item, self.base_dir)
            for item in configured_path
            if str(item).strip()
        ]
        configured_environment = raw.get("command_environment", {})
        if not isinstance(configured_environment, dict):
            raise ValueError("command_environment must be a JSON object")
        self.command_environment = {
            str(key): os.path.expandvars(str(value))
            for key, value in configured_environment.items()
            if value is not None
        }

        names = raw.get(
            "process_names",
            ["ChatGPT", "codex", "Claude", "Cursor", "Windsurf", "OpenCode", "gemini"],
        )
        if not isinstance(names, list):
            raise ValueError("process_names must be a JSON array")
        self.process_names = sorted(
            {str(name).strip() for name in names if str(name).strip()}, key=str.casefold
        )
        self.process_name_keys = {
            Path(name).stem.casefold() for name in self.process_names if name.strip()
        }

        self.poll_seconds = _as_float(raw.get("poll_seconds"), 2.0, 0.5)
        self.deep_sleep_delay_seconds = _as_float(
            raw.get("deep_sleep_delay_seconds"), 300.0, 0.0
        )
        self.health_timeout_seconds = _as_float(
            raw.get("health_timeout_seconds"), 720.0, 1.0
        )
        self.health_poll_seconds = _as_float(raw.get("health_poll_seconds"), 0.75, 0.1)
        self.health_request_timeout_seconds = _as_float(
            raw.get("health_request_timeout_seconds"), 3.0, 0.2
        )
        self.busy_wait_seconds = _as_float(raw.get("busy_wait_seconds"), 120.0, 0.0)
        self.start_retry_delay_seconds = _as_float(
            raw.get("start_retry_delay_seconds"), 60.0, 0.0
        )
        self.stop_timeout_seconds = _as_float(raw.get("stop_timeout_seconds"), 60.0, 1.0)
        self.postgres_timeout_seconds = _as_float(
            raw.get("postgres_timeout_seconds"), 120.0, 5.0
        )

        self.monitor_processes = _as_bool(raw.get("monitor_processes"), True)
        self.default_auto_wake_enabled = _as_bool(raw.get("auto_wake_enabled"), True)
        self.idle_stop_enabled = _as_bool(raw.get("idle_stop_enabled"), True)
        self.stop_upstream_on_exit = _as_bool(raw.get("stop_upstream_on_exit"), True)
        self.shutdown_event_name = str(raw.get("shutdown_event_name", "")).strip()

    def child_environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        if self.user_profile:
            environment["USERPROFILE"] = self.user_profile
            environment["HOME"] = self.user_profile
            drive, tail = os.path.splitdrive(self.user_profile)
            if drive:
                environment["HOMEDRIVE"] = drive
                environment["HOMEPATH"] = tail or "\\"
        if self.appdata:
            environment["APPDATA"] = self.appdata
        if self.localappdata:
            environment["LOCALAPPDATA"] = self.localappdata
        if self.path_prepend:
            existing_path = environment.get("PATH", "")
            environment["PATH"] = os.pathsep.join(
                [*self.path_prepend, existing_path] if existing_path else self.path_prepend
            )
        environment.setdefault("UV_PYTHON", "3.12")
        environment["HINDSIGHT_EMBED_DAEMON_STARTUP_TIMEOUT"] = str(
            max(1, round(self.health_timeout_seconds))
        )
        environment.update(self.command_environment)
        return environment


class CortexGateway:
    def __init__(self, config: GatewayConfig, shutdown_event: asyncio.Event) -> None:
        self.config = config
        self.shutdown_event = shutdown_event
        self.session: ClientSession | None = None
        self.lifecycle_lock = asyncio.Lock()
        self.lifecycle_state = "starting"
        self.last_error: str | None = None
        self.last_start_failure_monotonic: float | None = None
        self.last_transition_utc = _utc_now()
        self.startup_process: subprocess.Popen[bytes] | None = None
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self.monitor_task: asyncio.Task[Any] | None = None
        self.active_requests = 0
        self.clients: list[dict[str, Any]] = []
        self.no_clients_since: float | None = None
        self.manual_paused = False
        self.rearm_seen_no_clients = False
        self.auto_wake_enabled = config.default_auto_wake_enabled
        self._load_state()

    def _set_lifecycle(self, state: str, error: str | None = None) -> None:
        if state != self.lifecycle_state or error != self.last_error:
            self.lifecycle_state = state
            self.last_error = error
            self.last_transition_utc = _utc_now()
            if error:
                logging.warning("Lifecycle changed to %s: %s", state, error)
            else:
                logging.info("Lifecycle changed to %s", state)

    def _load_state(self) -> None:
        path = Path(self.config.state_path)
        try:
            with path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
            if isinstance(state, dict):
                self.manual_paused = _as_bool(state.get("manual_paused"), False)
                self.rearm_seen_no_clients = _as_bool(
                    state.get("rearm_seen_no_clients"), False
                )
                self.auto_wake_enabled = _as_bool(
                    state.get("auto_wake_enabled"), self.config.default_auto_wake_enabled
                )
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logging.warning("Could not read gateway state: %s", exc)

    def _save_state(self) -> None:
        path = Path(self.config.state_path)
        payload = {
            "manual_paused": self.manual_paused,
            "rearm_seen_no_clients": self.rearm_seen_no_clients,
            "auto_wake_enabled": self.auto_wake_enabled,
            "updated_utc": _utc_now(),
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, path)
        except OSError as exc:
            logging.warning("Could not persist gateway state: %s", exc)

    async def start(self) -> None:
        timeout = ClientTimeout(total=None, connect=10.0, sock_connect=10.0, sock_read=None)
        self.session = ClientSession(timeout=timeout, auto_decompress=False)
        healthy = await self.check_health()
        if healthy:
            self._set_lifecycle("ready")
        elif await self.is_upstream_listening():
            self._set_lifecycle("busy")
        else:
            self._set_lifecycle("sleeping")
        if self.config.monitor_processes:
            self.monitor_task = asyncio.create_task(
                self._process_monitor(), name="cortex-process-monitor"
            )

    async def close(self) -> None:
        if self.monitor_task:
            self.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor_task
        # Cancel monitor-triggered wake operations before taking the lifecycle
        # lock for shutdown.  A first launch can legitimately spend minutes in
        # health polling, and waiting behind it would defeat graceful service
        # stop.
        tasks = list(self.background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.config.stop_upstream_on_exit:
            with contextlib.suppress(Exception):
                await self.stop_upstream(manual=False)
        if self.session:
            await self.session.close()
            self.session = None

    def _track_task(self, coroutine: Any, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine, name=name)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    def authenticate(self, request: web.Request) -> bool:
        if self.config.allow_unauthenticated and not self.config.auth_token_sha256:
            return True
        scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
        if not separator or scheme.casefold() != "bearer" or not token:
            return False
        supplied = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return hmac.compare_digest(supplied, self.config.auth_token_sha256)

    async def check_health(self) -> bool:
        if not self.session:
            return False
        try:
            timeout = ClientTimeout(total=self.config.health_request_timeout_seconds)
            async with self.session.get(
                self.config.upstream_health_url, timeout=timeout
            ) as response:
                return response.status == 200
        except (ClientError, asyncio.TimeoutError, OSError):
            return False

    async def is_upstream_listening(self) -> bool:
        def probe() -> bool:
            try:
                with socket.create_connection(
                    (self.config.upstream_host, self.config.upstream_port), timeout=0.5
                ):
                    return True
            except OSError:
                return False

        return await asyncio.to_thread(probe)

    async def is_postgres_listening(self) -> bool:
        if not self.config.postgres_start_command and not self.config.postgres_stop_command:
            return False

        def probe() -> bool:
            try:
                with socket.create_connection(
                    (self.config.postgres_host, self.config.postgres_port), timeout=0.5
                ):
                    return True
            except OSError:
                return False

        return await asyncio.to_thread(probe)

    def _postgres_data_pid(self) -> int | None:
        if not self.config.postgres_data_dir:
            return None
        pid_path = Path(self.config.postgres_data_dir) / "postmaster.pid"
        try:
            first_line = pid_path.read_text(encoding="ascii", errors="strict").splitlines()[0]
            pid = int(first_line.strip())
            return pid if pid > 0 else None
        except (FileNotFoundError, OSError, ValueError, IndexError, UnicodeError):
            return None

    @staticmethod
    def _is_postgres_process(pid: int | None) -> bool:
        if not pid:
            return False
        names = {process_id: executable for process_id, executable in _windows_processes()}
        return Path(names.get(pid, "")).stem.casefold() in {"postgres", "postmaster"}

    def _managed_postgres_pid(self) -> int | None:
        data_pid = self._postgres_data_pid()
        listener_pid = _windows_tcp_listener_pid(self.config.postgres_port)
        if data_pid and listener_pid == data_pid and self._is_postgres_process(data_pid):
            return data_pid
        return None

    async def _wait_for_postgres(self, *, running: bool, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            listening = await self.is_postgres_listening()
            managed_pid = await asyncio.to_thread(self._managed_postgres_pid)
            if running and listening and managed_pid is not None:
                return True
            if not running and not listening:
                return True
            await asyncio.sleep(0.25)
        listening = await self.is_postgres_listening()
        managed_pid = await asyncio.to_thread(self._managed_postgres_pid)
        return (listening and managed_pid is not None) if running else not listening

    async def ensure_postgres(self) -> bool:
        if not self.config.postgres_start_command:
            return True

        listening = await self.is_postgres_listening()
        if listening:
            managed_pid = await asyncio.to_thread(self._managed_postgres_pid)
            if managed_pid is not None:
                return True
            listener_pid = await asyncio.to_thread(
                _windows_tcp_listener_pid, self.config.postgres_port
            )
            self._set_lifecycle(
                "error",
                f"PostgreSQL port {self.config.postgres_port} is occupied by an unmanaged process"
                + (f" (PID {listener_pid})" if listener_pid else ""),
            )
            return False

        logging.info("Starting managed PostgreSQL from %s", self.config.postgres_data_dir)
        return_code = await self._run_stop_command(
            self.config.postgres_start_command,
            "Embedded PostgreSQL start",
            timeout_seconds=self.config.postgres_timeout_seconds,
        )
        if return_code != 0:
            self._set_lifecycle(
                "error", f"Embedded PostgreSQL start exited with code {return_code}"
            )
            return False
        if await self._wait_for_postgres(
            running=True, timeout_seconds=self.config.postgres_timeout_seconds
        ):
            return True
        self._set_lifecycle(
            "error",
            f"Managed PostgreSQL did not become ready on port {self.config.postgres_port}",
        )
        return False

    async def _wait_until_healthy(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while not self.shutdown_event.is_set() and time.monotonic() < deadline:
            if await self.check_health():
                return True
            await asyncio.sleep(self.config.health_poll_seconds)
        return await self.check_health()

    def _spawn_hidden(self, command: Sequence[str]) -> subprocess.Popen[bytes]:
        if not command:
            raise ValueError("Cannot run an empty command")
        executable = command[0]
        if os.path.isabs(executable) and not Path(executable).is_file():
            raise FileNotFoundError(f"Executable not found: {executable}")

        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = SW_HIDE
            creationflags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

        log_path = Path(self.config.startup_log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        output = log_path.open("ab")
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=self.config.child_environment(),
                cwd=self.config.user_profile or None,
                shell=False,
                startupinfo=startupinfo,
                creationflags=creationflags,
                close_fds=True,
            )
        finally:
            output.close()
        return process

    async def _reap_process(self, process: subprocess.Popen[bytes], label: str) -> int:
        return_code = await asyncio.to_thread(process.wait)
        if return_code != 0:
            logging.warning("%s exited with code %s", label, return_code)
        else:
            logging.info("%s exited normally", label)
        return return_code

    async def ensure_upstream(self, *, trigger: str, force: bool = False) -> bool:
        async with self.lifecycle_lock:
            if await self.check_health():
                self._set_lifecycle("ready")
                return True

            # A live listening port can be an API occupied by local model work.
            # Never invoke daemon start in that case: Hindsight's supervisor may
            # otherwise replace the busy process and interrupt a retain/recall.
            if await self.is_upstream_listening():
                self._set_lifecycle("busy")
                wait_for = min(self.config.busy_wait_seconds, self.config.health_timeout_seconds)
                if wait_for > 0 and await self._wait_until_healthy(wait_for):
                    self._set_lifecycle("ready")
                # Listening is sufficient for proxying. The MCP request itself
                # can wait for the occupied API once the health probe gives up.
                return True

            if (
                not force
                and self.last_start_failure_monotonic is not None
                and time.monotonic() - self.last_start_failure_monotonic
                < self.config.start_retry_delay_seconds
            ):
                return False

            self._set_lifecycle("waking")
            logging.info("Starting Hindsight for trigger %s", trigger)
            try:
                if not await self.ensure_postgres():
                    return False
                self.startup_process = self._spawn_hidden(self.config.start_command)
                self._track_task(
                    self._reap_process(self.startup_process, "Hindsight starter"),
                    "hindsight-starter-reaper",
                )
                if await self._wait_until_healthy(self.config.health_timeout_seconds):
                    self.last_start_failure_monotonic = None
                    self._set_lifecycle("ready")
                    return True
                if await self.is_upstream_listening():
                    # Preserve a slow-but-live API instead of running another
                    # supervisor. Proxying may still succeed when it becomes free.
                    self._set_lifecycle("busy")
                    return True
                raise TimeoutError("Hindsight did not become healthy before the timeout")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_start_failure_monotonic = time.monotonic()
                self._set_lifecycle("error", str(exc))
                return False

    async def _run_stop_command(
        self,
        command: Sequence[str],
        label: str,
        *,
        timeout_seconds: float | None = None,
    ) -> int | None:
        if not command:
            return 0
        timeout = timeout_seconds or self.config.stop_timeout_seconds
        try:
            process = self._spawn_hidden(command)
            try:
                return_code = await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=timeout)
                if return_code != 0:
                    logging.warning("%s exited with code %s", label, return_code)
                return return_code
            except asyncio.TimeoutError:
                logging.warning("%s exceeded the stop timeout", label)
                with contextlib.suppress(OSError):
                    process.terminate()
                with contextlib.suppress(asyncio.TimeoutError, OSError):
                    await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5.0)
                return None
        except (OSError, ValueError) as exc:
            logging.warning("%s could not run: %s", label, exc)
            return None

    async def _stop_managed_postgres(self, captured_pid: int | None) -> bool:
        if not self.config.postgres_stop_command:
            return not await self.is_postgres_listening()

        if not await self.is_postgres_listening():
            return True

        listener_pid = await asyncio.to_thread(
            _windows_tcp_listener_pid, self.config.postgres_port
        )
        if captured_pid is None:
            captured_pid = await asyncio.to_thread(self._managed_postgres_pid)
        if captured_pid is None or listener_pid != captured_pid:
            logging.error(
                "Refusing to stop PostgreSQL: port %s is not owned by the configured data directory",
                self.config.postgres_port,
            )
            return False

        await self._run_stop_command(
            self.config.postgres_stop_command,
            "Embedded PostgreSQL stop",
            timeout_seconds=self.config.postgres_timeout_seconds,
        )
        if await self._wait_for_postgres(
            running=False, timeout_seconds=min(self.config.postgres_timeout_seconds, 20.0)
        ):
            return True

        # pg_ctl stop normally uses postmaster.pid. If another component removed
        # that file during shutdown, signal the already-validated server PID
        # directly. SIGINT is PostgreSQL's clean fast-shutdown signal on Windows.
        if self.config.postgres_pg_ctl_exe and self._is_postgres_process(captured_pid):
            await self._run_stop_command(
                [self.config.postgres_pg_ctl_exe, "kill", "INT", str(captured_pid)],
                "Embedded PostgreSQL fast-shutdown signal",
                timeout_seconds=15.0,
            )
            if await self._wait_for_postgres(
                running=False, timeout_seconds=min(self.config.postgres_timeout_seconds, 30.0)
            ):
                return True
        return False

    async def stop_upstream(self, *, manual: bool) -> None:
        async with self.lifecycle_lock:
            if not manual and self.active_requests > 0:
                logging.info("Skipping automatic sleep while MCP requests are active")
                return
            if manual:
                self.clients = await asyncio.to_thread(self._enumerate_clients)
                self.manual_paused = True
                self.rearm_seen_no_clients = not self.clients
                self._save_state()
            self._set_lifecycle("stopping")
            postgres_pid = await asyncio.to_thread(self._managed_postgres_pid)
            await self._run_stop_command(self.config.stop_command, "Hindsight stop")
            postgres_stopped = await self._stop_managed_postgres(postgres_pid)
            await self._run_stop_command(self.config.ollama_stop_command, "Ollama model unload")

            deadline = time.monotonic() + min(self.config.stop_timeout_seconds, 15.0)
            while time.monotonic() < deadline and await self.is_upstream_listening():
                await asyncio.sleep(0.25)
            upstream_listening = await self.is_upstream_listening()
            postgres_listening = await self.is_postgres_listening()
            if upstream_listening or postgres_listening or not postgres_stopped:
                remaining = []
                if upstream_listening:
                    remaining.append("Hindsight")
                if postgres_listening or not postgres_stopped:
                    remaining.append("PostgreSQL")
                self._set_lifecycle(
                    "error", f"Deep sleep incomplete; {' and '.join(remaining)} still running"
                )
            else:
                self._set_lifecycle("sleeping")
            self.no_clients_since = None

    async def set_auto_wake(self, enabled: bool) -> None:
        self.auto_wake_enabled = enabled
        self._save_state()

    async def explicit_start(self, trigger: str) -> bool:
        if self.manual_paused:
            if trigger == "control":
                self.manual_paused = False
                self.rearm_seen_no_clients = False
                self._save_state()
            elif trigger == "mcp-request":
                self.clients = await asyncio.to_thread(self._enumerate_clients)
                if (
                    self.auto_wake_enabled
                    and self.rearm_seen_no_clients
                    and self.clients
                ):
                    self.manual_paused = False
                    self.rearm_seen_no_clients = False
                    self._save_state()
                else:
                    return False
            else:
                return False
        return await self.ensure_upstream(trigger=trigger, force=True)

    def _enumerate_clients(self) -> list[dict[str, Any]]:
        processes = _windows_processes()
        clients: list[dict[str, Any]] = []
        for pid, executable in processes:
            if Path(executable).stem.casefold() in self.config.process_name_keys:
                clients.append({"pid": pid, "name": Path(executable).stem})
        clients.sort(key=lambda item: (str(item["name"]).casefold(), int(item["pid"])))
        return clients

    async def _process_monitor(self) -> None:
        wake_task: asyncio.Task[Any] | None = None
        while not self.shutdown_event.is_set():
            try:
                self.clients = await asyncio.to_thread(self._enumerate_clients)
                now = time.monotonic()
                if self.manual_paused:
                    if not self.clients and not self.rearm_seen_no_clients:
                        self.rearm_seen_no_clients = True
                        self._save_state()
                    elif (
                        self.clients
                        and self.rearm_seen_no_clients
                        and self.auto_wake_enabled
                    ):
                        self.manual_paused = False
                        self.rearm_seen_no_clients = False
                        self._save_state()
                        if not await self.is_upstream_listening():
                            wake_task = self._track_task(
                                self.ensure_upstream(
                                    trigger="process-monitor-rearm", force=True
                                ),
                                "process-monitor-rearm-wake",
                            )
                    self.no_clients_since = None
                elif self.clients:
                    self.no_clients_since = None
                    if (
                        self.auto_wake_enabled
                        and not self.manual_paused
                        and not await self.is_upstream_listening()
                        and (wake_task is None or wake_task.done())
                    ):
                        wake_task = self._track_task(
                            self.ensure_upstream(trigger="process-monitor", force=False),
                            "process-monitor-wake",
                        )
                else:
                    if self.no_clients_since is None:
                        self.no_clients_since = now
                    idle_for = now - self.no_clients_since
                    if (
                        self.config.idle_stop_enabled
                        and self.active_requests == 0
                        and idle_for >= self.config.deep_sleep_delay_seconds
                        and await self.is_upstream_listening()
                    ):
                        await self.stop_upstream(manual=False)
                        self.no_clients_since = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.warning("Process monitor iteration failed: %s", exc)
            await asyncio.sleep(self.config.poll_seconds)

    async def status_payload(self) -> dict[str, Any]:
        # Keep the always-on gateway health endpoint fast while the heavy API
        # sleeps. A failed HTTP health probe can consume its full timeout, while
        # a local TCP-listener check returns immediately when port 8888 is down.
        listening = await self.is_upstream_listening()
        healthy = await self.check_health() if listening else False
        postgres_listening = await self.is_postgres_listening()
        postgres_managed = False
        if postgres_listening:
            postgres_managed = (
                await asyncio.to_thread(self._managed_postgres_pid) is not None
            )
        if self.lifecycle_state not in {"waking", "stopping"}:
            if healthy:
                self._set_lifecycle("ready")
            elif listening:
                self._set_lifecycle("busy")
            elif postgres_listening:
                self._set_lifecycle(
                    "error", "PostgreSQL is still running while Hindsight is stopped"
                )
            elif self.lifecycle_state != "error":
                self._set_lifecycle("sleeping")
        if self.config.monitor_processes:
            self.clients = await asyncio.to_thread(self._enumerate_clients)
        return {
            "gateway_ready": True,
            "upstream_healthy": healthy,
            "upstream_listening": listening,
            "postgres_listening": postgres_listening,
            "postgres_managed": postgres_managed,
            "manual_paused": self.manual_paused,
            "rearm_seen_no_clients": self.rearm_seen_no_clients,
            "auto_wake_enabled": self.auto_wake_enabled,
            "clients": self.clients,
            "lifecycle_state": self.lifecycle_state,
            "active_requests": self.active_requests,
            "last_transition_utc": self.last_transition_utc,
            "last_error": self.last_error,
        }

    async def handle_health(self, request: web.Request) -> web.Response:
        if not _is_loopback(request.remote):
            raise web.HTTPForbidden(text="Loopback access only")
        payload = await self.status_payload()
        # The public health response intentionally contains no configuration,
        # file paths, command lines, token data, or detailed client identities.
        return web.json_response(
            {
                "gateway_ready": payload["gateway_ready"],
                "upstream_healthy": payload["upstream_healthy"],
                "upstream_listening": payload["upstream_listening"],
                "postgres_listening": payload["postgres_listening"],
                "lifecycle_state": payload["lifecycle_state"],
            }
        )

    async def handle_status(self, request: web.Request) -> web.Response:
        return web.json_response(await self.status_payload())

    async def handle_start(self, request: web.Request) -> web.Response:
        ready = await self.explicit_start("control")
        payload = await self.status_payload()
        return web.json_response(payload, status=200 if ready else 503)

    async def handle_stop(self, request: web.Request) -> web.Response:
        await self.stop_upstream(manual=True)
        return web.json_response(await self.status_payload())

    async def handle_auto_wake(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text='Expected JSON object: {"enabled": true|false}')
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            raise web.HTTPBadRequest(text='Expected JSON object: {"enabled": true|false}')
        await self.set_auto_wake(body["enabled"])
        return web.json_response(await self.status_payload())

    async def handle_proxy(self, request: web.Request) -> web.StreamResponse:
        if not self.session:
            raise web.HTTPServiceUnavailable(text="Gateway is still starting")

        # Count the request while waking as well as while proxying so the idle
        # monitor can never put Hindsight to sleep underneath an MCP call.
        self.active_requests += 1
        try:
            ready = await self.explicit_start("mcp-request")
        except Exception:
            self.active_requests = max(0, self.active_requests - 1)
            raise
        if not ready:
            self.active_requests = max(0, self.active_requests - 1)
            raise web.HTTPServiceUnavailable(
                text="Cortex is manually paused or could not wake Hindsight"
            )

        upstream = f"{self.config.upstream_url}{request.rel_url}"
        request_headers = _forward_headers(request.headers, request_side=True)
        data: Any = None
        if request.can_read_body:
            data = request.content.iter_chunked(64 * 1024)

        response: web.StreamResponse | None = None
        try:
            async with self.session.request(
                request.method,
                upstream,
                headers=request_headers,
                data=data,
                allow_redirects=False,
            ) as upstream_response:
                response_headers = _forward_headers(
                    upstream_response.headers, request_side=False
                )
                response = web.StreamResponse(
                    status=upstream_response.status,
                    reason=upstream_response.reason,
                    headers=response_headers,
                )
                await response.prepare(request)
                async for chunk in upstream_response.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except asyncio.CancelledError:
            raise
        except (ClientError, asyncio.TimeoutError, OSError) as exc:
            logging.warning("MCP upstream request failed: %s", type(exc).__name__)
            if response is not None and response.prepared:
                response.force_close()
                return response
            raise web.HTTPBadGateway(text="Hindsight MCP upstream request failed") from exc
        finally:
            self.active_requests = max(0, self.active_requests - 1)


@web.middleware
async def authentication_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    gateway: CortexGateway = request.app["gateway"]
    if request.path == "/health":
        return await handler(request)
    if not gateway.authenticate(request):
        raise web.HTTPUnauthorized(
            text="A valid Cortex bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await handler(request)


def create_application(gateway: CortexGateway) -> web.Application:
    application = web.Application(
        middlewares=[authentication_middleware], client_max_size=64 * 1024 * 1024
    )
    application["gateway"] = gateway
    application.router.add_get("/health", gateway.handle_health)
    application.router.add_get("/status", gateway.handle_status)
    application.router.add_post("/control/start", gateway.handle_start)
    application.router.add_post("/control/stop", gateway.handle_stop)
    application.router.add_post("/control/auto-wake", gateway.handle_auto_wake)
    application.router.add_route("*", "/mcp/{tail:.*}", gateway.handle_proxy)
    return application


def _windows_tcp_listener_pid(port: int) -> int | None:
    """Return the owning PID for an IPv4 TCP listener without spawning a shell."""

    if os.name != "nt" or not (1 <= port <= 65535):
        return None

    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    get_extended_tcp_table = iphlpapi.GetExtendedTcpTable
    get_extended_tcp_table.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.ULONG),
        wintypes.BOOL,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
    ]
    get_extended_tcp_table.restype = wintypes.DWORD

    size = wintypes.ULONG(0)
    family_ipv4 = 2  # AF_INET
    owner_pid_listeners = 3  # TCP_TABLE_OWNER_PID_LISTENER
    insufficient_buffer = 122
    result = get_extended_tcp_table(
        None,
        ctypes.byref(size),
        False,
        family_ipv4,
        owner_pid_listeners,
        0,
    )
    if result not in {0, insufficient_buffer} or size.value < ctypes.sizeof(wintypes.DWORD):
        return None

    buffer = ctypes.create_string_buffer(size.value)
    result = get_extended_tcp_table(
        buffer,
        ctypes.byref(size),
        False,
        family_ipv4,
        owner_pid_listeners,
        0,
    )
    if result != 0:
        return None

    count = wintypes.DWORD.from_buffer_copy(buffer.raw[: ctypes.sizeof(wintypes.DWORD)]).value
    row_size = ctypes.sizeof(MIB_TCPROW_OWNER_PID)
    offset = ctypes.sizeof(wintypes.DWORD)
    for index in range(count):
        row_offset = offset + index * row_size
        if row_offset + row_size > size.value:
            break
        row = MIB_TCPROW_OWNER_PID.from_buffer_copy(buffer, row_offset)
        local_port = socket.ntohs(int(row.dwLocalPort) & 0xFFFF)
        if local_port == port:
            return int(row.dwOwningPid)
    return None


def _windows_processes() -> list[tuple[int, str]]:
    """Enumerate Windows processes without spawning tasklist or requiring psutil."""

    if os.name != "nt":
        return []

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    results: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not process_first(snapshot, ctypes.byref(entry)):
            return []
        while True:
            results.append((int(entry.th32ProcessID), str(entry.szExeFile)))
            if not process_next(snapshot, ctypes.byref(entry)):
                break
    finally:
        close_handle(snapshot)
    return results


async def _named_event_monitor(name: str, shutdown_event: asyncio.Event) -> None:
    """Set the asyncio event when the Windows service signals its named event."""

    if os.name != "nt" or not name:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_event = kernel32.OpenEventW
    open_event.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    open_event.restype = wintypes.HANDLE
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait_for_single_object.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = None
    try:
        while not shutdown_event.is_set() and not handle:
            handle = open_event(0x00100000, False, name)  # SYNCHRONIZE
            if not handle:
                await asyncio.sleep(0.5)
        while not shutdown_event.is_set() and handle:
            result = wait_for_single_object(handle, 0)
            if result == 0:  # WAIT_OBJECT_0
                logging.info("Windows service requested graceful shutdown")
                shutdown_event.set()
                return
            if result == 0xFFFFFFFF:  # WAIT_FAILED
                logging.warning("Waiting on the Windows shutdown event failed")
                return
            await asyncio.sleep(0.25)
    finally:
        if handle:
            close_handle(handle)


def _configure_logging(path: str) -> None:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            RotatingFileHandler(
                log_path,
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
        ],
        force=True,
    )


async def run(config: GatewayConfig, shutdown_event_name: str) -> None:
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_shutdown(*_: Any) -> None:
        loop.call_soon_threadsafe(shutdown_event.set)

    for signal_name in ("SIGINT", "SIGTERM"):
        candidate = getattr(signal, signal_name, None)
        if candidate is None:
            continue
        try:
            loop.add_signal_handler(candidate, request_shutdown)
        except (NotImplementedError, RuntimeError):
            with contextlib.suppress(ValueError):
                signal.signal(candidate, request_shutdown)

    gateway = CortexGateway(config, shutdown_event)
    await gateway.start()
    application = create_application(gateway)
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.listen_host, config.listen_port)
    await site.start()
    logging.info(
        "Cortex gateway listening on %s:%s; upstream %s",
        config.listen_host,
        config.listen_port,
        config.upstream_url,
    )

    event_monitor: asyncio.Task[Any] | None = None
    if shutdown_event_name:
        event_monitor = asyncio.create_task(
            _named_event_monitor(shutdown_event_name, shutdown_event),
            name="windows-service-shutdown-event",
        )
    try:
        await shutdown_event.wait()
    finally:
        logging.info("Cortex gateway is shutting down")
        if event_monitor:
            event_monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await event_monitor
        await runner.cleanup()
        await gateway.close()
        logging.info("Cortex gateway stopped")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cortex localhost MCP gateway")
    parser.add_argument("--config", required=True, help="Path to gateway JSON configuration")
    parser.add_argument(
        "--shutdown-event",
        default="",
        help="Optional named Windows event signalled by the service on stop",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate configuration and exit without starting the gateway",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        config = GatewayConfig(Path(arguments.config))
        _configure_logging(config.log_path)
        if arguments.check_config:
            return 0
        shutdown_event_name = arguments.shutdown_event or config.shutdown_event_name
        asyncio.run(run(config, shutdown_event_name))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # pythonw has no stderr.  Logging may not yet be configured if JSON or a
        # path is invalid, so use the Windows event log's debugger channel as a
        # last resort without ever printing configuration contents or secrets.
        with contextlib.suppress(Exception):
            logging.exception("Cortex gateway failed: %s", exc)
        if sys.stderr is not None:
            print(f"Cortex gateway failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

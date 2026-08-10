#!/usr/bin/env python3
"""Windows bridge around Hindsight's official Codex hooks.

The bridge keeps Hindsight's official recall and extraction logic while adding
Cortex lifecycle behavior:

* wake the existing tray controller on session start;
* honor per-message and per-task opt-out phrases;
* snapshot each user turn so delayed retains cannot read a newer transcript;
* drain retained turns through one FIFO worker after deep sleep; and
* discard requests that Memory Defense rejects instead of retrying forever.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid


INSTALL_ROOT = Path.home() / ".hindsight" / "codex"
SCRIPT_ROOT = INSTALL_ROOT / "scripts"
STATE_ROOT = INSTALL_ROOT / "state"
TURN_ROOT = STATE_ROOT / "turn-capture"
DISABLED_ROOT = STATE_ROOT / "memory-disabled"
CANCEL_ROOT = STATE_ROOT / "memory-cancelled"
PENDING_ROOT = STATE_ROOT / "pending-retain"
LOG_PATH = STATE_ROOT / "cortex-hook-bridge.log"
POLICY_READY_PATH = STATE_ROOT / "policy-ready.json"
INSTALLED_POLICY_PATH = INSTALL_ROOT / "cortex-bank-policy.json"
USER_CONFIG_PATH = Path.home() / ".hindsight" / "codex.json"
WORKER_MUTEX_NAME = "Local\\Cortex.Hindsight.CodexRetainWorker"
MAX_PROMPT_BYTES = 128 * 1024
TURN_TTL_SECONDS = 2 * 60 * 60
MAX_TURN_RECORDS = 128
MAX_TURN_BYTES = 8 * 1024 * 1024
MAX_PENDING_ITEMS = 64
MAX_PENDING_BYTES = 8 * 1024 * 1024
ORPHAN_GRACE_SECONDS = 60

OFF_PHRASES = (
    "memory off for this task",
    "disable memory for this task",
    "don't use memory for this task",
    "do not use memory for this task",
)
ON_PHRASES = (
    "memory on for this task",
    "enable memory for this task",
    "resume memory for this task",
)
SKIP_PHRASES = (
    "don't remember this",
    "do not remember this",
    "memory off for this message",
    "skip memory for this message",
)
OFF_PATTERNS = (
    r"\b(?:stop|pause|disable)\s+(?:using\s+)?memory\b.*\b(?:this|current)\s+(?:task|chat|session)\b",
    r"\bstop\s+remembering\b.*\b(?:this|current)\s+(?:task|chat|session)\b",
)
ON_PATTERNS = (
    r"\b(?:resume|enable|turn\s+on)\s+(?:using\s+)?memory\b.*\b(?:this|current)\s+(?:task|chat|session)\b",
)
SKIP_PATTERNS = (
    r"\b(?:do\s+not|don['’]t|please\s+do\s+not|please\s+don['’]t)\s+remember\b",
    r"\bforget\s+(?:this|that|what\s+i\s+(?:said|told\s+you))\b",
)


def _log(message: str) -> None:
    try:
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 256_000:
            LOG_PATH.replace(LOG_PATH.with_suffix(".log.old"))
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with LOG_PATH.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _load_json(path: Path, default):
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (OSError, ValueError, TypeError):
        return default


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _write_json_atomic(path: Path, value) -> None:
    _write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2))


def _safe_key(*parts: str) -> str:
    value = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:40]


def _session_id(payload: dict) -> str:
    return str(payload.get("session_id") or "unknown")


def _turn_id(payload: dict) -> str:
    return str(payload.get("turn_id") or "current")


def _turn_record_path(payload: dict) -> Path:
    return TURN_ROOT / f"{_safe_key(_session_id(payload), _turn_id(payload))}.json"


def _disabled_path(payload: dict) -> Path:
    return DISABLED_ROOT / f"{_safe_key(_session_id(payload))}.json"


def _cancel_path(payload_or_session) -> Path:
    session_id = (
        _session_id(payload_or_session)
        if isinstance(payload_or_session, dict)
        else str(payload_or_session or "unknown")
    )
    return CANCEL_ROOT / f"{_safe_key(session_id)}.json"


def _cancel_epoch(payload_or_session) -> int:
    record = _load_json(_cancel_path(payload_or_session), {})
    try:
        return max(0, int(record.get("cancel_epoch", 0)))
    except (AttributeError, TypeError, ValueError):
        return 0


def _cleanup_stale_state() -> None:
    now = time.time()
    # Disabled/cancellation markers are deliberately not age-pruned: a Codex
    # task remains memory-off until that same task receives explicit ON.
    for directory in (DISABLED_ROOT, CANCEL_ROOT):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # Atomic writers normally remove their own temporary file. Sweep remnants
    # left by a process crash once no concurrent publisher could still need it.
    try:
        STATE_ROOT.mkdir(parents=True, exist_ok=True)
        for path in STATE_ROOT.rglob("*.tmp"):
            try:
                if path.stat().st_mtime < now - ORPHAN_GRACE_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass

    try:
        _cleanup_turn_records()
        _, _, wake_timeout = _api_settings()
        _cleanup_pending(wake_timeout)
    except Exception as exc:
        _log(f"Could not clean pending retention state: {exc}")


def _prompt_text(payload: dict) -> str:
    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    return prompt if isinstance(prompt, str) else str(prompt)


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in ("text", "input_text"):
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def _latest_user_prompt(payload: dict) -> str:
    direct = _prompt_text(payload)
    if direct:
        return direct

    transcript_path = payload.get("transcript_path")
    if not transcript_path:
        return ""
    try:
        sys.path.insert(0, str(SCRIPT_ROOT))
        from lib.content import read_transcript

        messages = read_transcript(str(transcript_path), include_tool_calls=False)
        for message in reversed(messages):
            if message.get("role") == "user":
                return _block_text(message.get("content", ""))
    except Exception as exc:  # Hooks must degrade gracefully.
        _log(f"Could not inspect latest user prompt: {exc}")
    return ""


def _control_decision(payload: dict, prompt: str) -> tuple[bool, bool]:
    """Return (session_disabled, skip_this_turn)."""
    marker = _disabled_path(payload)
    normalized = " ".join(prompt.casefold().split())

    if any(phrase in normalized for phrase in ON_PHRASES) or any(
        re.search(pattern, normalized) for pattern in ON_PATTERNS
    ):
        marker.unlink(missing_ok=True)
        return False, True
    if any(phrase in normalized for phrase in OFF_PHRASES) or any(
        re.search(pattern, normalized) for pattern in OFF_PATTERNS
    ):
        cancel_epoch = time.time_ns()
        _write_json_atomic(
            _cancel_path(payload),
            {
                "session_id": _session_id(payload),
                "cancel_epoch": cancel_epoch,
                "cancelled_at": time.time(),
            },
        )
        _write_json_atomic(
            marker,
            {"session_id": _session_id(payload), "disabled_at": time.time()},
        )
        _purge_session_pending(_session_id(payload))
        return True, True
    if any(phrase in normalized for phrase in SKIP_PHRASES) or any(
        re.search(pattern, normalized) for pattern in SKIP_PATTERNS
    ):
        return marker.exists(), True

    disabled = marker.exists()
    return disabled, disabled


def _record_turn(payload: dict, prompt: str, retain_allowed: bool) -> None:
    created_at = time.time()
    if len(prompt.encode("utf-8", errors="replace")) > MAX_PROMPT_BYTES:
        retain_allowed = False
    record = {
        "session_id": _session_id(payload),
        "turn_id": _turn_id(payload),
        "retain_allowed": bool(retain_allowed),
        "prompt": prompt if retain_allowed else None,
        "control_epoch": _cancel_epoch(payload),
        "created_at": created_at,
        "expires_at": created_at + TURN_TTL_SECONDS,
    }
    _write_json_atomic(_turn_record_path(payload), record)
    _cleanup_turn_records()
    if not _spawn_worker():
        _log("Could not start turn-state expiry worker")


def _take_turn_record(payload: dict) -> dict | None:
    path = _turn_record_path(payload)
    record = _load_json(path, None)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return record if isinstance(record, dict) else None


def _command_creation_flags(detached: bool = False) -> int:
    if os.name != "nt":
        return 0
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if detached:
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return flags


def _wake_controller() -> None:
    config = _load_json(USER_CONFIG_PATH, {})
    task_name = config.get("cortexControllerTaskName", "Cortex Brain Controller")
    try:
        subprocess.Popen(
            ["schtasks.exe", "/Run", "/TN", str(task_name)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_command_creation_flags(detached=True),
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        _log(f"Could not wake Cortex controller: {exc}")


def _delegate_recall(payload: bytes) -> int:
    script_path = SCRIPT_ROOT / "recall.py"
    if not script_path.is_file():
        _log(f"Official recall hook is missing: {script_path}")
        return 0
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=18,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"Official recall hook failed to run: {exc}")
        return 0

    if result.stdout:
        sys.stdout.buffer.write(result.stdout)
        sys.stdout.buffer.flush()
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
        sys.stderr.buffer.flush()
    return result.returncode


def _queue_retain(payload: dict, prompt: str, control_epoch: int | None = None) -> None:
    prompt_bytes = prompt.encode("utf-8", errors="replace")
    if len(prompt_bytes) > MAX_PROMPT_BYTES:
        _log(f"Skipped oversized automatic retain ({len(prompt_bytes)} bytes)")
        return

    PENDING_ROOT.mkdir(parents=True, exist_ok=True)
    turn_key = _safe_key(_session_id(payload), _turn_id(payload))
    created_at = time.time()
    _, _, wake_timeout = _api_settings()
    queue_id = f"{int(created_at * 1000):013d}-{turn_key}"
    snapshot_path = PENDING_ROOT / f"{queue_id}.jsonl"
    queue_path = PENDING_ROOT / f"{queue_id}.queue.json"

    # A flat JSONL message is a supported input to Hindsight's transcript
    # reader. It freezes the exact turn and contains no assistant/tool output.
    snapshot_line = json.dumps({"role": "user", "content": prompt}, ensure_ascii=False)
    _write_text_atomic(snapshot_path, snapshot_line + "\n")

    # Stop payloads may contain last_assistant_message and other large fields.
    # Build the official hook input from a strict allowlist so queued state is
    # user-only in both content and metadata.
    hook_payload = {
        "session_id": _session_id(payload),
        "turn_id": _turn_id(payload),
        "transcript_path": str(snapshot_path),
    }
    if isinstance(payload.get("cwd"), str) and payload.get("cwd"):
        hook_payload["cwd"] = payload["cwd"]
    queue_record = {
        "payload": hook_payload,
        "snapshot_path": str(snapshot_path),
        "created_at": created_at,
        "expires_at": created_at + wake_timeout,
        "control_epoch": int(control_epoch if control_epoch is not None else _cancel_epoch(payload)),
        "attempts": 0,
        "next_attempt_at": 0,
    }
    # Publish the queue record last, after its immutable snapshot is durable.
    try:
        _write_json_atomic(queue_path, queue_record)
    except Exception:
        _discard_queue(queue_path, snapshot_path)
        raise
    _cleanup_pending(wake_timeout)
    if queue_path.is_file() and not _spawn_worker():
        _log(f"Could not start retain worker; discarded plaintext queue item: {queue_path.name}")
        _discard_queue(queue_path, snapshot_path)


class _WorkerLock:
    def __init__(self):
        self.handle = None
        self.fd = None
        self.path = STATE_ROOT / "retain-worker.lock"

    def acquire(self) -> bool:
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_bool
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexW(None, True, WORKER_MUTEX_NAME)
            if not handle:
                return False
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            self.handle = handle
            return True

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(self.fd, str(os.getpid()).encode("ascii"))
            return True
        except FileExistsError:
            return False

    def release(self) -> None:
        if os.name == "nt" and self.handle:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.ReleaseMutex.argtypes = (ctypes.c_void_p,)
            kernel32.ReleaseMutex.restype = ctypes.c_bool
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            kernel32.CloseHandle.restype = ctypes.c_bool
            kernel32.ReleaseMutex(self.handle)
            kernel32.CloseHandle(self.handle)
            self.handle = None
        elif self.fd is not None:
            try:
                os.close(self.fd)
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            self.fd = None


def _spawn_worker() -> bool:
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=_command_creation_flags(detached=True),
            start_new_session=(os.name != "nt"),
        )
        return True
    except OSError as exc:
        _log(f"Could not launch retain queue worker: {exc}")
        return False


def _api_settings() -> tuple[str, str | None, int]:
    config = _load_json(USER_CONFIG_PATH, {})
    url = str(config.get("hindsightApiUrl") or "http://127.0.0.1:8888").rstrip("/")
    token = config.get("hindsightApiToken") or None
    try:
        timeout = max(5, min(900, int(config.get("retainWakeTimeout", 420))))
    except (TypeError, ValueError):
        timeout = 420
    return url, token, timeout


def _configured_bank_id() -> str:
    config = _load_json(USER_CONFIG_PATH, {})
    return str(config.get("bankId") or "cortex")


def _local_policy_ready() -> bool:
    marker = _load_json(POLICY_READY_PATH, None)
    if not isinstance(marker, dict) or not INSTALLED_POLICY_PATH.is_file():
        return False
    try:
        policy_hash = hashlib.sha256(INSTALLED_POLICY_PATH.read_bytes()).hexdigest()
    except OSError:
        return False
    api_url, _, _ = _api_settings()
    return (
        marker.get("schema_version") == 1
        and str(marker.get("policy_sha256") or "").casefold() == policy_hash.casefold()
        and str(marker.get("bank") or "") == _configured_bank_id()
        and str(marker.get("api_url") or "").rstrip("/") == api_url
    )


def _live_policy_ready(url: str, token: str | None) -> bool:
    if not _local_policy_ready():
        return False
    expected = _load_json(INSTALLED_POLICY_PATH, {}).get("updates", {})
    if not isinstance(expected, dict):
        return False

    bank_id = _configured_bank_id()
    headers = {"User-Agent": "cortex-codex-hook-bridge/1.2"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    encoded_bank = urllib.parse.quote(bank_id, safe="")
    try:
        request = urllib.request.Request(
            f"{url}/v1/default/banks/{encoded_bank}/config",
            headers=headers,
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
        live = body.get("config", {})
    except Exception:
        return False
    if not isinstance(live, dict):
        return False

    for key in (
        "retain_extraction_mode",
        "retain_mission",
        "reflect_mission",
        "recall_include_chunks",
        "recall_max_tokens",
    ):
        if key in expected and live.get(key) != expected.get(key):
            return False

    expected_defense = expected.get("memory_defense", {})
    live_defense = live.get("memory_defense", {})
    if not expected_defense.get("enabled") or not live_defense.get("enabled"):
        return False
    expected_rules = {
        (str(rule.get("on")), str(rule.get("action")))
        for rule in expected_defense.get("rules", [])
        if isinstance(rule, dict)
    }
    live_rules = {
        (str(rule.get("on")), str(rule.get("action")))
        for rule in live_defense.get("rules", [])
        if isinstance(rule, dict)
    }
    return expected_rules.issubset(live_rules)


def _inferred_snapshot_path(queue_path: Path) -> Path:
    suffix = ".queue.json"
    base = queue_path.name[: -len(suffix)] if queue_path.name.endswith(suffix) else queue_path.stem
    return queue_path.with_name(f"{base}.jsonl")


def _snapshot_path(record: dict, queue_path: Path) -> Path | None:
    raw = record.get("snapshot_path")
    if not raw:
        return None
    try:
        path = Path(str(raw)).resolve()
        expected = _inferred_snapshot_path(queue_path).resolve()
        root = PENDING_ROOT.resolve()
        if path != expected or not path.is_relative_to(root):
            return None
        return path
    except (OSError, ValueError):
        return None


def _discard_queue(queue_path: Path, snapshot_path: Path | None = None) -> None:
    candidates = [queue_path, snapshot_path, _inferred_snapshot_path(queue_path)]
    seen = set()
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            resolved = candidate.resolve()
            if resolved in seen or not resolved.is_relative_to(PENDING_ROOT.resolve()):
                continue
            seen.add(resolved)
            resolved.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def _cleanup_turn_records() -> None:
    TURN_ROOT.mkdir(parents=True, exist_ok=True)
    now = time.time()
    valid = []
    for path in TURN_ROOT.glob("*.json"):
        record = _load_json(path, None)
        if not isinstance(record, dict):
            path.unlink(missing_ok=True)
            continue
        try:
            created_at = float(record.get("created_at") or path.stat().st_mtime)
            expires_at = float(record.get("expires_at") or (created_at + TURN_TTL_SECONDS))
            size = path.stat().st_size
        except (OSError, TypeError, ValueError):
            path.unlink(missing_ok=True)
            continue
        if expires_at <= now:
            path.unlink(missing_ok=True)
            _log(f"Expired unfinished turn capture was removed: {path.name}")
            continue
        valid.append((created_at, path, size))

    total_bytes = sum(item[2] for item in valid)
    valid.sort(key=lambda item: item[0])
    while len(valid) > MAX_TURN_RECORDS or total_bytes > MAX_TURN_BYTES:
        _, path, size = valid.pop(0)
        total_bytes -= size
        path.unlink(missing_ok=True)
        _log(f"Turn-capture cap discarded oldest item: {path.name}")


def _cleanup_pending(wake_timeout: int) -> None:
    PENDING_ROOT.mkdir(parents=True, exist_ok=True)
    now = time.time()
    valid = []
    referenced_snapshots = set()

    for queue_path in sorted(PENDING_ROOT.glob("*.queue.json"), key=lambda path: path.name):
        record = _load_json(queue_path, None)
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            _log(f"Discarding unreadable retain queue item: {queue_path.name}")
            _discard_queue(queue_path)
            continue
        snapshot_path = _snapshot_path(record, queue_path)
        if snapshot_path is None or not snapshot_path.is_file():
            _log(f"Discarding retain item with missing/invalid snapshot: {queue_path.name}")
            _discard_queue(queue_path, snapshot_path)
            continue
        try:
            created_at = float(record.get("created_at") or queue_path.stat().st_mtime)
            expires_at = float(record.get("expires_at") or (created_at + wake_timeout))
        except (OSError, TypeError, ValueError):
            expires_at = now
            created_at = 0.0
        if expires_at <= now:
            _log(f"Expired queued retain was discarded: {queue_path.name}")
            _discard_queue(queue_path, snapshot_path)
            continue
        referenced_snapshots.add(snapshot_path.resolve())
        try:
            size = queue_path.stat().st_size + snapshot_path.stat().st_size
        except OSError:
            size = 0
        valid.append((created_at, queue_path, snapshot_path, size))

    # A snapshot is published just before its queue envelope. Give concurrent
    # publishers a short grace period, then remove any abandoned plaintext.
    for snapshot_path in PENDING_ROOT.glob("*.jsonl"):
        try:
            resolved = snapshot_path.resolve()
            if resolved not in referenced_snapshots and snapshot_path.stat().st_mtime < now - ORPHAN_GRACE_SECONDS:
                snapshot_path.unlink(missing_ok=True)
                _log(f"Removed orphaned retain snapshot: {snapshot_path.name}")
        except OSError:
            pass

    total_bytes = sum(item[3] for item in valid)
    valid.sort(key=lambda item: item[0])
    while len(valid) > MAX_PENDING_ITEMS or total_bytes > MAX_PENDING_BYTES:
        _, queue_path, snapshot_path, size = valid.pop(0)
        total_bytes -= size
        _discard_queue(queue_path, snapshot_path)
        _log(f"Pending retain cap discarded oldest item: {queue_path.name}")


def _purge_session_pending(session_id: str) -> None:
    try:
        PENDING_ROOT.mkdir(parents=True, exist_ok=True)
        for queue_path in PENDING_ROOT.glob("*.queue.json"):
            record = _load_json(queue_path, None)
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                continue
            if str(record["payload"].get("session_id") or "unknown") == str(session_id):
                _discard_queue(queue_path, _snapshot_path(record, queue_path))
                _log(f"Memory-off purged queued retain: {queue_path.name}")
    except OSError as exc:
        _log(f"Could not purge memory-disabled session queue: {exc}")


def _health(url: str, token: str | None) -> bool:
    headers = {"User-Agent": "cortex-codex-hook-bridge/1.2"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(f"{url}/health", headers=headers, method="GET")
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status == 200
    except Exception:
        return False


def _run_queue_item(queue_path: Path, url: str, token: str | None) -> str:
    """Return success, terminal, retry, or policy."""
    record = _load_json(queue_path, None)
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        _log(f"Discarding unreadable retain queue item: {queue_path.name}")
        _discard_queue(queue_path)
        return "terminal"

    snapshot_path = _snapshot_path(record, queue_path)
    if snapshot_path is None or not snapshot_path.is_file():
        _log(f"Discarding retain item with missing/invalid snapshot: {queue_path.name}")
        _discard_queue(queue_path, snapshot_path)
        return "terminal"
    try:
        if float(record.get("expires_at") or 0) <= time.time():
            _log(f"Expired queued retain was discarded: {queue_path.name}")
            _discard_queue(queue_path, snapshot_path)
            return "terminal"
    except (TypeError, ValueError):
        _discard_queue(queue_path, snapshot_path)
        return "terminal"

    # Recheck both the current disabled state and the persistent cancellation
    # generation immediately before submission. The generation survives an
    # OFF -> ON transition, so pre-OFF turns can never reappear afterward.
    session_id = str(record["payload"].get("session_id") or "unknown")
    if (DISABLED_ROOT / f"{_safe_key(session_id)}.json").exists():
        _log(f"Discarding queued retain for memory-disabled session: {queue_path.name}")
        _discard_queue(queue_path, snapshot_path)
        return "terminal"
    try:
        item_epoch = int(record.get("control_epoch") or 0)
    except (TypeError, ValueError):
        item_epoch = 0
    if item_epoch < _cancel_epoch(session_id):
        _log(f"Discarding queued retain cancelled by memory-off: {queue_path.name}")
        _discard_queue(queue_path, snapshot_path)
        return "terminal"

    if not _live_policy_ready(url, token):
        return "policy"

    official_retain = SCRIPT_ROOT / "retain.py"
    payload = json.dumps(record["payload"], ensure_ascii=False).encode("utf-8")
    try:
        result = subprocess.run(
            [sys.executable, str(official_retain)],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"Queued retain failed to run: {exc}")
        return "retry"

    error_text = result.stderr.decode("utf-8", errors="replace").casefold()
    failure_markers = ("retain failed", "unexpected error", "invalid api url")
    failed = result.returncode != 0 or any(marker in error_text for marker in failure_markers)
    if not failed:
        _discard_queue(queue_path, snapshot_path)
        return "success"

    # Hindsight Memory Defense rejects a blocked item with 422. Retrying would
    # create a permanent poison queue containing the sensitive prompt.
    if re.search(r"http\s+422\b", error_text):
        _log(f"Memory Defense/validation rejected retain; discarded {queue_path.name}")
        _discard_queue(queue_path, snapshot_path)
        return "terminal"

    _log(f"Official retain hook did not accept {queue_path.name}: {error_text[-500:]}")
    return "retry"


def _schedule_retry(queue_path: Path) -> None:
    record = _load_json(queue_path, None)
    if not isinstance(record, dict) or not queue_path.is_file():
        return
    try:
        attempts = max(0, int(record.get("attempts") or 0)) + 1
        expires_at = float(record.get("expires_at") or time.time())
    except (TypeError, ValueError):
        attempts = 1
        expires_at = time.time()
    delays = (5, 15, 30, 60)
    delay = delays[min(attempts - 1, len(delays) - 1)]
    record["attempts"] = attempts
    record["next_attempt_at"] = min(expires_at, time.time() + delay)
    if queue_path.is_file():
        _write_json_atomic(queue_path, record)


def _worker() -> int:
    lock = _WorkerLock()
    if not lock.acquire():
        return 0
    try:
        url, token, wake_timeout = _api_settings()
        empty_since = None
        last_policy_log = 0.0

        while True:
            _cleanup_turn_records()
            _cleanup_pending(wake_timeout)
            items = sorted(PENDING_ROOT.glob("*.queue.json"), key=lambda path: path.name)
            if not items:
                if any(TURN_ROOT.glob("*.json")):
                    empty_since = None
                    time.sleep(1)
                    continue
                if empty_since is None:
                    empty_since = time.monotonic()
                elif time.monotonic() - empty_since >= 2:
                    return 0
                time.sleep(0.25)
                continue
            empty_since = None

            if not _health(url, token):
                time.sleep(1)
                continue

            ran_item = False
            policy_blocked = False
            for queue_path in items:
                record = _load_json(queue_path, {})
                try:
                    next_attempt_at = float(record.get("next_attempt_at") or 0)
                except (AttributeError, TypeError, ValueError):
                    next_attempt_at = 0
                if next_attempt_at > time.time():
                    # Strict FIFO: a delayed oldest turn blocks newer turns.
                    break

                ran_item = True
                outcome = _run_queue_item(queue_path, url, token)
                if outcome == "retry":
                    _schedule_retry(queue_path)
                    break
                elif outcome == "policy":
                    policy_blocked = True
                    now = time.monotonic()
                    if now - last_policy_log >= 30:
                        _log("Live Cortex bank policy verification failed; queued prompts will expire without submission")
                        last_policy_log = now
                    break

            time.sleep(5 if policy_blocked else (0.25 if ran_item else 1))
    finally:
        lock.release()


def _parse_payload(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8", errors="replace"))
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--worker":
        return _worker()
    if len(sys.argv) != 2 or sys.argv[1] not in ("session", "recall", "retain"):
        return 0

    mode = sys.argv[1]
    payload_raw = sys.stdin.buffer.read()
    payload = _parse_payload(payload_raw)
    _cleanup_stale_state()

    if mode == "session":
        _wake_controller()
        _spawn_worker()  # Resume any retain left by a shutdown.
        return 0

    if mode == "recall":
        prompt = _prompt_text(payload)
        disabled, skip_current = _control_decision(payload, prompt)
        _record_turn(payload, prompt, retain_allowed=bool(prompt) and not disabled and not skip_current)
        if disabled or skip_current or not prompt:
            return 0
        url, token, _ = _api_settings()
        if not _health(url, token) or not _live_policy_ready(url, token):
            _log("Skipped automatic recall because the live Cortex bank policy could not be verified")
            return 0
        return _delegate_recall(payload_raw)

    record = _take_turn_record(payload)
    control_epoch = _cancel_epoch(payload)
    if record is not None:
        if not record.get("retain_allowed"):
            return 0
        prompt = record.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return 0
        try:
            control_epoch = int(record.get("control_epoch") or 0)
        except (TypeError, ValueError):
            control_epoch = 0
    else:
        # Compatibility fallback if UserPromptSubmit did not run. Parse the
        # transcript immediately; on parse failure, skip rather than risk
        # retaining a message whose opt-out state cannot be established.
        prompt = _latest_user_prompt(payload)
        if not prompt:
            return 0
        disabled, skip_current = _control_decision(payload, prompt)
        if disabled or skip_current:
            return 0

    if not _local_policy_ready():
        _log("Bank privacy policy marker is absent or stale; skipped automatic retain")
        return 0

    _queue_retain(payload, prompt, control_epoch=control_epoch)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # Never break a Codex turn because memory failed.
        _log(f"Unexpected bridge failure: {error}")
        raise SystemExit(0)

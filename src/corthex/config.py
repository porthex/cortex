from __future__ import annotations

import json
import ipaddress
import math
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    url: str
    bank: str
    timeout: float = 30.0


def validate_config(config: Config) -> Config:
    parsed = urlsplit(config.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("url must not contain credentials, a query, or a fragment")
    is_loopback = parsed.hostname.casefold() == "localhost"
    try:
        is_loopback = is_loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not is_loopback:
        raise ValueError("non-loopback Corthex URLs must use HTTPS")
    if not config.bank.strip() or not math.isfinite(config.timeout) or config.timeout <= 0:
        raise ValueError("bank must be non-empty and timeout must be positive")
    return config


def config_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get("CORTHEX_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    if sys.platform == "win32":
        root = Path(env.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return root / "Corthex" / "config.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Corthex" / "config.json"
    root = Path(env.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "corthex" / "config.json"


def save_config(config: Config, path: Path) -> None:
    config = validate_config(config)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(asdict(config), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def load_config(path: Path) -> Config:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Corthex is not configured; run 'corthex configure' ({path})") from exc
    if not isinstance(raw, dict):
        raise ValueError("configuration must be a JSON object")
    try:
        config = Config(
            url=str(raw["url"]).rstrip("/"),
            bank=str(raw["bank"]),
            timeout=float(raw.get("timeout", 30.0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("configuration requires url, bank, and a numeric timeout") from exc
    return validate_config(config)

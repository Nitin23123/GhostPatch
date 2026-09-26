"""Where GhostPatch reads its settings from.

In order of priority (the first value found wins):
1. environment variables that are already set
2. `.env` in the repository being worked on
3. `.env` in the current folder (or a parent folder)
4. the per-user config file written by `ghostpatch init`, so one setup works in every repository
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv


def user_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "ghostpatch" / "config.env"


def settings_files(repo: Path) -> list[Path]:
    """The settings files that exist, highest priority first."""
    candidates = [repo / ".env"]
    found = find_dotenv(usecwd=True)
    if found:
        candidates.append(Path(found))
    candidates.append(user_config_path())
    out: list[Path] = []
    for path in candidates:
        path = path.resolve()
        if path.is_file() and path not in out:
            out.append(path)
    return out


def load_settings(repo: Path) -> list[Path]:
    """Load every settings file (without overriding values already set). Returns the files used."""
    files = settings_files(repo)
    for path in files:
        load_dotenv(path, override=False)
    return files


def write_settings(path: Path, values: dict[str, str | None]) -> None:
    """Set (or, with None, remove) keys in a settings file, keeping everything else in it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and "=" in line and key in remaining:
            value = remaining.pop(key)
            if value is not None:
                out.append(f"{key}={value}")
        else:
            out.append(line)
    out += [f"{key}={value}" for key, value in remaining.items() if value is not None]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    if sys.platform != "win32":
        path.chmod(0o600)  # API keys: readable by this user only

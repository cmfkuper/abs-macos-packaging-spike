# Audiobook Bob — audiobook CD ripper.
# Copyright (C) 2026 Chris Kuper
# Licensed under the GNU General Public License v3.0 or later.
# See the LICENSE file in the project root for details.
"""Persisted per-user settings.

Location (per platform, never next to the app):
  Windows:  %APPDATA%\\ABS Audiobook Ripper\\settings.json
  macOS:    ~/Library/Application Support/ABS Audiobook Ripper/settings.json
  other:    $XDG_CONFIG_HOME (or ~/.config)/ABS Audiobook Ripper/settings.json

Plain human-editable JSON, one flat object. Unknown keys are preserved so the
file can grow (audio quality etc.) without migrations. A missing or corrupt
file silently falls back to defaults.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

APP_DIR_NAME = "ABS Audiobook Ripper"


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_DIR_NAME


def settings_path() -> Path:
    return config_dir() / "settings.json"


def default_output_root() -> Path:
    """The historical hardcoded location: Output/ next to the app."""
    return Path(__file__).resolve().parent.parent / "Output"


# Audio quality tiers. Constant bitrate, source sample rate (44.1 kHz) kept —
# no resampling. The MB/hour figures are measured from real encodes, not
# nominal math (see git history for the verification run).
QUALITY_TIERS = {
    "good":   {"bitrate": "48k",  "channels": 1},
    "better": {"bitrate": "64k",  "channels": 1},
    "best":   {"bitrate": "128k", "channels": 2},
}


class Settings:
    """Flat key/value store backed by settings.json. Add keys freely."""

    DEFAULTS = {
        # output_root is special-cased in get(): its default is computed.
        "audio_quality": "better",
    }

    def __init__(self, path: Path | None = None):
        self.path = path or settings_path()
        self._data: dict = {}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._data = raw if isinstance(raw, dict) else {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            self._data = {}

    def save(self) -> None:
        """Atomic write so a crash mid-save can't corrupt the file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def has(self, key: str) -> bool:
        """True if the key was explicitly saved (vs falling back to a default)."""
        return key in self._data

    def get(self, key: str, default=None):
        if key in self._data:
            return self._data[key]
        if key == "output_root":
            return str(default_output_root())
        return self.DEFAULTS.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value

    # ---- typed accessors for the keys the app uses ----

    @property
    def output_root(self) -> Path:
        return Path(self.get("output_root"))

    @output_root.setter
    def output_root(self, value: Path | str) -> None:
        self.set("output_root", str(value))

    @property
    def audio_quality(self) -> str:
        tier = self.get("audio_quality")
        return tier if tier in QUALITY_TIERS else self.DEFAULTS["audio_quality"]

    @audio_quality.setter
    def audio_quality(self, value: str) -> None:
        if value not in QUALITY_TIERS:
            raise ValueError(f"Unknown audio quality tier: {value!r}")
        self.set("audio_quality", value)


def check_output_folder(path: Path) -> str | None:
    """Return None if the folder is usable, else a plain-English problem."""
    if not path.exists():
        return f"The saved folder no longer exists: {path}"
    if not path.is_dir():
        return f"Not a folder: {path}"
    probe = path / ".abs-write-test.tmp"
    try:
        probe.write_bytes(b"x")
        probe.unlink()
    except OSError:
        return f"You don't have permission to write to: {path}"
    return None

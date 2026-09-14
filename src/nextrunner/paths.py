"""Where nextrunner keeps its files, and how to point it somewhere else.

The board (board.db) is found the first way that applies:

1. its own variable: NEXTRUNNER_DB
2. NEXTRUNNER_HOME, one folder that holds it
3. the usual place for the platform:

    macOS    ~/Library/Application Support/nextrunner
    Linux    $XDG_DATA_HOME/nextrunner (board.db); default ~/.local/share
    Windows  %LOCALAPPDATA%\\nextrunner (board.db)

Use NEXTRUNNER_HOME to develop or try things out without touching the real board.
"""
import os
import sys
from pathlib import Path

APP = "nextrunner"


def _env_dir(name):
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _platform_dirs():
    """(config, data, log) folders for this platform."""
    home = Path.home()
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support" / APP
        return support, support, home / "Library" / "Logs" / APP
    if sys.platform == "win32":
        config = _env_dir("APPDATA") or home / "AppData" / "Roaming"
        local = _env_dir("LOCALAPPDATA") or home / "AppData" / "Local"
        return config / APP, local / APP, local / APP
    return ((_env_dir("XDG_CONFIG_HOME") or home / ".config") / APP,
            (_env_dir("XDG_DATA_HOME") or home / ".local" / "share") / APP,
            (_env_dir("XDG_STATE_HOME") or home / ".local" / "state") / APP)


def _pick(var, filename, which):
    explicit = os.environ.get(var)
    if explicit:
        return Path(explicit).expanduser()
    root = _env_dir("NEXTRUNNER_HOME")
    return root / filename if root else _platform_dirs()[which] / filename


def db_file():
    return _pick("NEXTRUNNER_DB", "board.db", 1)

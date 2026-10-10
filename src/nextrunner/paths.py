"""Where nextrunner keeps its files, and how to point it somewhere else.

Three files: the board (board.db), the agent settings (agents.json) and the
dispatcher log (dispatch.log). Each is found the first way that applies:

1. its own variable: NEXTRUNNER_DB, NEXTRUNNER_AGENTS, NEXTRUNNER_LOG
2. NEXTRUNNER_HOME, one folder that holds all three (also `nextrunner --home DIR`)
3. the usual place for the platform:

    macOS    ~/Library/Application Support/nextrunner  (log in ~/Library/Logs/nextrunner)
    Linux    $XDG_CONFIG_HOME/nextrunner (agents.json), $XDG_DATA_HOME/nextrunner (board.db),
             $XDG_STATE_HOME/nextrunner (dispatch.log); defaults ~/.config, ~/.local/share, ~/.local/state
    Windows  %APPDATA%\\nextrunner (agents.json), %LOCALAPPDATA%\\nextrunner (board.db, dispatch.log)

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


def agents_file():
    return _pick("NEXTRUNNER_AGENTS", "agents.json", 0)


def db_file():
    return _pick("NEXTRUNNER_DB", "board.db", 1)


def log_file():
    """The dispatcher log. With only NEXTRUNNER_DB set, it sits next to that board so a trial run stays in one folder."""
    explicit, db = os.environ.get("NEXTRUNNER_LOG"), os.environ.get("NEXTRUNNER_DB")
    if not explicit and db and not os.environ.get("NEXTRUNNER_HOME"):
        return Path(db).expanduser().parent / "dispatch.log"
    return _pick("NEXTRUNNER_LOG", "dispatch.log", 2)


def describe():
    """[(label, path, why)] for `nextrunner where`: each file and the setting that put it there."""
    def why(var):
        if os.environ.get(var):
            return var
        return "NEXTRUNNER_HOME" if os.environ.get("NEXTRUNNER_HOME") else "platform default"
    return [("board", db_file(), why("NEXTRUNNER_DB")),
            ("agents", agents_file(), why("NEXTRUNNER_AGENTS")),
            ("log", log_file(), why("NEXTRUNNER_LOG"))]


def private_dir(path):
    """Make the folder if needed. A folder made here is readable by this user only; an existing one keeps its mode."""
    path = Path(path)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    return path


def private_file(path, text=None):
    """Write `text` to `path` (or just create it), readable by this user only, whether or not it existed."""
    path = Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if text is not None else 0), 0o600)
    with os.fdopen(fd, "w") as f:
        if text is not None:
            f.write(text)
    os.chmod(path, 0o600)
    return path


def inside(path, root):
    """True when `path` is `root` or lies under it, after expanding ~ and following links."""
    try:
        Path(path).expanduser().resolve().relative_to(Path(root).expanduser().resolve())
    except ValueError:
        return False
    return True


def is_sandboxed():
    """True when any of the variables above moved a file away from the platform default."""
    return any(os.environ.get(v) for v in ("NEXTRUNNER_HOME", "NEXTRUNNER_DB", "NEXTRUNNER_AGENTS", "NEXTRUNNER_LOG"))

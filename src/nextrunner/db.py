"""The board file: schema, connection, and the few helpers everything shares."""
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import paths

TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT '',
    cwd           TEXT,
    assignee      TEXT,
    strict        INTEGER NOT NULL DEFAULT 0,
    edit          INTEGER NOT NULL DEFAULT 0,
    commit_changes INTEGER NOT NULL DEFAULT 0,
    follows       TEXT,
    held          INTEGER NOT NULL DEFAULT 0,
    fanout        TEXT,
    status        TEXT NOT NULL DEFAULT 'ready'
                  CHECK (status IN ('ready', 'running', 'done', 'blocked', 'cancelled')),
    claimed_by    TEXT,
    claim_expires REAL,
    claim_token   TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    result        TEXT,
    created_by    TEXT NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
"""

SCHEMA = TASKS_TABLE + """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    at      REAL NOT NULL,
    agent   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    text    TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agents (
    name       TEXT PRIMARY KEY,
    down_until REAL NOT NULL,
    reason     TEXT NOT NULL DEFAULT ''
);
"""


# A task is free when it is ready, or when its claim ran out (the agent died),
# and nobody is holding it back for approval.
CLAIMABLE = "held = 0 AND (status = 'ready' OR (status = 'running' AND claim_expires < :now))"


def now():
    return time.time()


def connect(path=None):
    path = Path(path) if path else paths.db_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'tasks'").fetchone():
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    else:
        cols = {col["name"] for col in conn.execute("PRAGMA table_info(tasks)")}
        if "edit" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN edit INTEGER NOT NULL DEFAULT 0")  # board made before --edit
        if "claim_token" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN claim_token TEXT")  # board made before claim tokens
        if "commit_changes" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN commit_changes INTEGER NOT NULL DEFAULT 0")  # before --commit
        if "follows" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN follows TEXT")  # board made before --follow
        if "held" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN held INTEGER NOT NULL DEFAULT 0")  # board made before --hold
        if "fanout" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN fanout TEXT")  # board made before fan-out
        if "'cancelled'" not in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'tasks'").fetchone()["sql"]:
            widen_status(conn, path)  # board made before `cancel`: the CHECK on status must be rebuilt
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def widen_status(conn, path):
    """Rebuild the tasks table so status may be 'cancelled'. SQLite cannot change a CHECK in place.

    A copy of the board file is kept next to it first. Rows, columns, IDs and
    the events table are untouched; events keep pointing at `tasks` by name.
    """
    backup = path.with_name(path.name + ".before-cancel")
    if not backup.exists():
        shutil.copy2(path, backup)
    cols = ", ".join(col["name"] for col in conn.execute("PRAGMA table_info(tasks)"))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(TASKS_TABLE.replace("IF NOT EXISTS tasks", "tasks_new"))
        conn.execute(f"INSERT INTO tasks_new ({cols}) SELECT {cols} FROM tasks")
        conn.execute("DROP TABLE tasks")
        conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def log(conn, task_id, agent, kind, text=""):
    conn.execute(
        "INSERT INTO events (task_id, at, agent, kind, text) VALUES (?, ?, ?, ?, ?)",
        (task_id, now(), agent, kind, text),
    )


def board_dir(conn):
    """The folder holding the board file."""
    return Path(conn.execute("PRAGMA database_list").fetchone()["file"]).parent


def get(conn, task_id):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

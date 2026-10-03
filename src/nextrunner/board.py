"""Tasks and events: add, claim, note, beat, done, release, reopen."""
import hashlib
import os
import secrets
import sqlite3

from .db import CLAIMABLE, get, log, now, tx

ME = os.environ.get("NEXTRUNNER_AGENT", "human")

ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
ID_EPOCH = 1767225600  # 2026-01-01 00:00 UTC; six base-36 digits of seconds last until about 2095


def new_task_id(t=None):
    """'t_' + seconds since ID_EPOCH in six base-36 digits + four random base-36 digits.

    IDs sort by creation time as text, and two can only match when both tasks
    are made in the same second and draw the same four random digits.
    """
    n, stamp = max(0, int(now() if t is None else t) - ID_EPOCH), ""
    for _ in range(6):
        n, r = divmod(n, 36)
        stamp = ID_ALPHABET[r] + stamp
    return "t_" + stamp + "".join(secrets.choice(ID_ALPHABET) for _ in range(4))


def add(conn, title, body="", to=None, strict=False, cwd=None, by=ME, edit=False, commit=False, follows=None,
        hold=False):
    edit = edit or commit  # committing the changes only makes sense if the agent may make them
    if follows:
        earlier = get(conn, follows)
        if earlier is None:
            raise ValueError(f"no task {follows}")
        # Continue with the agent that last ran the earlier task, in the same folder, unless told otherwise.
        to = to or last_session(conn, follows)[0] or earlier["claimed_by"] or earlier["assignee"]
        cwd = cwd or earlier["cwd"]
    level = "edit, commit" if commit else "edit" if edit else "read-only"
    for attempt in range(5):
        t = now()
        task_id = new_task_id(t)
        try:
            with tx(conn):
                conn.execute(
                    "INSERT INTO tasks (id, title, body, cwd, assignee, strict, edit, commit_changes, follows, held, "
                    "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, title, body, cwd, to, int(strict), int(edit), int(commit), follows, int(hold), by, t, t),
                )
                log(conn, task_id, by, "created", f"to={to or 'anyone'}{' (strict)' if strict else ''}, {level}"
                    f"{f', follows {follows}' if follows else ''}{', held for approval' if hold else ''}")
            return task_id
        except sqlite3.IntegrityError:
            if attempt == 4:
                raise  # five clashes in a row means something else is wrong


def claim(conn, task_id, agent, ttl=900, steal=False):
    """Take a task. Only one caller can win; an expired claim counts as free.

    Returns a token for this claim, or None if the claim was lost.
    """
    t, token = now(), secrets.token_hex(8)
    with tx(conn):
        won = conn.execute(
            "UPDATE tasks SET status = 'running', claimed_by = :agent, claim_expires = :exp, claim_token = :token, updated_at = :now "
            f"WHERE id = :id AND {CLAIMABLE} AND (assignee IS NULL OR assignee = :agent OR :steal)",
            {"agent": agent, "exp": t + ttl, "token": token, "now": t, "id": task_id, "steal": int(steal)},
        ).rowcount == 1
        if won:
            log(conn, task_id, agent, "claimed", f"for {ttl:g}s")
    return token if won else None


def claim_next(conn, agent, ttl=900):
    """Claim the oldest free task for this agent or anyone. Returns (id, token) or None."""
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE {CLAIMABLE} AND (assignee IS NULL OR assignee = :agent) "
        "ORDER BY created_at, rowid",
        {"now": now(), "agent": agent},
    ).fetchall()
    for row in rows:
        token = claim(conn, row["id"], agent, ttl)
        if token:
            return row["id"], token
    return None


def note(conn, task_id, agent, text):
    with tx(conn):
        log(conn, task_id, agent, "note", text)


def _owned(conn, task_id, agent, sets, params, kind, text, token=None):
    """Change a task only while `agent` still holds it, and with a token, only under that claim."""
    with tx(conn):
        ok = conn.execute(
            f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ? AND status = 'running' AND claimed_by = ? "
            "AND (? IS NULL OR claim_token = ?)",
            (*params, now(), task_id, agent, token, token),
        ).rowcount == 1
        if ok:
            log(conn, task_id, agent, kind, text)
    return ok


def beat(conn, task_id, agent, ttl=900, token=None):
    return _owned(conn, task_id, agent, "claim_expires = ?", (now() + ttl,), "beat", f"for {ttl:g}s", token)


def done(conn, task_id, agent, result, token=None):
    sets = "status = 'done', result = ?, claim_expires = NULL"
    return _owned(conn, task_id, agent, sets, (result,), "done", result, token)


def release(conn, task_id, agent, reason="", kind="released", token=None):
    """Hand a task back. kind='failed' also counts as an attempt."""
    sets = "status = 'ready', claimed_by = NULL, claim_expires = NULL, attempts = attempts + ?"
    return _owned(conn, task_id, agent, sets, (int(kind == "failed"),), kind, reason, token)


def brief_hash(task):
    """A short fingerprint of a task's title and brief, so an approval says what text it covered."""
    return hashlib.sha256(f"{task['title']}\n{task['body']}".encode()).hexdigest()[:12]


def approve(conn, task_id, by=ME):
    """Let a held task run. Returns False if the task is not held."""
    with tx(conn):
        task = get(conn, task_id)
        ok = conn.execute("UPDATE tasks SET held = 0, updated_at = ? WHERE id = ? AND held = 1",
                          (now(), task_id)).rowcount == 1
        if ok:
            log(conn, task_id, by, "approved", f"brief {brief_hash(task)}")
    return ok


def reopen(conn, task_id, by=ME):
    with tx(conn):
        ok = conn.execute(
            "UPDATE tasks SET status = 'ready', claimed_by = NULL, claim_expires = NULL, attempts = 0, "
            "updated_at = ? WHERE id = ? AND status IN ('blocked', 'done')",
            (now(), task_id),
        ).rowcount == 1
        if ok:
            # Forget earlier failures so every agent may try again.
            conn.execute("UPDATE events SET kind = 'failed-before-reopen' WHERE task_id = ? AND kind = 'failed'", (task_id,))
            log(conn, task_id, by, "reopened")
    return ok


def last_session(conn, task_id, agent=None):
    """(agent, session ID) of the latest run on a task that left one, or (None, None)."""
    row = conn.execute(
        "SELECT agent, text FROM events WHERE task_id = ? AND kind = 'session' AND (? IS NULL OR agent = ?) "
        "ORDER BY id DESC LIMIT 1", (task_id, agent, agent)).fetchone()
    return (row["agent"], row["text"]) if row else (None, None)

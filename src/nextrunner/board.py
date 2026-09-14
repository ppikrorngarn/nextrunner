"""Tasks and events: add, claim, note, beat, done, release, reopen."""
import os
import secrets

from .db import CLAIMABLE, log, now, tx

ME = os.environ.get("NEXTRUNNER_AGENT", "human")


def add(conn, title, body="", to=None, strict=False, cwd=None, by=ME):
    t = now()
    task_id = "t_" + secrets.token_hex(4)
    with tx(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, body, cwd, assignee, strict, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, title, body, cwd, to, int(strict), by, t, t),
        )
        log(conn, task_id, by, "created", f"to={to or 'anyone'}{' (strict)' if strict else ''}")
    return task_id


def claim(conn, task_id, agent, ttl=900, steal=False):
    """Take a task. Only one caller can win; an expired claim counts as free."""
    t = now()
    with tx(conn):
        won = conn.execute(
            "UPDATE tasks SET status = 'running', claimed_by = :agent, claim_expires = :exp, updated_at = :now "
            f"WHERE id = :id AND {CLAIMABLE} AND (assignee IS NULL OR assignee = :agent OR :steal)",
            {"agent": agent, "exp": t + ttl, "now": t, "id": task_id, "steal": int(steal)},
        ).rowcount == 1
        if won:
            log(conn, task_id, agent, "claimed", f"for {ttl:g}s")
    return won


def claim_next(conn, agent, ttl=900):
    """Claim the oldest free task for this agent or anyone."""
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE {CLAIMABLE} AND (assignee IS NULL OR assignee = :agent) "
        "ORDER BY created_at, rowid",
        {"now": now(), "agent": agent},
    ).fetchall()
    for row in rows:
        if claim(conn, row["id"], agent, ttl):
            return row["id"]
    return None


def note(conn, task_id, agent, text):
    with tx(conn):
        log(conn, task_id, agent, "note", text)


def _owned(conn, task_id, agent, sets, params, kind, text):
    """Change a task only while `agent` still holds it."""
    with tx(conn):
        ok = conn.execute(
            f"UPDATE tasks SET {sets}, updated_at = ? WHERE id = ? AND status = 'running' AND claimed_by = ?",
            (*params, now(), task_id, agent),
        ).rowcount == 1
        if ok:
            log(conn, task_id, agent, kind, text)
    return ok


def beat(conn, task_id, agent, ttl=900):
    return _owned(conn, task_id, agent, "claim_expires = ?", (now() + ttl,), "beat", f"for {ttl:g}s")


def done(conn, task_id, agent, result):
    sets = "status = 'done', result = ?, claim_expires = NULL"
    return _owned(conn, task_id, agent, sets, (result,), "done", result)


def release(conn, task_id, agent, reason="", kind="released"):
    """Hand a task back. kind='failed' also counts as an attempt."""
    sets = "status = 'ready', claimed_by = NULL, claim_expires = NULL, attempts = attempts + ?"
    return _owned(conn, task_id, agent, sets, (int(kind == "failed"),), kind, reason)


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

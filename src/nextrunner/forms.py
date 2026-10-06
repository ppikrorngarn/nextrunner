"""Turning what someone typed into a new task. Used by the full-screen UI."""
import os

from .board import ME, add


def add_from_answers(conn, answers, follows=None):
    """Turn the UI's add form into a task. Returns (task id or None, message)."""
    title = answers.get("title", "").strip()
    if not title:
        return None, "cancelled: no title"
    level = (answers.get("level") or "r").strip().lower()[:1]
    if level not in "rec":
        return None, f"cancelled: level must be r, e or c, not {level!r}"
    to = answers.get("to", "").strip() or None
    strict = answers.get("strict", "").strip().lower().startswith("y")
    if strict and not (to or follows):
        return None, "cancelled: strict needs an agent"
    cwd = os.path.expanduser(answers.get("cwd", "").strip()) or None
    if cwd and not os.path.isdir(cwd):
        return None, f"cancelled: no folder {cwd}"
    if level in "ec" and not (cwd or follows):
        return None, "cancelled: an edit task needs a folder"
    hold = answers.get("hold", "").strip().lower().startswith("y")
    task_id = add(conn, title, answers.get("body", "").strip(), to, strict, cwd, ME,
                  edit=level in "ec", commit=level == "c", follows=follows, hold=hold)
    return task_id, f"added {task_id}"

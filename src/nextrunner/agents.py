"""Who the dispatcher can start, which of them is resting, and who gets a task."""
import json
import re
import sys

from . import paths
from .db import now

MAX_ATTEMPTS = 3   # a strict task blocks after this many failures by its one agent
COOLDOWN_MIN = 30  # how long an agent is skipped after a quota or login error


# Failures that mean "this agent is unavailable", not "this task is bad".
# Phrases come from real CLI output; see LimitTextTest for the samples.
LIMIT_RE = re.compile(
    r"usage limit|rate limit|quota|hit your [^.\n]{0,40}?(limit|budget)|"
    r"out of (extra usage|(usage )?credits?|tokens|funds)|credit balance|"
    r"credits? (exhausted|depleted)|insufficient (available )?credits?|"
    r"session expired|token (has )?expired|failed to authenticate|"
    r"sign in again|log in again|run /login|run `?\w+ (auth )?login\b|invalid api key|"
    r"overloaded|at capacity|temporarily unavailable|\b429\b",
    re.I,
)


def limit_reason(text, width=200):
    """The line of an agent's output that says why it is unavailable, for the board and the log."""
    for line in text.splitlines():
        if LIMIT_RE.search(line):
            return line.strip()[:width]
    return text.strip()[:width]


def load_agents():
    """Read how to start each agent. Key order is the failover order.

    {"NAME": {"cmd": [...], "cmd_edit": [...], "reply": "stdout" | "file" | "json:<key>"}}
    cmd runs read-only tasks. cmd_edit runs tasks added with --edit; leave it
    out and the agent never gets one. {prompt}, {cwd}, {out} and {board} (the
    folder holding the board file) are filled in. "file" reads the reply from
    {out}; "json:<key>" parses stdout and fails the run if is_error is set.
    """
    path = paths.agents_file()
    if not path.exists():
        sys.exit(f"no {path}: copy agents.example.json and describe your agents")
    return json.loads(path.read_text())


def set_down(conn, name, minutes, reason=""):
    conn.execute(
        "INSERT INTO agents (name, down_until, reason) VALUES (?, ?, ?) "
        "ON CONFLICT (name) DO UPDATE SET down_until = excluded.down_until, reason = excluded.reason",
        (name, now() + minutes * 60, reason),
    )


def set_up(conn, name):
    conn.execute("DELETE FROM agents WHERE name = ?", (name,))


def is_up(conn, name):
    row = conn.execute("SELECT down_until FROM agents WHERE name = ?", (name,)).fetchone()
    return not row or row["down_until"] <= now()


# ---- dispatcher -------------------------------------------------------------

def command_for(spec, task):
    """The command this agent runs for this task, or None if it may not take it."""
    return spec.get("cmd_edit") if task["edit"] else spec["cmd"]


def candidates(conn, task, agents):
    """The agents that may still run this task, in failover order, or (None, why) when none may."""
    preferred = [task["assignee"]] if task["assignee"] else []
    names = preferred if task["strict"] else list(dict.fromkeys(preferred + list(agents)))
    names = [n for n in names if command_for(agents[n], task)]
    if not names:
        return None, "blocked: no agent that may take this has a command for edit tasks"
    if task["strict"]:
        names = names if task["attempts"] < MAX_ATTEMPTS else []
    else:
        failed = {r["agent"] for r in conn.execute(
            "SELECT agent FROM events WHERE task_id = ? AND kind = 'failed'", (task["id"],))}
        names = [n for n in names if n not in failed]
    return names, None


def pick_agent(conn, task, agents):
    """Who should run this task now: (name, None), or (None, why).

    why is 'waiting' (every agent left is resting) or 'blocked: ...'.
    """
    names, why = candidates(conn, task, agents)
    if names is None:
        return None, why
    for name in names:
        if is_up(conn, name):
            return name, None
    return None, "waiting" if names else "blocked: every agent that could take this has failed it"

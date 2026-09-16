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

    {"NAME": {"cmd": [...], "reply": "stdout" | "file" | "json:<key>"}}
    {prompt}, {cwd}, {out} and {board} (the folder holding the board file)
    are filled in. "file" reads the reply from {out}; "json:<key>" parses
    stdout and fails the run if is_error is set.
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

def candidates(conn, task, agents):
    """The agents that may still run this task, in failover order."""
    preferred = [task["assignee"]] if task["assignee"] else []
    names = preferred if task["strict"] else list(dict.fromkeys(preferred + list(agents)))
    if task["strict"]:
        names = names if task["attempts"] < MAX_ATTEMPTS else []
    else:
        failed = {r["agent"] for r in conn.execute(
            "SELECT agent FROM events WHERE task_id = ? AND kind = 'failed'", (task["id"],))}
        names = [n for n in names if n not in failed]
    return names


def pick_agent(conn, task, agents):
    """Who should run this task now: (name, None), or (None, why).

    why is 'waiting' (every agent left is resting) or 'blocked: ...'.
    """
    names = candidates(conn, task, agents)
    for name in names:
        if is_up(conn, name):
            return name, None
    return None, "waiting" if names else "blocked: every agent that could take this has failed it"

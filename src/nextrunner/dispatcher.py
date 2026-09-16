"""The dispatcher: one pass over the board, starting an agent for every free task."""
import time
from pathlib import Path

from .agents import COOLDOWN_MIN, LIMIT_RE, limit_reason, load_agents, pick_agent, set_down
from .board import claim, done, release
from .db import CLAIMABLE, get, log, now, tx
from .runner import build_prompt, run_agent

def say_now(text):
    """Print and flush, so a log file shows each line as it happens."""
    print(text, flush=True)


def clock(t):
    """A board time as HH:MM on the local clock, for messages."""
    return time.strftime("%H:%M", time.localtime(t))


def dispatch(conn, agents=None, timeout=600, dry_run=False, say=say_now):
    """One pass over the board: run every free task, rerouting when an agent fails."""
    agents = agents or load_agents()
    board = Path(conn.execute("PRAGMA database_list").fetchone()["file"]).parent
    free = conn.execute(f"SELECT id FROM tasks WHERE {CLAIMABLE} ORDER BY created_at, rowid", {"now": now()}).fetchall()
    for row in free:  # oldest first
        task_id = row["id"]
        while True:  # until the task is done, waiting or blocked; a failed run goes on to the next agent
            task = get(conn, task_id)
            if task["assignee"] and task["assignee"] not in agents:
                break  # no starter for this name: someone pulls the task by hand
            agent, why = pick_agent(conn, task, agents)
            if why == "waiting":
                say(f"{task_id} waiting: every agent that could take it is resting; "
                    f"run dispatch again later, or use --loop")
                break
            if not agent:
                with tx(conn):
                    conn.execute(f"UPDATE tasks SET status = 'blocked', claimed_by = NULL, updated_at = :now "
                                 f"WHERE id = :id AND {CLAIMABLE}", {"now": now(), "id": task_id})
                    log(conn, task_id, "dispatcher", "blocked", why.partition(": ")[2])
                say(f"{task_id} {why}")
                break
            if dry_run:
                say(f"{task_id} would go to {agent}")
                break
            spec, cwd = agents[agent], task["cwd"] or str(board)
            token = claim(conn, task_id, agent, ttl=timeout + 60, steal=True)
            if not token:
                break  # someone else took it first
            say(f"{task_id} -> {agent}")
            ok, text = run_agent(spec["cmd"], spec.get("reply", "stdout"), build_prompt(conn, task, agent),
                                 cwd, board, timeout)
            if ok:
                if done(conn, task_id, agent, text, token):
                    say(f"{task_id} done by {agent}")
                else:
                    say(f"{task_id} result from {agent} was dropped: the claim was lost")
                break
            if LIMIT_RE.search(text):
                reason = limit_reason(text)
                set_down(conn, agent, COOLDOWN_MIN, reason)
                release(conn, task_id, agent, text, kind="limited", token=token)
                say(f"{task_id} {agent} resting until {clock(now() + COOLDOWN_MIN * 60)} ({COOLDOWN_MIN} min): {reason}")
            else:
                release(conn, task_id, agent, text, kind="failed", token=token)
                say(f"{task_id} {agent} failed, rerouting")

"""The dispatcher: one pass over the board, starting an agent for every free task."""
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from .agents import COOLDOWN_MIN, LIMIT_RE, command_for, limit_reason, load_agents, pick_agent, set_down
from .board import claim, done, release
from .db import CLAIMABLE, get, log, now, tx
from .runner import build_prompt, commit_changes, git_snapshot, run_agent

def say_now(text):
    """Print and flush, so a log file shows each line as it happens."""
    print(text, flush=True)


def clock(t):
    """A board time as HH:MM on the local clock, for messages."""
    return time.strftime("%H:%M", time.localtime(t))


def dispatch(conn, agents=None, timeout=600, dry_run=False, say=say_now, jobs=1):
    """One pass over the board: run every free task, rerouting when an agent fails.

    Up to `jobs` runs go at once in total, and up to each agent's "parallel"
    setting (default 1) on one agent. Only the agent runs happen in worker
    threads; every board read and write stays on this thread and this
    connection, so claims, tokens and rests work as they do with one job.
    """
    agents = agents or load_agents()
    board = Path(conn.execute("PRAGMA database_list").fetchone()["file"]).parent
    free = conn.execute(f"SELECT id FROM tasks WHERE {CLAIMABLE} ORDER BY created_at, rowid", {"now": now()}).fetchall()
    pending = [row["id"] for row in free]  # oldest first; a failed task goes back to the front
    running = {}                           # future -> (task_id, agent, token, git snapshot)
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        while pending or running:
            busy = Counter(agent for _, agent, *_ in running.values())
            for task_id in list(pending):
                if len(running) >= max(1, jobs):
                    break
                task = get(conn, task_id)
                if task["assignee"] and task["assignee"] not in agents:
                    pending.remove(task_id)  # no starter for this name: someone pulls the task by hand
                    continue
                agent, why = pick_agent(conn, task, agents, busy)
                if why == "busy":
                    continue  # its agent is running another task; look again when a run ends
                if why == "waiting":
                    pending.remove(task_id)
                    say(f"{task_id} waiting: every agent that could take it is resting; "
                        f"run dispatch again later, or use --loop")
                    continue
                pending.remove(task_id)
                if not agent:
                    with tx(conn):
                        conn.execute(f"UPDATE tasks SET status = 'blocked', claimed_by = NULL, updated_at = :now "
                                     f"WHERE id = :id AND {CLAIMABLE}", {"now": now(), "id": task_id})
                        log(conn, task_id, "dispatcher", "blocked", why.partition(": ")[2])
                    say(f"{task_id} {why}")
                    continue
                if dry_run:
                    say(f"{task_id} would go to {agent}")
                    continue
                spec, cwd = agents[agent], task["cwd"] or str(board)
                token = claim(conn, task_id, agent, ttl=timeout + 60, steal=True)
                if not token:
                    continue  # someone else took it first
                say(f"{task_id} -> {agent}")
                before = git_snapshot(cwd) if task["commit_changes"] else None
                future = pool.submit(run_agent, command_for(spec, task), spec.get("reply", "stdout"),
                                     build_prompt(conn, task, agent, cwd), cwd, board, timeout)
                running[future] = (task_id, agent, token, before)
                busy[agent] += 1
            if not running:
                break  # nothing started and nothing left to wait for
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                task_id, agent, token, before = running.pop(future)
                try:
                    ok, text = future.result()
                except Exception as err:  # a bug in the run itself counts as a failed run
                    ok, text = False, f"dispatcher error: {err}"
                if ok:
                    if done(conn, task_id, agent, text, token):
                        say(f"{task_id} done by {agent}")
                        task = get(conn, task_id)
                        if task["commit_changes"]:
                            outcome = commit_changes(task, agent, before, text)
                            with tx(conn):
                                log(conn, task_id, "dispatcher", "commit", outcome)
                            say(f"{task_id} commit: {outcome}")
                    else:
                        say(f"{task_id} result from {agent} was dropped: the claim was lost")
                    continue
                if LIMIT_RE.search(text):
                    reason = limit_reason(text)
                    set_down(conn, agent, COOLDOWN_MIN, reason)
                    release(conn, task_id, agent, text, kind="limited", token=token)
                    say(f"{task_id} {agent} resting until {clock(now() + COOLDOWN_MIN * 60)} ({COOLDOWN_MIN} min): {reason}")
                else:
                    release(conn, task_id, agent, text, kind="failed", token=token)
                    say(f"{task_id} {agent} failed, rerouting")
                pending.insert(0, task_id)

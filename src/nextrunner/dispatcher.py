"""The dispatcher: one pass over the board, starting an agent for every free task."""
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from .agents import COOLDOWN_MIN, TRANSIENT_HITS, TRANSIENT_MIN, classify, limit_reason, load_agents, pick_agent, \
    rest_ends, set_down
from .board import claim, done, last_session, release
from .db import CLAIMABLE, board_dir, get, log, now, tx
from .runner import build_prompt, command_and_session, commit_changes, git_snapshot, run_agent

def self_command():
    """The command that starts this program again: the bundled app itself, or python -m nextrunner."""
    return [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "nextrunner"]


def say_now(text):
    """Print and flush, so a log file shows each line as it happens."""
    print(text, flush=True)


def clock(t):
    """A board time as HH:MM on the local clock, for messages."""
    return time.strftime("%H:%M", time.localtime(t))


def dispatch(conn, agents=None, timeout=600, dry_run=False, say=say_now, jobs=1, sleep=time.sleep):
    """One pass over the board: run every free task, rerouting when an agent fails.

    Up to `jobs` runs go at once in total, and up to each agent's "parallel"
    setting (default 1) on one agent. Only the agent runs happen in worker
    threads; every board read and write stays on this thread and this
    connection, so claims, tokens and rests work as they do with one job.

    `timeout` is the default for one run; an agent's own "timeout" in
    agents.json wins. A run that ends with a passing error (the model is at
    capacity) rests its agent TRANSIENT_MIN minutes, and a task that has no
    one else to go to waits inside the pass for that rest to end. A quota or
    login error rests the agent COOLDOWN_MIN minutes, and such a task is left
    for the next pass.
    """
    agents = agents or load_agents()
    board = board_dir(conn)
    free = conn.execute(f"SELECT id FROM tasks WHERE {CLAIMABLE} ORDER BY created_at, rowid", {"now": now()}).fetchall()
    pending = [row["id"] for row in free]  # oldest first; a failed task goes back to the front
    running = {}                           # future -> (task_id, agent, token, git snapshot)
    flaky = Counter()                      # passing errors per agent in this pass
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        while pending or running:
            busy = Counter(agent for _, agent, *_ in running.values())
            wake = None                    # when the earliest short rest a pending task waits on ends
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
                    back = rest_ends(conn, task, agents)
                    if back and back - now() <= TRANSIENT_MIN * 60:
                        wake = min(wake or back, back)  # a short rest: keep the task and come back to it
                        continue
                    pending.remove(task_id)
                    until = f" until {clock(back)}" if back else ""
                    say(f"{task_id} waiting: every agent that could take it is resting{until}; "
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
                limit = spec.get("timeout", timeout)
                token = claim(conn, task_id, agent, ttl=limit + 60, steal=True)
                if not token:
                    continue  # someone else took it first
                say(f"{task_id} -> {agent}")
                before = git_snapshot(cwd) if task["commit_changes"] else None
                cmd, session = command_and_session(conn, task, agent, spec)
                if session:
                    say(f"{task_id} resumes {agent} session {session}")
                attempt = conn.execute("SELECT count(*) FROM events WHERE task_id = ? AND kind = 'claimed'",
                                       (task_id,)).fetchone()[0]
                trace = board / "runs" / task_id / f"{attempt}-{agent}.log"
                future = pool.submit(run_agent, cmd, spec.get("reply", "stdout"),
                                     build_prompt(conn, task, agent, cwd, resuming=bool(session)), cwd, board,
                                     limit, session, spec.get("session"), trace, spec.get("usage"))
                running[future] = (task_id, agent, token, before)
                busy[agent] += 1
            if not running:
                if wake is None:
                    break  # nothing started and nothing left to wait for
                sleep(max(0.0, wake - now()) + 0.5)  # the only pending tasks wait on a short rest
                continue
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                task_id, agent, token, before = running.pop(future)
                try:
                    ok, text, session, line = future.result()
                except Exception as err:  # a bug in the run itself counts as a failed run
                    ok, text, session, line = False, f"dispatcher error: {err}", None, ""
                with tx(conn):
                    if line:
                        log(conn, task_id, agent, "run", line)
                    if session and session != last_session(conn, task_id, agent)[1]:
                        log(conn, task_id, agent, "session", session)
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
                kind = classify(text)
                if kind == "transient":
                    flaky[agent] += 1
                    if flaky[agent] >= TRANSIENT_HITS:
                        kind = "limited"  # busy this often is as good as out of quota: rest it properly
                if kind:
                    minutes = TRANSIENT_MIN if kind == "transient" else COOLDOWN_MIN
                    reason = limit_reason(text)
                    set_down(conn, agent, minutes, reason)
                    release(conn, task_id, agent, text, kind="limited", token=token)
                    say(f"{task_id} {agent} resting until {clock(now() + minutes * 60)} ({minutes} min): {reason}")
                else:
                    release(conn, task_id, agent, text, kind="failed", token=token)
                    say(f"{task_id} {agent} failed, rerouting")
                pending.insert(0, task_id)

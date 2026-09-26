"""Plain-text views of the board, shared by list, show and status."""
import time
from collections import Counter

from .agents import is_up
from .db import now

def stamp(t):
    return time.strftime("%m-%d %H:%M:%S", time.localtime(t))


def shown_status(task):
    expired = task["status"] == "running" and task["claim_expires"] < now()
    return "expired" if expired else task["status"]


def print_task(conn, task):
    print(f"{task['id']}  {task['title']}")
    print(f"  status:   {shown_status(task)}" + (f" (held by {task['claimed_by']})" if task["status"] == "running" else ""))
    print(f"  for:      {task['assignee'] or 'anyone'}{' (strict)' if task['strict'] else ''}")
    may = "edit files in its folder" if task["edit"] else "read only"
    print(f"  may:      {may}{'; the dispatcher commits the changes' if task['commit_changes'] else ''}")
    print(f"  folder:   {task['cwd'] or '-'}")
    print(f"  attempts: {task['attempts']}")
    if task["body"]:
        print(f"\n{task['body']}")
    if task["result"] is not None:
        print(f"\nResult:\n{task['result']}")
    print("\nEvents:")
    for e in conn.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id", (task["id"],)):
        text = "" if e["kind"] == "done" else e["text"]  # the result is printed above
        print(f"  {stamp(e['at'])}  {e['agent']:<14} {e['kind']:<9} {text}")


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def row_cells(task, state, t):
    """(who, level, time since the last event, claim time left) for one row of the board."""
    who = task["claimed_by"] if task["status"] == "running" else (task["assignee"] or "anyone")
    level = "commit" if task["commit_changes"] else "edit" if task["edit"] else "read"
    left = f"{ago(task['claim_expires'] - t)} left" if task["status"] == "running" and state != "expired" else ""
    return who, level, f"{ago(t - task['last_at'])} ago", left


STATE_ORDER = ["STALE", "running", "expired", "ready", "blocked", "done"]
FINISHED = "('done')"  # statuses that leave the open list


def board_view(conn, stale_min=15, show_all=False):
    """(now, [(task, state, row text without the title)], summary) for status.

    Shows open tasks and tasks finished in the last hour, or every task with show_all.
    """
    t = now()
    where = "" if show_all else f"WHERE status NOT IN {FINISHED} OR updated_at > :since"
    tasks = conn.execute(
        "SELECT t.*, (SELECT MAX(at) FROM events e WHERE e.task_id = t.id) AS last_at FROM tasks t "
        f"{where} ORDER BY status IN {FINISHED}, created_at, rowid", {"since": t - 3600}).fetchall()
    id_width = max((len(task["id"]) for task in tasks), default=0)  # old and new IDs differ in length
    counts, rows = {}, []
    for task in tasks:
        state = shown_status(task)
        if state == "running" and t - task["last_at"] > stale_min * 60:
            state = "STALE"  # holds a claim but has said nothing for a while
        counts[state] = counts.get(state, 0) + 1
        who, level, last, left = row_cells(task, state, t)
        head = f"{task['id']:<{id_width}}  {state:<8} {who:<14} {level:<6} {last:<9} {left:<9} "
        rows.append((task, state, head))
    recent = "" if show_all else " in the last hour"
    summary = ", ".join(f"{counts[k]} {k.lower() if k != 'done' else k + recent}"
                        for k in STATE_ORDER if k in counts)
    return t, rows, summary or "board is empty"


def recent_events(conn, limit=5, width=100):
    lines = []
    for e in reversed(conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()):
        text = "" if e["kind"] == "done" else " ".join(e["text"].split())
        lines.append(f"  {time.strftime('%H:%M', time.localtime(e['at']))} {e['task_id']} {e['agent']} {e['kind']} {text}"[:width])
    return lines


def render_status(conn, stale_min=15, width=100, color=False):
    """The board as text: open tasks, tasks done in the last hour, resting agents, latest events."""
    paint = {"running": "36", "ready": "0", "blocked": "31", "expired": "33", "STALE": "33;1", "done": "32"}

    def colored(text, key):
        return f"\033[{paint.get(key, '0')}m{text}\033[0m" if color else text

    t, rows, summary = board_view(conn, stale_min)
    lines = [f"nextrunner  {time.strftime('%H:%M:%S', time.localtime(t))}  {summary}", ""]
    lines += [colored(head + task["title"][:max(10, width - len(head))], state) for task, state, head in rows]
    lines += [] if rows else ["no open tasks"]
    resting = conn.execute("SELECT * FROM agents WHERE down_until > ? ORDER BY name", (t,)).fetchall()
    if resting:
        lines += ["", "resting: " + "; ".join(
            f"{r['name']} for {ago(r['down_until'] - t)} ({r['reason'][:40]})" for r in resting)]
    events = recent_events(conn, 5, width)
    if events:
        lines += ["", "latest:"] + events
    if any(state == "STALE" for _, state, _ in rows):
        lines += ["", f"STALE = running, but no event for over {stale_min} min. Check it with: nextrunner show <id>"]
    return "\n".join(lines)


def agent_states(conn, agents, rows):
    running = Counter(task["claimed_by"] for task, state, _ in rows if state in ("running", "STALE"))
    parts = []
    for name, spec in agents.items():
        state = "up" if is_up(conn, name) else "resting"
        parts.append(f"{name} {state} {running[name]}/{spec.get('parallel', 1)}")
    return "agents: " + "   ".join(parts)

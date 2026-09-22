"""Plain-text views of the board, shared by list and show."""
import time

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

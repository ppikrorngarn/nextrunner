"""The nextrunner command line."""
import argparse
import sys

from . import __version__
from .board import ME, add, beat, claim, claim_next, done, note, release, reopen
from .db import connect, get
from .view import print_task, shown_status

DOC = """\
nextrunner: a shared task board for a team of AI agents.

State lives in one SQLite file, so no agent owns it and any agent can pick up
where another stopped.

Agents with a shell can work the board themselves:
    nextrunner next --as NAME         take the next task
    nextrunner note <id> --as NAME    leave a checkpoint
    nextrunner done <id> --as NAME --result "..."

Agent names are free text; the board does not care.
"""


def main(argv=None):
    p = argparse.ArgumentParser(prog="nextrunner", description=DOC, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"nextrunner {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name, help, task=False, who=False):
        sp = sub.add_parser(name, help=help)
        if task:
            sp.add_argument("id")
        if who:
            sp.add_argument("--as", dest="agent", default=ME, help=f"agent name (default: $NEXTRUNNER_AGENT or {ME})")
        return sp

    sp = cmd("add", "create a task")
    sp.add_argument("title")
    sp.add_argument("--body", default="", help="the brief: goal, folder, what is known, what done looks like")
    sp.add_argument("--to", metavar="AGENT", help="agent this task is for; others take it only if that agent fails")
    sp.add_argument("--strict", action="store_true", help="with --to: never reroute, wait for that agent")
    sp.add_argument("--cwd", help="folder the agent starts in")
    sp.add_argument("--by", default=ME)
    sp = cmd("list", "list tasks")
    sp.add_argument("--all", action="store_true", help="include done tasks")
    cmd("show", "show one task with its events", task=True)
    sp = cmd("claim", "take a task", task=True, who=True)
    sp.add_argument("--ttl", type=float, default=900, help="seconds before the claim expires (default 900)")
    sp.add_argument("--steal", action="store_true", help="take it even if it is for another agent")
    sp = cmd("next", "take the oldest free task for you", who=True)
    sp.add_argument("--ttl", type=float, default=900)
    sp = cmd("note", "leave a checkpoint or comment", task=True, who=True)
    sp.add_argument("text")
    token_help = "the token from claim or next: act only if that claim still holds"
    sp = cmd("beat", "extend your claim", task=True, who=True)
    sp.add_argument("--ttl", type=float, default=900)
    sp.add_argument("--token", help=token_help)
    sp = cmd("done", "finish a task you hold", task=True, who=True)
    sp.add_argument("--result", required=True)
    sp.add_argument("--token", help=token_help)
    sp = cmd("release", "hand back a task you hold", task=True, who=True)
    sp.add_argument("--reason", default="")
    sp.add_argument("--token", help=token_help)
    cmd("reopen", "put a blocked or done task back to ready", task=True)

    a = p.parse_args(argv)
    if a.cmd == "add":
        if a.strict and not a.to:
            p.error("--strict needs --to")
    conn = connect()

    def need(ok, message):
        if not ok:
            sys.exit(f"refused: {message}")

    if a.cmd == "add":
        print(add(conn, a.title, a.body, a.to, a.strict, a.cwd, a.by))
    elif a.cmd == "list":
        clauses, params = [], {}
        if not a.all:
            clauses.append("status != 'done'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        tasks = conn.execute(f"SELECT * FROM tasks {where} ORDER BY created_at, rowid", params).fetchall()
        for t in tasks:
            who = t["claimed_by"] if t["status"] == "running" else (t["assignee"] or "anyone")
            print(f"{t['id']}  {shown_status(t):<8} {who:<14} {t['title']}")
    elif a.cmd == "show":
        task = get(conn, a.id)
        need(task, f"no task {a.id}")
        print_task(conn, task)
    elif a.cmd == "claim":
        token = claim(conn, a.id, a.agent, a.ttl, a.steal)
        need(token, f"{a.id} is not free for {a.agent}")
        print(f"claimed {a.id} as {a.agent}, token {token}")
    elif a.cmd == "next":
        got = claim_next(conn, a.agent, a.ttl)
        need(got, f"no free task for {a.agent}")
        print_task(conn, get(conn, got[0]))
        print(f"\ntoken: {got[1]}")
    elif a.cmd == "note":
        need(get(conn, a.id), f"no task {a.id}")
        note(conn, a.id, a.agent, a.text)
    elif a.cmd == "beat":
        need(beat(conn, a.id, a.agent, a.ttl, a.token), f"{a.agent} does not hold {a.id}")
    elif a.cmd == "done":
        need(done(conn, a.id, a.agent, a.result, a.token), f"{a.agent} does not hold {a.id}")
    elif a.cmd == "release":
        need(release(conn, a.id, a.agent, a.reason, token=a.token), f"{a.agent} does not hold {a.id}")
    elif a.cmd == "reopen":
        need(reopen(conn, a.id), f"{a.id} is not blocked or done")

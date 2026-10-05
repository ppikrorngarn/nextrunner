"""The nextrunner command line."""
import argparse
import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

from . import __version__
from .agents import COOLDOWN_MIN, is_up, load_agents, set_down, set_up
import re

from .board import ME, add, approve, beat, claim, claim_next, done, fan_out, note, release, reopen, siblings
from .db import connect, get
from .dispatcher import dispatch
from .setup import doctor, init
from . import paths
from .view import FINISHED, parse_when, print_task, render_compare, render_status, shown_status, stamp

PLACEHOLDER = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")  # {LIKE_THIS} in a brief file, filled with --fill

DOC = """\
nextrunner: a shared task board for a team of AI agents.

State lives in one SQLite file, so no agent owns it and any agent can pick up
where another stopped. `nextrunner dispatch` is the only orchestrator and it is
plain code: it starts an agent for each ready task, records the reply word for
word, and reroutes when an agent fails or runs out of tokens.

Agents with a shell can also work the board themselves:
    nextrunner next --as NAME         take the next task
    nextrunner note <id> --as NAME    leave a checkpoint
    nextrunner done <id> --as NAME --result "..."

The dispatcher learns how to start each agent from agents.json (see
`nextrunner init` writes a template). Agent names are free text; the board does not care.

A task is read-only unless it was added with --edit. Each agent has one
command for read tasks and, if you trust it with changes, a second one for
edit tasks.
"""


def main(argv=None):
    p = argparse.ArgumentParser(prog="nextrunner", description=DOC, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"nextrunner {__version__}")
    p.add_argument("--home", metavar="DIR", help="keep the board, agents.json and log in DIR (same as $NEXTRUNNER_HOME)")
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
    body = sp.add_mutually_exclusive_group()
    body.add_argument("--body", default="", help="the brief: goal, folder, what is known, what done looks like")
    body.add_argument("--body-file", metavar="PATH", help="read the brief from a file, or from stdin with -")
    sp.add_argument("--fill", action="append", default=[], metavar="KEY=VALUE",
                    help="replace {KEY} in the brief with VALUE, or with a file's text when VALUE is @PATH; repeatable. "
                         "A {KEY} left unfilled is refused")
    sp.add_argument("--to", action="append", metavar="AGENT",
                    help="agent this task is for; others take it only if that agent fails. "
                         "Given twice or more: one strict task per agent with the same brief (see compare)")
    sp.add_argument("--strict", action="store_true", help="with --to: never reroute, wait for that agent")
    sp.add_argument("--cwd", help="folder the agent starts in")
    sp.add_argument("--edit", action="store_true", help="let the agent change files in that folder (default: read-only)")
    sp.add_argument("--commit", action="store_true",
                    help="implies --edit: when the agent finishes, the dispatcher commits the files it changed (never pushes)")
    sp.add_argument("--follow", metavar="ID",
                    help="continue task ID's conversation: same agent and folder, resuming its session if it has one")
    sp.add_argument("--hold", action="store_true",
                    help="nobody may take the task until a person runs `nextrunner ok <id>`; for work that acts outside the machine")
    sp.add_argument("--by", default=ME)
    sp = cmd("list", "list tasks")
    sp.add_argument("--all", action="store_true", help="include done tasks")
    sp.add_argument("--since", metavar="DATE", help="only tasks last changed on or after DATE (local; implies --all)")
    sp.add_argument("--until", metavar="DATE", help="only tasks last changed on or before DATE (a bare date means its whole day)")
    sp.add_argument("--json", action="store_true")
    sp = cmd("show", "show one task with its events", task=True)
    sp.add_argument("--json", action="store_true")
    sp = cmd("compare", "show every answer of a fan-out group (a task added with --to twice or more), one under another",
             task=True)
    sp.add_argument("--json", action="store_true")
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
    sp = cmd("ok", "approve a task added with --hold, so an agent may take it", task=True)
    sp.add_argument("--by", default=ME)
    sp = cmd("agents", "show which agents the dispatcher can start")
    sp.add_argument("--expanded", action="store_true", help="also print every command, with $lists filled in")
    sp = cmd("down", "mark an agent unavailable")
    sp.add_argument("name")
    sp.add_argument("--minutes", type=float, default=COOLDOWN_MIN)
    sp.add_argument("--reason", default="")
    sp = cmd("up", "mark an agent available again")
    sp.add_argument("name")
    sp = cmd("dispatch", "start agents for free tasks")
    sp.add_argument("--loop", type=float, metavar="SECONDS", help="keep running, one pass every SECONDS")
    sp.add_argument("--timeout", type=int, default=600,
                    help="seconds one agent run may take unless the agent sets its own \"timeout\" (default 600)")
    sp.add_argument("--jobs", type=int, default=1, metavar="N",
                    help="run up to N tasks at once; one agent runs one at a time unless it sets \"parallel\" (default 1)")
    sp.add_argument("--dry-run", action="store_true")
    sp = cmd("status", "print the board as text: open tasks, resting agents, latest events (for agents and scripts)")
    sp.add_argument("--stale", type=float, default=15, metavar="MINUTES",
                    help="flag a running task with no event for this long (default 15)")
    sp = cmd("init", "write a starter agents.json (a template, or your own agents with --agent)")
    sp.add_argument("--agent", action="append", default=[], metavar="NAME='COMMAND'",
                    help="describe one agent that answers on stdout, for example --agent mybot='mybot run'; repeatable")
    sp.add_argument("--force", action="store_true", help="replace an existing agents.json")
    cmd("doctor", "check agents.json: programs exist, placeholders and reply modes are valid")
    cmd("where", "show which files nextrunner is using, and why")

    a = p.parse_args(argv)
    if a.cmd == "dispatch" and a.jobs < 1:
        p.error("--jobs must be 1 or more")
    if a.cmd == "add":
        a.to = list(dict.fromkeys(a.to or []))
        if a.strict and not (a.to or a.follow):
            p.error("--strict needs --to or --follow")
        if len(a.to) > 1 and a.follow:
            p.error("--follow continues one agent's session, so it takes one --to")
        if (a.edit or a.commit) and not (a.cwd or a.follow):
            p.error("--edit and --commit need --cwd, the folder the agent may change")
    if a.cmd == "add" and a.body_file:
        try:
            a.body = sys.stdin.read() if a.body_file == "-" else Path(a.body_file).read_text(encoding="utf-8")
        except OSError as err:
            p.error(f"--body-file: {err}")
        a.body = a.body.strip()
        if not a.body:
            p.error("--body-file: the file is empty")
    if a.cmd == "add" and (a.fill or a.body_file):
        fills = {}
        for item in a.fill:
            key, sep, value = item.partition("=")
            if not sep or not PLACEHOLDER.fullmatch("{" + key + "}"):
                p.error(f"--fill wants KEY=VALUE with KEY like PR_URL, not {item!r}")
            if value.startswith("@"):
                try:
                    value = Path(value[1:]).read_text(encoding="utf-8").strip()
                except OSError as err:
                    p.error(f"--fill {key}: {err}")
            fills[key] = value
        a.body = PLACEHOLDER.sub(lambda m: fills.get(m.group(1), m.group(0)), a.body)
        left = sorted({m.group(1) for m in PLACEHOLDER.finditer(a.body)})
        if left:
            p.error("the brief still has " + ", ".join("{" + k + "}" for k in left) + "; fill each with --fill KEY=VALUE")
    if a.cmd == "list":
        try:
            a.since = parse_when(a.since) if a.since else None
            a.until = parse_when(a.until, end=True) if a.until else None
        except ValueError as err:
            p.error(str(err))
        if a.since and a.until and a.since >= a.until:
            p.error("--since must be before --until")
    if a.home:
        os.environ["NEXTRUNNER_HOME"] = a.home
    if a.cmd == "where":
        for label, path, why in paths.describe():
            print(f"{label:<7} {path}  ({why}){'' if path.exists() else '  [not created yet]'}")
        return
    if a.cmd == "init":
        print("\n".join(init(a.agent, a.force)))
        return
    if a.cmd == "doctor":
        lines, errors = doctor()
        print("\n".join(lines))
        sys.exit(1 if errors else 0)
    conn = connect()

    def need(ok, message):
        if not ok:
            sys.exit(f"refused: {message}")

    if a.cmd == "add":
        need(a.follow is None or get(conn, a.follow), f"no task {a.follow}")
        if len(a.to) > 1:
            ids = fan_out(conn, a.title, a.body, a.to, cwd=a.cwd, by=a.by, edit=a.edit, commit=a.commit, hold=a.hold)
            print("\n".join(ids))
        else:
            print(add(conn, a.title, a.body, a.to[0] if a.to else None, a.strict, a.cwd, a.by, a.edit, a.commit,
                      a.follow, a.hold))
    elif a.cmd == "list":
        clauses, params = [], {}
        if not (a.all or a.since or a.until):
            clauses.append(f"status NOT IN {FINISHED}")
        if a.since:
            clauses.append("updated_at >= :since"); params["since"] = a.since
        if a.until:
            clauses.append("updated_at < :until"); params["until"] = a.until
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        tasks = conn.execute(f"SELECT * FROM tasks {where} ORDER BY created_at, rowid", params).fetchall()
        if a.json:
            print(json.dumps([dict(t) | {"status": shown_status(t)} for t in tasks], indent=2))
        id_width = max((len(t["id"]) for t in tasks), default=0)
        for t in [] if a.json else tasks:
            who = t["claimed_by"] if t["status"] == "running" else (t["assignee"] or "anyone")
            level = "commit" if t["commit_changes"] else "edit" if t["edit"] else "read"
            print(f"{t['id']:<{id_width}}  {shown_status(t):<8} {who:<14} {level:<6} {t['title']}")
    elif a.cmd == "show":
        task = get(conn, a.id)
        need(task, f"no task {a.id}")
        if a.json:
            events = [dict(e) for e in conn.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id", (a.id,))]
            print(json.dumps(dict(task) | {"status": shown_status(task), "events": events}, indent=2))
        else:
            print_task(conn, task)
    elif a.cmd == "compare":
        tasks = siblings(conn, a.id)
        need(tasks, f"no task {a.id}")
        if a.json:
            print(json.dumps([dict(t) | {"status": shown_status(t)} for t in tasks], indent=2))
        else:
            print(render_compare(tasks, shutil.get_terminal_size((100, 24)).columns))
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
    elif a.cmd == "ok":
        need(get(conn, a.id), f"no task {a.id}")
        need(approve(conn, a.id, a.by), f"{a.id} is not held")
        print(f"approved {a.id}; the next dispatch may run it")
    elif a.cmd == "agents":
        for name, spec in load_agents().items():
            row = conn.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
            state = "up" if is_up(conn, name) else f"down until {stamp(row['down_until'])}  {row['reason']}"
            print(f"{name:<10} {'read+edit' if spec.get('cmd_edit') else 'read only':<10} "
                  f"parallel {spec.get('parallel', 1):<3} {state}")
            if a.expanded:
                for key in ("cmd", "cmd_edit", "cmd_resume", "cmd_edit_resume"):
                    if spec.get(key):
                        print(f"  {key}: {shlex.join(spec[key])}")
    elif a.cmd == "down":
        set_down(conn, a.name, a.minutes, a.reason)
    elif a.cmd == "up":
        set_up(conn, a.name)
    elif a.cmd == "status":
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        print(render_status(conn, a.stale, shutil.get_terminal_size((120, 24)).columns, color))
    elif a.cmd == "dispatch":
        while True:
            dispatch(conn, timeout=a.timeout, dry_run=a.dry_run, jobs=a.jobs)
            if not a.loop:
                break
            time.sleep(a.loop)

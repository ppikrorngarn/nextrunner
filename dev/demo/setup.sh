#!/bin/bash
# Make a fresh fake HOME with three demo agents. Usage: setup.sh SCENE  (cli | fanout | ui)
# The real board is never touched: HOME points at a throwaway folder (DEMO_HOME, default
# /private/tmp/nr-demo), so the platform default paths resolve there and the UI shows no
# sandbox chip.
set -euo pipefail
DEMO="$(cd "$(dirname "$0")" && pwd)"
export HOME="${DEMO_HOME:-/private/tmp/nr-demo}"
rm -rf "$HOME"
mkdir -p "$HOME/Library/Application Support/nextrunner" "$HOME/projects/app"
AGENT="$DEMO/demo_agent.py"
cat > "$HOME/Library/Application Support/nextrunner/agents.json" <<EOF
{
  "agent-a": {"cmd": ["python3", "$AGENT", "--name", "agent-a", "{prompt}"], "reply": "stdout", "parallel": 2},
  "agent-b": {"cmd": ["python3", "$AGENT", "--name", "agent-b", "{prompt}"], "reply": "stdout"},
  "agent-c": {"cmd": ["python3", "$AGENT", "--name", "agent-c", "{prompt}"], "reply": "stdout"}
}
EOF
export NEXTRUNNER_AGENT=you DEMO_DELAY=0.3
nr() { nextrunner "$@"; }

case "${1:-}" in
cli)
  touch "$HOME/limit-agent-a"   # agent-a is out of tokens: the dispatcher rests it and reroutes
  ;;
fanout)
  : # starts from an empty board
  ;;
ui)
  nr add "Summarise the open issues" --body "Group them by area and say where to start." >/dev/null
  nr add "Find why the nightly build is slow" --body "Look at the last ten runs." --to agent-c >/dev/null
  nr add "Review the change to the login form" --body "The diff is in the login-form branch." --to agent-b --strict >/dev/null
  nr add "Check the flaky upload test" --body "It failed twice this week." --to agent-c --strict >/dev/null
  nr dispatch --jobs 3 >/dev/null 2>&1 || true
  nr dispatch --jobs 3 >/dev/null 2>&1 || true
  nr dispatch --jobs 3 >/dev/null 2>&1 || true
  nr add "Is the cache key safe across users?" --body "Look at the cache middleware." --to agent-a --to agent-b >/dev/null
  nr dispatch --jobs 3 >/dev/null 2>&1 || true
  nr add "Post the weekly update to the team channel" --body "Draft it; I will read it before it goes out." --to agent-a --hold >/dev/null
  nr add "Draft release notes for v0.2" --body "From the merged changes since v0.1." >/dev/null
  ;;
*) echo "usage: setup.sh cli|fanout|ui" >&2; exit 2 ;;
esac

# Make the board look lived in: spread the tasks over the last hour, give each run a plausible
# length, and show log paths as ~ (the real HOME here is a long scratch path).
[ "$1" = ui ] && python3 - "$HOME" <<'PY'
import os, random, re, sqlite3, sys
home = sys.argv[1]
random.seed(7)
ago = {"Summarise": 48, "nightly": 41, "login": 33, "flaky": 27, "cache": 18, "weekly": 9, "release": 4}
db = sqlite3.connect(os.path.join(home, "Library/Application Support/nextrunner/board.db"))
for tid, title in db.execute("SELECT id, title FROM tasks").fetchall():
    base = db.execute("SELECT created_at FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    base -= next((v for k, v in ago.items() if k in title), 0) * 60
    t = base
    for rowid, kind, text in db.execute("SELECT rowid, kind, text FROM events WHERE task_id = ? ORDER BY rowid", (tid,)).fetchall():
        if kind == "claimed":
            t += random.randint(3, 9)
        elif kind == "run":
            secs = random.randint(6, 14) if "exit=1" in (text or "") else random.randint(38, 96)
            t += secs
            text = re.sub(r"^\d+s ", f"{secs}s ", text or "")
        for prefix in {home, os.path.realpath(home)}:
            text = (text or "").replace(prefix, "~") if text else text
        db.execute("UPDATE events SET at = ?, text = ? WHERE rowid = ?", (t, text, rowid))
    db.execute("UPDATE tasks SET created_at = ?, updated_at = ? WHERE id = ?", (base, t, tid))
db.commit()
PY
[ -f "$HOME/Library/Application Support/nextrunner/board.db" ] && nr list --all || true

# Source this to work on nextrunner without touching your real board, agents or log:
#
#     . dev/env.sh
#     nextrunner where                      # shows every file now points into .sandbox/
#     nextrunner add "try it" --to agent-a && nextrunner dispatch && nextrunner list --all
#
# It sets NEXTRUNNER_HOME to .sandbox/ in this repo (git ignores it) and writes an agents.json whose agents
# are dev/fake_agent.py, so dispatching costs nothing. Delete .sandbox/ to start over.
# Needs `nextrunner` on the PATH: `uv run nextrunner ...` works without installing, or run `uv tool install -e .`.
_dev_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export NEXTRUNNER_HOME="$(dirname "$_dev_dir")/.sandbox"
mkdir -p "$NEXTRUNNER_HOME"
if [ ! -f "$NEXTRUNNER_HOME/agents.json" ]; then
  python3 - "$_dev_dir/fake_agent.py" "$NEXTRUNNER_HOME/agents.json" <<'PY'
import json, sys
fake, out = sys.argv[1:]
def spec(name, parallel=1):
    cmd = [sys.executable, fake, "--name", name, "{prompt}"]
    return {"cmd": cmd, "cmd_edit": cmd[:2] + ["--edit"] + cmd[2:], "reply": "stdout", "parallel": parallel}
json.dump({"agent-a": spec("agent-a", 2), "agent-b": spec("agent-b")}, open(out, "w"), indent=2)
PY
fi
unset _dev_dir
echo "nextrunner sandbox: NEXTRUNNER_HOME=$NEXTRUNNER_HOME"

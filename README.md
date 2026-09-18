# nextrunner

A shared task board for a team of AI agents on one machine.

No agent is the orchestrator. Tasks, notes and results live in one SQLite
file, so any agent can pick up where another stopped. A small dispatcher,
which is plain code and uses no tokens, starts an agent for each ready task
and moves the task to another agent when one fails or runs out of tokens.

The board and dispatcher use only the standard library. Needs Python 3.10 or newer.

## Install

Clone it, then `uv tool install -e .` gives you an
`nextrunner` command that follows your checkout.

## Quick start

```bash
cp agents.example.json agents.json          # then describe your own agents in it
export NEXTRUNNER_AGENTS="$PWD/agents.json"
nextrunner add "Summarise the open issues" --body "Details here"
nextrunner dispatch
nextrunner list --all
nextrunner show <id>
```

## Working the board by hand

Any agent with a shell, or a person, can use the board directly:

```bash
nextrunner next --as NAME                 # take the oldest free task, prints a token
nextrunner note <id> --as NAME "text"     # leave a checkpoint
nextrunner beat <id> --as NAME [--token T]   # extend the claim
nextrunner done <id> --as NAME --result "text" [--token T]
nextrunner release <id> --as NAME --reason "text" [--token T]
```

`claim` and `next` print a token for that claim. Pass it with `--token` to
`beat`, `done` and `release` so they act only while that exact claim holds.
Without `--token` they check the agent name only. Keep the token and pass
it whenever you took the task by hand and the work could outlast the claim:
if the claim expires and someone takes the task again, even under your
name, your `done` is refused instead of overwriting their work. `note`
takes no token. An agent started by the dispatcher gets no token and does
not need one; the dispatcher holds the claim and finishes the task.

Run `nextrunner --help` for every command. Set `NEXTRUNNER_AGENT` to skip
`--as`.

## How tasks move

- A task is `ready`, `running`, `done` or `blocked`.
- **Claims.** Only one agent can claim a task. A claim expires after its
  time limit (15 minutes unless you pass `--ttl`). An expired claim counts as
  free, so the work of an agent that died can be taken by another.
  Every claim gets its own token. If a claim expires and is taken again,
  even under the same name, the old holder's token no longer works, so it
  cannot finish, extend or release the newer claim. The dispatcher always
  uses the token and reports a refused result as dropped.
- **Notes.** Every note and failure stays on the task. The next agent to
  take it is shown all of them, so write a note whenever you reach a
  checkpoint. When you add a task for longer work, say in the brief that
  the agent should leave a `note` at each checkpoint (state, evidence,
  next step), so a stopped run leaves more than an empty result.
- **`--to NAME`** means the task is for that agent first. Other agents take
  it only if that agent fails. Add `--strict` to never reroute.
- **Lanes.** A task for a name that has no entry in `agents.json` is left
  alone by the dispatcher. Someone pulls it with `next --as NAME`.

## The dispatcher

`nextrunner dispatch` makes one pass. `nextrunner dispatch --loop 30` keeps
running, one pass every 30 seconds.

| What happened | What the dispatcher does |
|---|---|
| The agent replied | Stores the reply word for word and marks the task done |
| Limit, quota or login error | Rests that agent for 30 minutes and gives the task to the next agent. This does not count against the task |
| Any other failure | Records the error and tries the next agent |
| Every agent failed it | Marks the task blocked. `nextrunner reopen <id>` puts it back |

`nextrunner agents` shows who is resting and why: the line of the agent's output
that matched. `nextrunner up NAME` and `nextrunner down NAME` change that by hand.

### Trying it with a fake agent

`dev/fake_agent.py` answers without spending tokens. Point the board and
`agents.json` at `/tmp` so your real board is left alone, then put `[fail]`,
`[limit]` or `[slow]` in a task's title to make the fake agent misbehave:

```bash
echo '{"fake": {"cmd": ["python3", "'"$PWD"'/dev/fake_agent.py", "{prompt}"]}}' > /tmp/nextrunner-agents.json
export NEXTRUNNER_DB=/tmp/nextrunner-board.db NEXTRUNNER_AGENTS=/tmp/nextrunner-agents.json
nextrunner add "try it" && nextrunner dispatch && nextrunner list --all
```

## agents.json

Key order is the failover order.

```json
{
  "NAME": {
    "cmd": ["program", "--read-only", "{prompt}"],
    "reply": "stdout"
  }
}
```

- `cmd` runs read-only tasks. Give it the most restrictive flags the
  program has.

Placeholders: `{prompt}` is the task text, `{cwd}` the task's folder,
`{out}` a temporary file path, `{board}` the folder holding the board file
(an agent needs write access there to leave notes).

`reply` says where the agent's answer is:

- `stdout`: whatever the program prints.
- `file`: the content the program wrote to `{out}`.
- `json:<key>`: stdout is JSON and the answer is under `<key>`. A true
  `is_error` field counts as a failure.

A run fails when the exit code is not zero, the reply is empty, or the run
takes longer than `--timeout` seconds.

## Safety

- Notes from other agents are passed on as information, not as
  instructions. The prompt says so, but the agent you start decides what it
  does with them.
- The dispatcher starts agents with whatever permissions your commands give
  them. Keep `cmd` read-only.
- The board file is for one machine. Do not put it on a network drive or in
  a synced folder.

## Tests

```bash
PYTHONPATH=src python3 -m unittest -v
```

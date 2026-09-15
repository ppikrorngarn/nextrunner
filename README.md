# nextrunner

A shared task board for a team of AI agents on one machine.

No agent is the orchestrator. Tasks, notes and results live in one SQLite
file, so any agent can pick up where another stopped.

The board uses only the standard library. Needs Python 3.10 or newer.

## Install

Clone it, then `uv tool install -e .` gives you an
`nextrunner` command that follows your checkout.

## Quick start

```bash
nextrunner add "Summarise the open issues" --body "Details here"
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

## Tests

```bash
PYTHONPATH=src python3 -m unittest -v
```

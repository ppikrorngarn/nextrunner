# nextrunner

A shared task board for a team of AI agents on one machine.

No agent is the orchestrator. Tasks, notes and results live in one SQLite
file, so any agent can pick up where another stopped. A small dispatcher,
which is plain code and uses no tokens, starts an agent for each ready task
and moves the task to another agent when one fails or runs out of tokens.

The board and dispatcher use only the standard library. The full-screen UI uses [Textual](https://textual.textualize.io/). Needs Python 3.10 or newer, or use the single-file app from a Release, which needs nothing.

## Install

Clone it, then `uv tool install -e .` gives you an
`nextrunner` command that follows your checkout. Then `nextrunner init`
writes a starter `agents.json` where nextrunner looks for it.

Without Python: download the file for your machine from the latest
[Release](../../releases) (`nextrunner-macos-arm64`, `nextrunner-macos-x64`,
`nextrunner-linux-x64`, `nextrunner-linux-arm64`; Windows is untested), make it
executable, and put it on your PATH. On macOS the first run is blocked
because the file is not signed; allow it once with:

```bash
xattr -d com.apple.quarantine ./nextrunner-macos-arm64
```

## Quick start

```bash
nextrunner where                           # which files it uses; see "Where the files live"
nextrunner init                            # writes a starter agents.json; edit it (or: nextrunner init --agent mybot='mybot run')
nextrunner doctor                          # checks that every agent's program exists and the settings make sense
nextrunner add "Summarise the open issues" --body "Details here"
nextrunner add "Review the change" --body-file brief.md      # a long brief from a file (or - for stdin)
nextrunner add "Review the change" --body-file brief.md --to agent-a --to agent-b   # a second opinion: one task per agent
nextrunner compare <id>                                       # their answers, one under another
nextrunner dispatch
nextrunner list --all
nextrunner list --since 2026-10-06 --until 2026-10-06   # what changed that day, done tasks included
nextrunner status               # the board as text
nextrunner ui                   # the board full screen
```

`status` prints open tasks and tasks finished in
the last hour, how long ago each one last had an event, how much claim time
is left, which agents are resting, and the five latest events. A running task
with no event for 15 minutes is marked `STALE` (change it with `--stale
MINUTES`). It is plain text, so agents and scripts can read it; use `nextrunner ui` to watch the
board live.

`nextrunner ui` is the same board full screen, and you can act on it. The task list is on the
left and the selected task on the right: who holds it and for how long, its brief or its
result (rendered as Markdown, tables included), and its events with colour by kind. In a
terminal narrower than 110 columns the detail pane moves under the list.

| Key | Does |
|---|---|
| ↑ ↓ (or j k) | Move between tasks |
| Enter | Open the task full screen, in the same colours: the whole brief and result as Markdown and every event, kept up to date while it runs; Esc or ← goes back, q quits |
| / | Filter by title, agent, id or state. Enter keeps the filter, Esc clears it |
| a | Add a task in a form: title, agent, strict, level (read, edit, commit), folder, brief; Ctrl+S adds, Esc cancels |
| f | Add a follow-up to the selected task (same agent and folder, resumes its session) |
| o | Reopen the selected task |
| d | Start a dispatcher pass in the background (asks for `--jobs`); output goes to the dispatch log |
| p | Pause an agent for some minutes, or resume it (a form with Pause and Resume buttons) |
| t | Switch between open tasks plus the last hour, and every task |
| Ctrl+P | Command palette, including themes. The theme and the `t` choice are remembered in `ui.json` next to `agents.json` |
| q | Quit. A dispatcher started from the UI keeps running |

The top shows a count chip for each state (● running, ◐ stale, ◌ expired, ○ ready, ✗ blocked,
✓ done) and each agent as up with its runs against its `parallel` limit, or resting with the
time left. A toast appears when a task finishes, blocks or goes stale. A yellow `sandbox` chip
shows when `NEXTRUNNER_HOME` or another file override is active, so you know it is not your real board.

## Where the files live

`nextrunner where` prints them. Each is found the first way that applies: its own variable
(`NEXTRUNNER_DB`, `NEXTRUNNER_AGENTS`, `NEXTRUNNER_LOG`), then `NEXTRUNNER_HOME` (one folder for all three, or
`nextrunner --home DIR ...`), then the platform default:

| | `agents.json` | `board.db` | `dispatch.log` |
|---|---|---|---|
| macOS | `~/Library/Application Support/nextrunner` | same | `~/Library/Logs/nextrunner` |
| Linux | `$XDG_CONFIG_HOME/nextrunner` (`~/.config`) | `$XDG_DATA_HOME/nextrunner` (`~/.local/share`) | `$XDG_STATE_HOME/nextrunner` (`~/.local/state`) |
| Windows | `%APPDATA%\nextrunner` | `%LOCALAPPDATA%\nextrunner` | same as the board |

## Trying things out without touching your real board

```bash
. dev/env.sh                     # NEXTRUNNER_HOME=.sandbox, with two fake agents that cost nothing
nextrunner add "try it" --to agent-a   # put [fail], [limit] or [slow] in a title to make the fake agent misbehave
nextrunner dispatch && nextrunner list --all
```

Delete `.sandbox/` to start over. The tests do the same on their own: `tests/__init__.py`
points `NEXTRUNNER_HOME` at a temporary folder before anything runs.

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

- A task is `ready`, `running`, `done`, `blocked` or `cancelled`.
- **Task IDs** look like `t_0e8d4ba639`: six base-36 digits of seconds
  since 2026 (so IDs sort by creation time) and four random ones. Two IDs
  can only match within the same second; `add` then draws again.
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
- **Fan-out.** `--to` given twice or more makes one strict task per agent,
  all with the same brief, grouped as a fan-out. `nextrunner compare <id>` prints
  every answer in the group one under another (`--json` for scripts). A
  fan-out cannot follow an earlier task, since a follow-up continues one
  agent's session.
- **Fill a brief file.** `--fill KEY=VALUE` replaces `{KEY}` in the brief
  (`@PATH` takes the value from a file). `add` refuses a brief that still
  has a `{PLACEHOLDER}`, so a template is never sent half filled.
- **Edit needs a folder.** `--edit` and `--commit` are refused without
  `--cwd` (or `--follow`, which inherits one): an agent that may change
  files must be told where.
- **Cancel.** `nextrunner cancel <id> [--reason TEXT]` takes a task off the board
  for good when it is ready (held or not), blocked, or its run died. A task
  someone is running is left alone: wait, or let its claim expire. Cancelled
  tasks leave `list` and the open board like done ones (`--all`, `--since`
  and `t` in the UI still show them), and `nextrunner reopen` brings one back. The
  first time a board made before this command is opened, its tasks table is
  rebuilt to allow the new status, and a copy named `board.db.before-cancel`
  is left next to it.
- **Held for approval.** `nextrunner add --hold` makes a task that nobody may
  take, not the dispatcher, not `next`, not `claim`, until a person runs
  `nextrunner ok <id>`. Use it when an agent
  drafts work that acts outside the machine, such as a message to post: the
  draft goes on the board held, you read it, and your `ok` is an `approved`
  event on the task with a fingerprint of the brief it covered. A held task
  shows as `held`. This guards against a draft being run before anyone
  looked at it; it is not a lock against an agent with a shell, which can
  run `nextrunner ok` too.
- **Lanes.** A task for a name that has no entry in `agents.json` is left
  alone by the dispatcher. Someone pulls it with `next --as NAME`.
- **Read or edit.** A task is read-only unless you add it with `--edit`.
  An edit task lets the agent change files in the task's folder (`--cwd`).
- **Commit.** Add a task with `--commit` (it implies `--edit`) and the
  dispatcher commits what the agent changed once it finishes, so the agent
  needs no write access to `.git`. See below.

## The dispatcher

`nextrunner dispatch` makes one pass. `nextrunner dispatch --loop 30` keeps
running, one pass every 30 seconds.

`nextrunner dispatch --jobs 3` runs up to 3 tasks at once (default 1, one at a
time). One agent never runs two tasks at the same moment: a task for a busy
agent waits for it, and a task for anyone goes to the next free agent. Claims,
tokens, failover and rests work the same as with one job. A pass ends when
every run it started has finished.

To let one agent run several tasks at once, give it `"parallel": N` in
`agents.json` (default 1). `--jobs` still caps the total. Several runs of one
agent share its usage limit, so they can run it out sooner, and edit tasks in
the same folder can get in each other's way.

For each free task it picks the agent the task is for, or else the first
available agent in `agents.json` order. Then:

| What happened | What the dispatcher does |
|---|---|
| The agent replied | Stores the reply word for word and marks the task done |
| Busy error (the model is at capacity, overloaded, HTTP 429) | Rests that agent for 2 minutes and gives the task to the next agent. A task with no one else to go to waits inside the pass and is retried when the rest ends, resuming the agent's session. Three busy errors from one agent in a pass count as a quota error. This does not count against the task |
| Quota, credits or login error | Rests that agent for 30 minutes and gives the task to the next agent. A task with no one else to go to is left for the next pass; the dispatcher says until when. This does not count against the task |
| Any other failure | Records the error and tries the next agent |
| Every agent failed it | Marks the task blocked. `nextrunner reopen <id>` puts it back |
| An edit task, and no agent has an edit command | Marks the task blocked |

`nextrunner agents` shows who is resting and why: the line of the agent's output
that matched. `nextrunner up NAME` and `nextrunner down NAME` change that by hand.

### Commits for `--commit` tasks

Before the run, the dispatcher notes which files in the folder's git
repository already have changes. After a successful run it commits only the
files that were clean before and changed during the run:

- The message is the reply's last `COMMIT: <summary>` line (the prompt asks
  for one), or else the task title. The body names the task and the agent.
- A file that already had changes before the run is left uncommitted and
  named on the board, because the agent's change cannot be told apart from
  what was already there.
- Anything you had staged stays staged and out of the commit.
- A failed run commits nothing. A folder outside a git repository is skipped.
- It never pushes. Git hooks run as usual; if one refuses, the board says so.

The outcome is a `commit` event on the task (`nextrunner show <id>`). Two
`--commit` tasks in the same repository at the same time (`--jobs` above 1)
can pick up each other's files, so run those one at a time.

### What a run printed

Every run the dispatcher starts leaves a `run` event on the task: how long
it took, the exit code, any usage figures, and the path of a trace file
holding everything the agent printed on both streams, with the command and
folder it was started with. Traces live in `runs/<task id>/<attempt>-<agent>.log`
next to the board (`nextrunner where` shows the folder). `nextrunner show <id>` prints
the event; open the file when a reply looks thin or a run failed for no
clear reason. Nothing deletes them; remove the `runs` folder when you like.

Usage figures come from an optional `usage` key on the agent: an object of
label to regular expression, each with one group, matched against the
run's output. For an agent that reports cost and turns as JSON:

```json
"usage": {"cost": "\"total_cost_usd\":\\s*([0-9.]+)", "turns": "\"num_turns\":\\s*(\\d+)"}
```

gives `run  41s exit=0 cost=0.0312 turns=7 log=...`. `nextrunner doctor` checks
the patterns. Figures are whatever the agent reports; nextrunner adds nothing up.

### Hooks: hear when a task ends

Add `"$hooks"` to `agents.json` and the dispatcher runs a command of yours
after a task is `done`, `limited`, `failed` or `blocked`, once the board is
written. `{id}`, `{title}`, `{agent}`, `{kind}` and `{text}` (the result,
reason or error, cut to 500 characters) are filled in. A macOS notification
for finished tasks and a spoken line when an agent rests:

```json
{
  "$hooks": {
    "done": ["osascript", "-e", "display notification \"{title}\" with title \"nextrunner: {agent} done\""],
    "limited": ["say", "{agent} is resting"]
  },
  "NAME": { "cmd": ["..."] }
}
```

Hooks are for telling a person. Output is ignored, a hook has 15 seconds,
and one that fails is noted on the task as a `hook` event and never counts
against the task. `nextrunner doctor` checks the programs and placeholders.
Nothing runs unless you add the key.

## Running the dispatcher all the time (macOS)

`examples/nextrunner.dispatch.plist` is a LaunchAgent template that runs
`nextrunner dispatch --loop 30` at login and restarts it if it exits. Nothing
installs it for you.

Install:

```bash
cp examples/nextrunner.dispatch.plist ~/Library/LaunchAgents/local.nextrunner.dispatch.plist
# edit every /PATH/TO/... in the copy, including PATH for your agent programs
plutil -lint ~/Library/LaunchAgents/local.nextrunner.dispatch.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.nextrunner.dispatch.plist
launchctl print gui/$(id -u)/local.nextrunner.dispatch | grep -E 'state|pid'
```

Stop and remove:

```bash
launchctl bootout gui/$(id -u)/local.nextrunner.dispatch
rm ~/Library/LaunchAgents/local.nextrunner.dispatch.plist
```

`bootout` stops it until you bootstrap it again. Because of `KeepAlive`,
killing the process alone only makes launchd start a new one.

Log: the template sends the dispatcher's output and errors to `dispatch.log`
(`tail -f` it; `nextrunner where` shows the folder the UI uses). The file grows forever;
empty it with `: > dispatch.log` now and then.

Sleep: nothing runs while the Mac sleeps. The dispatcher and any agent it
started are frozen and carry on after wake. A claim's time limit keeps
counting during sleep, so a task that was running may show as expired and
free after a long sleep. The board then treats it like any other expired
claim, and the old run cannot finish it if a newer claim exists. The run
`--timeout` uses a clock that stops during sleep on macOS, so sleep does not
count against it. To keep tasks moving, keep the Mac awake (for
example `caffeinate -s` on power) or accept that work waits until wake.
The template runs only while you are logged in.

## agents.json

Key order is the failover order.

```json
{
  "NAME": {
    "cmd": ["program", "--read-only", "{prompt}"],
    "cmd_edit": ["program", "--allow-edits", "{prompt}"],
    "reply": "stdout"
  }
}
```

- `cmd` runs read-only tasks. Give it the most restrictive flags the
  program has.
- `cmd_edit` runs tasks added with `--edit`. Leave it out and that agent
  never gets an edit task.

The board only chooses which command to run. The limits themselves come
from the flags you put in each command, so check what your programs can
enforce.

A long set of flags that several commands share, such as the tools an agent
may use, is written once as a named list and used by name:

```json
{
  "$lists": {
    "safe-tools": ["--allowedTools", "Read", "Grep", "Glob"]
  },
  "NAME": {
    "cmd": ["program", "@safe-tools", "{prompt}"],
    "cmd_edit": ["program", "@safe-tools", "--allow-edits", "{prompt}"]
  }
}
```

`@safe-tools` stands for the list's words, in place. A list may not use
another list. `nextrunner doctor` names a `@word` that has no list, and
`nextrunner agents --expanded` prints every command with the lists filled in.

Placeholders: `{prompt}` is the task text, `{cwd}` the task's folder,
`{out}` a temporary file path, `{board}` the folder holding the board file
(an agent needs write access there to leave notes).

`reply` says where the agent's answer is:

- `stdout`: whatever the program prints.
- `file`: the content the program wrote to `{out}`.
- `json:<key>`: stdout is JSON and the answer is under `<key>`. A true
  `is_error` field counts as a failure.

A run fails when the exit code is not zero, the reply is empty, or the run
takes longer than the agent's `timeout`, or else `--timeout`, seconds.

Optional keys:

- `parallel`: how many tasks this agent may run at once (default 1).
- `usage`: `{label: pattern}`; see "What a run printed".
- `timeout`: seconds one run of this agent may take. It replaces
  `dispatch --timeout` for this agent, and the claim lasts a minute longer
  than it. Give a slow agent its own, for example `"timeout": 1800`,
  instead of raising the default for everyone.
- `session`: a regular expression whose first group is the agent's session
  or conversation ID in its output (stdout, then stderr). With `reply:
  stdout`, the reply is the text after the line where the ID was found.
- `cmd_resume` and `cmd_edit_resume`: the commands that continue a saved
  session, with `{session}` as its ID. Give them the same limits as `cmd`
  and `cmd_edit`, and check that the program keeps those limits when it
  resumes; some restore the old session's settings unless told otherwise.

## Sessions and follow-ups

For an agent with a `session` pattern, the dispatcher saves the session ID
after each run as a `session` event on the task. The next run continues it
instead of starting fresh when:

- the task goes back to the same agent, for example after a failure; or
- the task was added with `--follow ID`, which also takes the earlier
  task's agent and folder unless you give `--to` or `--cwd`:

```bash
nextrunner add "Now add tests for it" --follow t_0e8ea7858s --edit
```

If the agent has no resume command for that level, or the follow-up goes to
another agent, the run starts fresh and the prompt includes the earlier
task's result instead. Any command placeholder is filled with
`str.format`, so a literal `{` or `}` in a command must be doubled.

`agents.json` lives outside the repo, because it describes your own setup.

## Building the single-file app

```bash
uv run --with pyinstaller python packaging/build.py --name nextrunner-macos-arm64
```

That makes `dist/nextrunner-macos-arm64` (about 16 MB, no Python needed to run it) and a `.sha256`,
then runs it the way a user would: version, add, dispatch with the fake dev agent.
PyInstaller cannot cross-compile, so one build per OS and CPU.
`.github/workflows/release.yml` builds macOS arm64 and x64, Linux x64 and arm64, and Windows
(experimental, untested) on a `v*` tag and attaches them to a GitHub Release.

## Safety

- Notes from other agents are passed on as information, not as
  instructions. The prompt says so, but the agent you start decides what it
  does with them.
- The dispatcher starts agents with whatever permissions your commands give
  them. Keep `cmd` read-only and add `cmd_edit` only for agents you trust
  with changes.
- `--hold` guards against a task running before a person looked at it. Any
  agent with a shell can run `nextrunner ok`, so it is a record of who approved
  what, not a lock.
- Read and edit levels guard against accidents. They are not a security
  boundary: every agent runs as you, and any agent with a shell can add an
  `--edit` task.
- The board file is for one machine. Do not put it on a network drive or in
  a synced folder.

## Tests

```bash
PYTHONPATH=src python3 -m unittest -v   # or: uv run python -m unittest -v
```

`.github/workflows/test.yml` runs them on Linux and macOS, on Python 3.10 and
3.13, for every push and pull request.

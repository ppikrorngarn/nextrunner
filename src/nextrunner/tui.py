"""`nextrunner ui`: the board full screen, with keys to act on it (Textual).

The dispatcher it starts is a separate process that keeps going if the UI is
closed; its output goes to the dispatch log (`nextrunner where` shows the path).
"""
import os
import subprocess
import time
from collections import Counter

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Footer, Input, Label, Markdown, Select, Static

from . import paths
from .agents import is_up, load_agents, set_down, set_up
from .board import reopen
from .db import get
from .dispatcher import self_command
from .forms import add_from_answers
from .view import agent_status, ago, board_view, row_cells, stamp

ICONS = {"running": "●", "STALE": "◐", "expired": "◌", "ready": "○", "held": "◇", "blocked": "✗", "done": "✓",
         "cancelled": "–"}
STATE_STYLE = {"running": "cyan", "blocked": "red", "expired": "yellow", "STALE": "bold yellow", "done": "green",
               "ready": "", "held": "magenta", "cancelled": "dim"}
KIND_STYLE = {"created": "dim", "claimed": "cyan", "note": "yellow", "done": "green", "failed": "red",
              "limited": "yellow", "blocked": "red", "commit": "magenta", "reopened": "cyan", "released": "dim",
              "beat": "dim", "session": "dim", "approved": "green", "fan-out": "dim", "cancelled": "dim", "hook": "red",
              "run": "dim"}
LEVELS = [("read only", "r"), ("edit files in the folder", "e"), ("edit, and the dispatcher commits", "c")]


class AddScreen(ModalScreen):
    """Form for a new task, or for a follow-up when `follows` is set. Dismisses with an answers dict, or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel"), Binding("ctrl+s", "submit", "Add")]
    DEFAULT_CSS = """
    AddScreen { align: center middle; background: $background 60%; }
    #form { width: 80; height: auto; max-height: 90%; border: round $accent; background: $surface; padding: 1 2; }
    #form .heading { text-style: bold; color: $accent; }
    #form Label { margin-top: 1; color: $text-muted; }
    #buttons { margin-top: 1; height: auto; }
    #buttons Button { margin-right: 2; }
    """

    def __init__(self, agents, follows=None):
        super().__init__()
        self.agents, self.follows = agents, follows

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="form"):
            yield Label(f"Follow-up to {self.follows}" if self.follows else "New task", classes="heading")
            yield Label("Title")
            yield Input(id="title", placeholder="what should the agent do?", compact=True)
            if not self.follows:
                yield Label("Agent")
                yield Select([("anyone", "")] + [(name, name) for name in self.agents], value="", allow_blank=False,
                             compact=True, id="to")
            yield Checkbox("Only that agent (never reroute)" if not self.follows else "Only the same agent",
                           value=bool(self.follows), compact=True, id="strict")
            yield Label("May")
            yield Select(LEVELS, value="r", allow_blank=False, compact=True, id="level")
            if not self.follows:
                yield Label("Folder")
                yield Input(os.getcwd(), compact=True, id="cwd")
            yield Label("Brief")
            yield Input(id="body", placeholder="details, what done looks like", compact=True)
            with Horizontal(id="buttons"):
                yield Button("Add (ctrl+s)", variant="primary", compact=True, id="add")
                yield Button("Cancel (esc)", compact=True, id="cancel")

    def on_mount(self):
        self.query_one("#title").focus()

    def action_submit(self):
        def value(name, default=""):
            found = self.query(f"#{name}")
            return found.first().value if found else default
        self.dismiss({"title": value("title"), "to": value("to") or "", "strict": "y" if value("strict", False) else "n",
                      "level": value("level", "r"), "cwd": value("cwd"), "body": value("body")})

    def on_button_pressed(self, event):
        self.dismiss(None) if event.button.id == "cancel" else self.action_submit()

    def on_input_submitted(self, event):
        if event.input.id == "title" and event.value.strip():
            self.action_submit()


class AskScreen(ModalScreen):
    """One question with one answer box. Dismisses with the text, or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
    DEFAULT_CSS = """
    AskScreen { align: center middle; background: $background 60%; }
    #ask { width: 60; height: auto; border: round $accent; background: $surface; padding: 1 2; }
    """

    def __init__(self, question, default=""):
        super().__init__()
        self.question, self.default = question, default

    def compose(self) -> ComposeResult:
        with Vertical(id="ask"):
            yield Label(self.question)
            yield Input(self.default, id="answer")

    def on_mount(self):
        self.query_one("#answer").focus()

    def on_input_submitted(self, event):
        self.dismiss(event.value.strip())


class PauseScreen(ModalScreen):
    """Pause or resume one agent. Dismisses with (name, minutes or None to resume), or None."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]
    DEFAULT_CSS = """
    PauseScreen { align: center middle; background: $background 60%; }
    #pause { width: 60; height: auto; border: round $accent; background: $surface; padding: 1 2; }
    #pause Label { margin-top: 1; color: $text-muted; }
    #pause Horizontal { margin-top: 1; height: auto; }
    #pause Button { margin-right: 2; }
    """

    def __init__(self, conn, agents):
        super().__init__()
        self.conn, self.agents = conn, agents

    def compose(self) -> ComposeResult:
        options = [(f"{name} ({'up' if is_up(self.conn, name) else 'resting'})", name) for name in self.agents]
        with Vertical(id="pause"):
            yield Label("Agent")
            yield Select(options, allow_blank=False, id="agent")
            yield Label("Pause for how many minutes")
            yield Input("60", id="minutes")
            with Horizontal():
                yield Button("Pause", variant="warning", id="pause-btn")
                yield Button("Resume", variant="success", id="resume-btn")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event):
        name = self.query_one("#agent").value
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "resume-btn":
            self.dismiss((name, None))
        else:
            raw = self.query_one("#minutes").value.strip()
            self.dismiss((name, float(raw) if raw.replace(".", "", 1).isdigit() else 60.0))


class TaskDetail(VerticalScroll):
    """One task in colour: title, who holds it, brief and result as Markdown, and its events.

    Used beside the list (the latest events) and full screen (everything).
    """

    DEFAULT_CSS = """
    TaskDetail #d-head { text-style: bold; height: auto; }
    TaskDetail #d-meta { height: auto; color: $text-muted; margin-bottom: 1; }
    TaskDetail .label { height: auto; text-style: bold; color: $accent; }
    TaskDetail Markdown { height: auto; margin: 0; padding: 0; background: transparent; }
    TaskDetail #d-events-label { margin-top: 1; }
    TaskDetail #d-events { height: auto; }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="d-head")
        yield Static(id="d-meta")
        yield Static(id="d-label", classes="label")
        yield Markdown(id="d-body")
        yield Static(id="d-label2", classes="label")
        yield Markdown(id="d-body2")
        yield Static(id="d-events-label", classes="label")
        yield Static(id="d-events")

    def set_markdown(self, name, text):
        """Show Markdown text, or hide the section when there is none. Skips a render that would change nothing."""
        widget = self.query_one(f"#{name}", Markdown)
        widget.display = bool(text)
        if getattr(self, "_rendered", {}).get(name) == text:
            return
        widget.update(text or "")
        self.__dict__.setdefault("_rendered", {})[name] = text

    def show_message(self, head, body=""):
        """Nothing to show: a note in the title, and optionally some Markdown under it."""
        self.query_one("#d-head").update(Text(head, style="dim"))
        for name in ("d-meta", "d-label", "d-label2", "d-events-label", "d-events"):
            self.query_one(f"#{name}").update("")
        self.set_markdown("d-body", body)
        self.set_markdown("d-body2", "")

    def show(self, conn, task_id, full=False):
        """Fill the pane. full=True shows the brief and the result both, and every event."""
        task = get(conn, task_id)
        self.query_one("#d-head").update(Text(task["title"]))
        parts = [task["id"], f"for {task['assignee'] or 'anyone'}{' (strict)' if task['strict'] else ''}",
                 "commit" if task["commit_changes"] else "edit" if task["edit"] else "read-only"]
        if task["status"] == "running":
            remaining = task["claim_expires"] - time.time()
            parts.append(f"held by {task['claimed_by']}, " + (f"{ago(remaining)} left" if remaining > 0 else "claim expired"))
        if task["attempts"]:
            parts.append(f"{task['attempts']} failed attempt{'s' * (task['attempts'] != 1)}")
        if task["follows"]:
            parts.append(f"follows {task['follows']}")
        if task["fanout"]:
            parts.append(f"fan-out {task['fanout']}")
        lines = ["  ·  ".join(parts)]
        if task["cwd"]:
            lines.append(task["cwd"])
        if full:
            lines.append(f"created {stamp(task['created_at'])} by {task['created_by']}")
        self.query_one("#d-meta").update(Text("\n".join(lines)))
        if full:
            sections = [("Brief", task["body"])] if task["body"] else []
            sections += [("Result", task["result"])] if task["result"] is not None else []
        else:
            sections = [("Result", task["result"])] if task["result"] is not None else \
                [("Brief", task["body"])] if task["body"] else []
        sections += [("", "")] * (2 - len(sections))
        for (label, text), suffix in zip(sections, ("", "2")):
            self.query_one(f"#d-label{suffix}").update(Text(label))
            self.set_markdown(f"d-body{suffix}", text)
        self.query_one("#d-events-label").update(Text("Events"))
        self.query_one("#d-events").update(events_table(conn, task["id"], None if full else 12, None if full else 300))


class TextScreen(ModalScreen):
    """One task full screen: the same colours as the pane beside the list, with the whole brief, result and history."""

    BINDINGS = [Binding("escape,left", "dismiss(None)", "Back"),
                Binding("q", "app.quit", "Quit")]  # a modal hides the app's own keys, so q is bound here too
    DEFAULT_CSS = """
    TextScreen { align: center middle; background: $background 60%; }
    #text { width: 90%; height: 90%; border: round $accent; background: $surface; padding: 1 2; }
    """

    def __init__(self, conn, task_id):
        super().__init__()
        self.conn, self.task_id = conn, task_id

    def compose(self) -> ComposeResult:
        yield TaskDetail(id="text")

    def on_mount(self):
        self.query_one("#text").border_title = f"{self.task_id}   esc back · q quit"
        self.query_one(TaskDetail).show(self.conn, self.task_id, full=True)


CHIP_STYLE = {"running": "black on cyan", "STALE": "black on yellow", "expired": "black on yellow",
              "ready": "black on white", "held": "white on magenta", "blocked": "bold white on red", "done": "black on green",
              "cancelled": "dim"}


def chip(label, kind):
    """A coloured label, for the counts at the top."""
    return Text(f" {label} ", style=CHIP_STYLE.get(kind, "reverse"))


def counts_line(rows, show_all):
    """Chips for how many tasks are in each state."""
    counts = Counter(state for _, state, _ in rows)
    line = Text()
    for state in ("running", "STALE", "expired", "ready", "held", "blocked", "done", "cancelled"):
        if counts[state]:
            recent = "" if show_all or state not in ("done", "cancelled") else " 1h"
            label = f"{ICONS[state]} {counts[state]} {state.lower()}{recent}"
            line.append_text(chip(label, state))
            line.append("  ")
    return line if line.plain else Text("board is empty", style="dim")


def agents_line(status):
    """One chip per agent: up with its runs against its limit, or resting with the time left."""
    line = Text("agents  ", style="dim")
    for a in status:
        if a["up"]:
            line.append(f"● {a['name']} ", style="green")
            line.append(f"{a['running']}/{a['parallel']}   ", style="dim")
        else:
            line.append(f"◌ {a['name']} resting {a['left']}   ", style="yellow")
    return line if status else Text("agents  none: agents.json is missing (run nextrunner init)", style="dim")


def events_table(conn, task_id, limit=12, cut=300):
    """The events of one task as a table, newest last. limit=None shows all of them, cut=None the whole text."""
    rows = conn.execute("SELECT * FROM events WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                        (task_id, -1 if limit is None else limit)).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM events WHERE task_id = ?", (task_id,)).fetchone()[0]
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(ratio=1, overflow="fold")
    if total > len(rows):
        table.add_row("", "", "", Text(f"… {total - len(rows)} earlier", style="dim"))
    for e in reversed(rows):
        body = "" if e["kind"] == "done" else " ".join(e["text"].split())[:cut]
        table.add_row(time.strftime("%H:%M:%S", time.localtime(e["at"])),
                      e["agent"], Text(e["kind"], style=KIND_STYLE.get(e["kind"], "")), body)
    return table


class BoardApp(App):
    TITLE = "nextrunner"
    BINDINGS = [
        Binding("a", "add", "Add"),
        Binding("f", "follow_up", "Follow-up"),
        Binding("o", "reopen", "Reopen"),
        Binding("d", "dispatch", "Dispatch"),
        Binding("p", "pause", "Pause/resume"),
        Binding("t", "toggle_all", "All/recent"),
        Binding("enter", "open", "Open"),
        Binding("q", "quit", "Quit"),
        Binding("j", "move(1)", show=False),
        Binding("k", "move(-1)", show=False),
    ]
    CSS = """
    #top { height: auto; padding: 0 1; background: $panel; }
    #title { height: 1; text-style: bold; }
    #chips { height: 1; }
    #agents { height: 1; }
    #main { height: 1fr; }
    #tasks { height: 1fr; }
    #side { height: 1fr; border-top: solid $primary 50%; padding: 0 2; }
    DataTable { background: transparent; }
    """

    def __init__(self, conn, stale_min=15, every=2):
        super().__init__()
        self.conn, self.stale_min, self.every = conn, stale_min, every
        self.selected, self.show_all, self.proc = None, False, None

    # ---- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="top"):
            yield Static(id="title")
            yield Static(id="chips")
            yield Static(id="agents")
        with Vertical(id="main"):
            yield DataTable(id="tasks", cursor_type="row", zebra_stripes=False)
            yield TaskDetail(id="side")
        yield Footer()

    def on_mount(self):
        table = self.query_one(DataTable)
        for label, key in (("", "icon"), ("id", "id"), ("agent", "agent"), ("last", "last"), ("title", "title")):
            table.add_column(label, key=key)
        self.refresh_board()
        self.set_interval(self.every, self.refresh_board)
        table.focus()

    # ---- the board -------------------------------------------------------

    def agents(self):
        try:
            return load_agents()
        except SystemExit:
            return {}

    @property
    def dispatching(self):
        return self.proc is not None and self.proc.poll() is None

    def refresh_board(self):
        t, rows, summary = board_view(self.conn, self.stale_min, self.show_all)
        table = self.query_one(DataTable)
        table.clear()
        for task, state, _ in rows:
            who, level, last, left = row_cells(task, state, t)
            table.add_row(Text(ICONS[state], style=STATE_STYLE.get(state, "")), Text(task["id"], style="dim"),
                          Text(who, overflow="ellipsis", no_wrap=True), Text(last, style="dim"),
                          Text(task["title"], style="dim" if state in ("done", "cancelled") else "", overflow="ellipsis",
                               no_wrap=True),
                          key=task["id"])
        ids = [task["id"] for task, _, _ in rows]
        if self.selected not in ids:
            self.selected = ids[0] if ids else None
        if self.selected:
            table.move_cursor(row=ids.index(self.selected), animate=False)
        title = Text(f"nextrunner  {time.strftime('%H:%M:%S', time.localtime(t))}", style="bold")
        if self.dispatching:
            title.append("   dispatching…", style="cyan")
        self.query_one("#title").update(title)
        self.query_one("#chips").update(counts_line(rows, self.show_all))
        self.query_one("#agents").update(agents_line(agent_status(self.conn, self.agents(), rows)))
        self.show_detail()

    def show_detail(self):
        side = self.query_one("#side", TaskDetail)
        if not self.selected:
            side.show_message("no tasks", "Press **a** to add a task.")
        else:
            side.show(self.conn, self.selected)

    def on_data_table_row_highlighted(self, event):
        if event.row_key is not None and event.row_key.value is not None:
            self.selected = event.row_key.value
            self.show_detail()

    def on_data_table_row_selected(self, event):
        self.action_open()

    def say(self, message):
        self.notify(message, timeout=6)

    # ---- actions ---------------------------------------------------------

    def action_move(self, step: int):
        table = self.query_one(DataTable)
        table.action_cursor_down() if step > 0 else table.action_cursor_up()

    def action_open(self):
        if self.selected:
            self.push_screen(TextScreen(self.conn, self.selected))

    def action_toggle_all(self):
        self.show_all = not self.show_all
        self.say("showing every task" if self.show_all else "showing open tasks and the last hour")
        self.refresh_board()

    def action_add(self):
        def finish(answers):
            if answers is not None:
                task_id, message = add_from_answers(self.conn, answers)
                self.selected = task_id or self.selected
                self.say(message)
                self.refresh_board()
        self.push_screen(AddScreen(self.agents()), finish)

    def action_follow_up(self):
        if not self.selected:
            return
        earlier = self.selected

        def finish(answers):
            if answers is not None:
                task_id, message = add_from_answers(self.conn, answers, follows=earlier)
                self.selected = task_id or self.selected
                self.say(message)
                self.refresh_board()
        self.push_screen(AddScreen(self.agents(), follows=earlier), finish)

    def action_reopen(self):
        if self.selected:
            ok = reopen(self.conn, self.selected)
            self.say(f"reopened {self.selected}" if ok else f"{self.selected} is not blocked or done")
            self.refresh_board()

    def action_dispatch(self):
        if self.dispatching:
            self.say("a dispatcher started from here is still running")
            return

        def finish(jobs):
            if jobs is None:
                return
            if not jobs.isdigit() or int(jobs) < 1:
                self.say("cancelled: jobs must be a number of 1 or more")
                return
            log_path = paths.log_file()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a") as log_file:
                self.proc = subprocess.Popen([*self_command(), "dispatch", "--jobs", jobs], stdout=log_file,
                                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
            self.say(f"dispatching with --jobs {jobs}; output in {log_path}")
            self.refresh_board()
        self.push_screen(AskScreen("Run how many tasks at once?", "3"), finish)

    def action_pause(self):
        agents = self.agents()
        if not agents:
            self.say("no agents: agents.json is missing")
            return

        def finish(answer):
            if answer is None:
                return
            name, minutes = answer
            if minutes is None:
                set_up(self.conn, name)
                self.say(f"{name} resumed")
            else:
                set_down(self.conn, name, minutes, "paused by hand")
                self.say(f"{name} paused for {minutes:g} min")
            self.refresh_board()
        self.push_screen(PauseScreen(self.conn, agents), finish)


def run_ui(conn, stale_min=15, every=2):
    BoardApp(conn, stale_min, every).run()

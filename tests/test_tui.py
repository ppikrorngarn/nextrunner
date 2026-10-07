"""The full-screen board, driven with Textual's test pilot (no terminal needed)."""
import asyncio
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from rich.console import Console
from textual.widgets import DataTable

from nextrunner import agents, board, db, tui


def table_text(widget):
    """What a Static holding a Rich table shows, as plain text."""
    console = Console(width=200, file=io.StringIO(), record=True)
    console.print(widget.content)
    return console.export_text()


def markdown_text(widget):
    """What a Markdown widget shows, block by block, as plain text."""
    return "\n".join(str(block.content) for block in widget.children)


def text_of(widget):
    return str(getattr(widget, "content", None) or widget.render())


class UiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        (home / "agents.json").write_text(json.dumps({"alpha": {"cmd": ["x"], "parallel": 2}, "beta": {"cmd": ["y"]}}))
        patcher = mock.patch.dict(os.environ, {"NEXTRUNNER_HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.first = board.add(self.conn, "first [odd] title", "the brief", to="alpha", cwd=self.tmp.name)
        self.second = board.add(self.conn, "second", by="human")

    def app(self):
        return tui.BoardApp(self.conn, every=60)

    async def test_lists_open_tasks_and_a_summary(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one(DataTable).row_count, 2)
            self.assertIn("2 ready", text_of(app.query_one("#chips")))
            self.assertIn("● alpha 0/2", text_of(app.query_one("#agents")))
            self.assertIn("first [odd] title", text_of(app.query_one("#d-head")))
            self.assertEqual(app.query_one("#d-body").source, "the brief")

    async def test_a_single_newline_in_the_brief_stays_a_line_break(self):
        task = board.add(self.conn, "lines", "step one\nstep two", to="alpha")
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.query_one(tui.TaskDetail).show(self.conn, task)
            await pilot.pause()
            self.assertIn("step one\nstep two", markdown_text(app.query_one("#d-body")))

    async def test_the_full_screen_keeps_line_breaks_in_the_brief_and_the_result(self):
        task = board.add(self.conn, "lines", "step one\nstep two", to="alpha")
        self.conn.execute("UPDATE tasks SET result = ? WHERE id = ?", ("done one\ndone two", task))
        self.conn.commit()
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.push_screen(tui.TextScreen(self.conn, task))
            await pilot.pause()
            self.assertIn("step one\nstep two", markdown_text(app.screen.query_one("#d-body")))
            self.assertIn("done one\ndone two", markdown_text(app.screen.query_one("#d-body2")))

    async def test_moving_changes_the_detail_pane(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.selected, self.second)
            self.assertIn("second", text_of(app.query_one("#d-head")))

    async def test_j_and_k_move_like_the_arrows(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            self.assertEqual(app.selected, self.second)
            await pilot.press("k")
            await pilot.pause()
            self.assertEqual(app.selected, self.first)

    async def test_enter_opens_the_task(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, tui.TextScreen)
            self.assertIn("Events", text_of(app.screen.query_one("#d-events-label")))
            self.assertIn("first [odd] title", text_of(app.screen.query_one("#d-head")))
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, tui.TextScreen)
            await pilot.press("enter", "left")  # the left arrow goes back too
            await pilot.pause()
            self.assertNotIsInstance(app.screen, tui.TextScreen)

    async def test_the_task_view_shows_brief_result_and_every_event_in_colour(self):
        token = board.claim(self.conn, self.first, "alpha")
        for i in range(20):
            board.note(self.conn, self.first, "alpha", f"checkpoint {i}: " + "word " * 80)
        board.done(self.conn, self.first, "alpha", "## Done\n\n| a | b |\n|---|---|\n| 1 | 2 |", token)
        app = self.app()
        app.selected = self.first
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            side = app.query_one("#side")
            self.assertEqual(side.query_one("#d-body").source[:7], "## Done")  # beside the list: the result only
            self.assertFalse(side.query_one("#d-body2").display)
            self.assertIn("earlier", table_text(side.query_one("#d-events")))  # and only the latest events
            await pilot.press("enter")
            await pilot.pause()
            full = app.screen
            self.assertEqual(full.query_one("#d-body").source, "the brief")
            self.assertEqual(text_of(full.query_one("#d-label")), "Brief")
            self.assertTrue(full.query_one("#d-body2").source.startswith("## Done"))
            self.assertEqual(text_of(full.query_one("#d-label2")), "Result")
            events = table_text(full.query_one("#d-events"))
            self.assertIn("checkpoint 0:", events)  # all of them
            self.assertNotIn("earlier", events)
            self.assertEqual(events.count("word"), 20 * 80)  # not cut

    async def test_the_task_view_keeps_up_with_a_running_task(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertNotIn("a late note", table_text(app.screen.query_one("#d-events")))
            board.note(self.conn, self.first, "alpha", "a late note")
            app.screen.refresh_task()
            await pilot.pause()
            self.assertIn("a late note", table_text(app.screen.query_one("#d-events")))

    async def test_q_quits_even_from_inside_a_task(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, tui.TextScreen)
            with mock.patch.object(app, "exit") as leave:
                await pilot.press("q")
                await pilot.pause()
            leave.assert_called_once()
            self.assertIsInstance(app.screen, tui.TextScreen)  # q did not just go back

    async def test_add_a_task_with_the_form(self):
        app = self.app()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            self.assertIsInstance(app.screen, tui.AddScreen)
            await pilot.press(*"write the report")
            await pilot.press("ctrl+s")
            await pilot.pause()
            added = [t for t in self.conn.execute("SELECT * FROM tasks") if t["title"] == "write the report"]
            self.assertEqual(len(added), 1)
            self.assertIsNone(added[0]["assignee"])
            self.assertEqual(added[0]["cwd"], os.getcwd())
            self.assertEqual(app.query_one(DataTable).row_count, 3)

    async def test_an_empty_title_adds_nothing(self):
        app = self.app()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)

    async def test_follow_up_continues_the_selected_task(self):
        app = self.app()
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()
            await pilot.press(*"and then this")
            await pilot.press("ctrl+s")
            await pilot.pause()
            follow = [t for t in self.conn.execute("SELECT * FROM tasks") if t["title"] == "and then this"][0]
            self.assertEqual((follow["follows"], follow["assignee"], follow["strict"]), (self.first, "alpha", 1))
            self.assertEqual(follow["cwd"], self.tmp.name)

    async def test_y_approves_a_held_task(self):
        held = board.add(self.conn, "held draft", hold=True)
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertIn("1 held", text_of(app.query_one("#chips")))
            await pilot.press("down", "down")  # the held task is the newest, so it is last
            await pilot.pause()
            self.assertEqual(app.selected, held)
            await pilot.press("y")
            await pilot.pause()
            self.assertEqual(board.get(self.conn, held)["held"], 0)
            self.assertNotIn("held", text_of(app.query_one("#chips")))

    async def test_x_cancels_the_selected_task(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.selected, self.first)
            await pilot.press("x")
            await pilot.pause()
            self.assertEqual(board.get(self.conn, self.first)["status"], "cancelled")
            self.assertIn("1 cancelled", text_of(app.query_one("#chips")))
            await pilot.press("o")  # the row is still on the board for an hour, so o brings it back
            await pilot.pause()
            self.assertEqual(board.get(self.conn, self.first)["status"], "ready")

    async def test_reopen_a_blocked_task(self):
        self.conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (self.first,))
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            self.assertEqual(board.get(self.conn, self.first)["status"], "ready")

    async def test_t_shows_old_finished_tasks(self):
        self.conn.execute("UPDATE tasks SET status = 'done', updated_at = updated_at - 7200 WHERE id = ?", (self.second,))
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one(DataTable).row_count, 1)
            await pilot.press("t")
            await pilot.pause()
            self.assertEqual(app.query_one(DataTable).row_count, 2)

    async def test_pause_and_resume_an_agent(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertIsInstance(app.screen, tui.PauseScreen)
            await pilot.click("#pause-btn")
            await pilot.pause()
            self.assertFalse(agents.is_up(self.conn, "alpha"))
            self.assertIn("◌ alpha resting", text_of(app.query_one("#agents")))
            await pilot.press("p")
            await pilot.pause()
            await pilot.click("#resume-btn")
            await pilot.pause()
            self.assertTrue(agents.is_up(self.conn, "alpha"))

    async def test_dispatch_starts_a_separate_process(self):
        app = self.app()
        with mock.patch.object(tui.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("d")
                await pilot.pause()
                await pilot.press("backspace", "2", "enter")
                await pilot.pause()
                self.assertEqual(popen.call_args.args[0][-3:], ["dispatch", "--jobs", "2"])
                self.assertIn("dispatching", text_of(app.query_one("#title")))
        self.assertTrue((Path(self.tmp.name) / "dispatch.log").exists())

    async def test_a_second_dispatch_is_refused_while_one_runs(self):
        app = self.app()
        with mock.patch.object(tui.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = None
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                await pilot.press("d", "enter")
                await pilot.pause()
                await pilot.press("d")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, tui.AskScreen)
                self.assertEqual(popen.call_count, 1)


    async def test_a_result_is_shown_as_markdown(self):
        token = board.claim(self.conn, self.second, "alpha")
        board.done(self.conn, self.second, "alpha", "## Done\n\n| a | b |\n|---|---|\n| 1 | 2 |", token)
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            self.assertTrue(app.query_one("#d-body").source.startswith("## Done"))
            self.assertIn("Result", text_of(app.query_one("#d-label")))

    async def test_markdown_waits_for_a_pane_with_width(self):
        # A pty that never reports a window size is 0x0, and Textual's Markdown loops forever at width 0
        # (the packaging smoke test covers the real thing). So nothing is rendered until the pane has
        # width, and the result still appears once it does.
        token = board.claim(self.conn, self.first, "alpha")
        board.done(self.conn, self.first, "alpha", "## Done\n\nsome **result** text", token)
        app = self.app()
        app.selected = self.first  # a finished task sorts last, so select it by hand

        async def run():
            async with app.run_test(size=(1, 1)) as pilot:
                await pilot.pause()
                before = app.query_one("#d-body").source
                await pilot.resize_terminal(120, 40)
                await pilot.pause(0.5)  # the pane looks again after 0.2 s
                return before, app.query_one("#d-body").source
        before, after = await asyncio.wait_for(run(), 30)
        self.assertEqual(before, "")
        self.assertTrue(after.startswith("## Done"))

    async def test_filter_narrows_the_list_and_escape_clears_it(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash", *"alpha")
            await pilot.pause()
            table = app.query_one(DataTable)
            self.assertEqual(table.row_count, 1)  # only the task for alpha
            self.assertEqual(app.selected, self.first)
            self.assertIn("filter: alpha (1/2)", text_of(app.query_one("#title")))
            await pilot.press("enter")  # keeps the filter, returns to the list
            await pilot.pause()
            self.assertEqual(table.row_count, 1)
            self.assertIs(app.focused, table)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(table.row_count, 2)
            self.assertNotIn("filter", text_of(app.query_one("#title")))

    async def test_a_filter_with_no_match_says_so(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash", *"zzz")
            await pilot.pause()
            self.assertEqual(app.query_one(DataTable).row_count, 0)
            self.assertIn("nothing matches", text_of(app.query_one("#d-head")))

    async def test_toasts_when_a_task_finishes_or_blocks_but_not_on_first_look(self):
        app = self.app()
        toasts = []
        app.notify = lambda message, **kw: toasts.append((kw.get("title"), message))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(toasts, [])  # the first look at the board says nothing
            token = board.claim(self.conn, self.first, "alpha")
            board.done(self.conn, self.first, "alpha", "ok", token)
            self.conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (self.second,))
            app.refresh_board()
            await pilot.pause()
            self.assertEqual(sorted(title for title, _ in toasts), ["✓ done", "✗ blocked"])
            self.assertIn(self.first, dict((t, m) for t, m in toasts)["✓ done"])
            app.refresh_board()
            await pilot.pause()
            self.assertEqual(len(toasts), 2)  # no repeat

    async def test_layout_follows_the_width(self):
        app = self.app()
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#main").has_class("-narrow"))
            await pilot.resize_terminal(90, 40)
            await pilot.pause()
            self.assertTrue(app.query_one("#main").has_class("-narrow"))

    async def test_the_table_never_scrolls_sideways(self):
        board.add(self.conn, "a very long title " * 12, to="alpha")
        for width in (140, 90, 70):
            app = self.app()
            async with app.run_test(size=(width, 40)) as pilot:
                await pilot.pause()
                await pilot.pause()
                self.assertEqual(app.query_one(DataTable).max_scroll_x, 0, width)

    async def test_theme_and_view_are_remembered(self):
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.theme = "nord"
            await pilot.press("t")
            await pilot.pause()
        saved = json.loads((Path(self.tmp.name) / "ui.json").read_text())
        self.assertEqual(saved, {"theme": "nord", "show_all": True})
        again = self.app()
        async with again.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual((again.theme, again.show_all), ("nord", True))

    async def test_a_bad_saved_theme_falls_back(self):
        (Path(self.tmp.name) / "ui.json").write_text('{"theme": "no-such-theme"}')
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.theme, tui.DEFAULT_THEME)

    async def test_no_agents_file_says_how_to_fix_it(self):
        (Path(self.tmp.name) / "agents.json").unlink()
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertIn("nextrunner init", text_of(app.query_one("#agents")))


if __name__ == "__main__":
    unittest.main()


class LineBreakTest(unittest.TestCase):
    def tokens(self, source):
        return [child.type for token in tui.keep_line_breaks().parse(source) for child in token.children or ()]

    def test_a_single_newline_becomes_a_hard_break(self):
        self.assertEqual(self.tokens("one\ntwo"), ["text", "hardbreak", "text"])
        self.assertNotIn("softbreak", self.tokens("one\ntwo\nthree"))

    def test_a_blank_line_still_starts_a_new_paragraph(self):
        kinds = [token.type for token in tui.keep_line_breaks().parse("one\n\ntwo")]
        self.assertEqual(kinds.count("paragraph_open"), 2)

    def test_code_blocks_are_left_alone(self):
        fence = [token for token in tui.keep_line_breaks().parse("```\na\nb\n```") if token.type == "fence"]
        self.assertEqual(fence[0].content, "a\nb\n")

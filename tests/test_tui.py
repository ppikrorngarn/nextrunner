"""The full-screen board, driven with Textual's test pilot (no terminal needed)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import DataTable

from nextrunner import agents, board, db, tui


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

    async def test_no_agents_file_says_how_to_fix_it(self):
        (Path(self.tmp.name) / "agents.json").unlink()
        app = self.app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            self.assertIn("nextrunner init", text_of(app.query_one("#agents")))


if __name__ == "__main__":
    unittest.main()

"""Where files go, and how to point nextrunner elsewhere."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nextrunner import cli, db, dispatcher, paths

VARS = ("NEXTRUNNER_HOME", "NEXTRUNNER_DB", "NEXTRUNNER_AGENTS", "NEXTRUNNER_LOG")


class PathsTest(unittest.TestCase):
    def env(self, **values):
        clean = {k: v for k, v in os.environ.items() if k not in VARS}
        return mock.patch.dict(os.environ, {**clean, **values}, clear=True)

    def test_tests_run_in_a_throwaway_home(self):
        self.assertIn("nextrunner-test-home-", os.environ["NEXTRUNNER_HOME"])
        self.assertEqual(paths.db_file().parent, Path(os.environ["NEXTRUNNER_HOME"]))

    def test_home_holds_all_three_files(self):
        with self.env(NEXTRUNNER_HOME="/x/home"):
            self.assertEqual(paths.db_file(), Path("/x/home/board.db"))
            self.assertEqual(paths.agents_file(), Path("/x/home/agents.json"))
            self.assertEqual(paths.log_file(), Path("/x/home/dispatch.log"))

    def test_a_single_file_variable_beats_home(self):
        with self.env(NEXTRUNNER_HOME="/x/home", NEXTRUNNER_DB="/y/other.db"):
            self.assertEqual(paths.db_file(), Path("/y/other.db"))
            self.assertEqual(paths.agents_file(), Path("/x/home/agents.json"))

    def test_log_sits_next_to_a_board_given_alone(self):
        with self.env(NEXTRUNNER_DB="/y/trial/board.db"):
            self.assertEqual(paths.log_file(), Path("/y/trial/dispatch.log"))

    def test_platform_defaults_without_variables(self):
        with self.env(HOME="/home/u", XDG_CONFIG_HOME="", XDG_DATA_HOME="", XDG_STATE_HOME=""), \
                mock.patch.object(Path, "home", return_value=Path("/home/u")):
            for platform, config, data, log in [
                ("darwin", "/home/u/Library/Application Support/nextrunner", "/home/u/Library/Application Support/nextrunner",
                 "/home/u/Library/Logs/nextrunner"),
                ("linux", "/home/u/.config/nextrunner", "/home/u/.local/share/nextrunner", "/home/u/.local/state/nextrunner"),
            ]:
                with mock.patch.object(sys, "platform", platform):
                    self.assertEqual(paths.agents_file(), Path(config) / "agents.json", platform)
                    self.assertEqual(paths.db_file(), Path(data) / "board.db", platform)
                    self.assertEqual(paths.log_file(), Path(log) / "dispatch.log", platform)

    def test_linux_follows_xdg_variables(self):
        with self.env(XDG_CONFIG_HOME="/c", XDG_DATA_HOME="/d", XDG_STATE_HOME="/s"), \
                mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(paths.agents_file(), Path("/c/nextrunner/agents.json"))
            self.assertEqual(paths.db_file(), Path("/d/nextrunner/board.db"))
            self.assertEqual(paths.log_file(), Path("/s/nextrunner/dispatch.log"))

    def test_where_says_why(self):
        with self.env(NEXTRUNNER_HOME="/x/home", NEXTRUNNER_DB="/y/other.db"):
            why = {label: reason for label, _, reason in paths.describe()}
        self.assertEqual(why, {"board": "NEXTRUNNER_DB", "agents": "NEXTRUNNER_HOME", "log": "NEXTRUNNER_HOME"})

    def test_connect_makes_the_folder(self):
        with tempfile.TemporaryDirectory() as tmp, self.env(NEXTRUNNER_HOME=str(Path(tmp) / "new" / "folder")):
            db.connect().close()
            self.assertTrue((Path(tmp) / "new" / "folder" / "board.db").exists())

    def test_home_flag_moves_every_file(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, self.env(), contextlib.redirect_stdout(out):
            cli.main(["--home", tmp, "where"])
        self.assertEqual(out.getvalue().count(tmp), 3)

    def test_the_app_starts_itself_the_right_way(self):
        self.assertEqual(dispatcher.self_command(), [sys.executable, "-m", "nextrunner"])
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertEqual(dispatcher.self_command(), [sys.executable])


if __name__ == "__main__":
    unittest.main()

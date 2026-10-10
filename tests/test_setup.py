"""nextrunner init and nextrunner doctor."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nextrunner import agents, cli, setup

PY = sys.executable


class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"NEXTRUNNER_HOME": self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = Path(self.tmp.name) / "agents.json"

    def test_writes_the_template_by_default(self):
        lines = setup.init()
        self.assertEqual(json.loads(self.path.read_text()), json.loads(setup.example_text()))
        self.assertIn("nextrunner doctor", lines[1])

    def test_agent_flag_writes_just_those_agents(self):
        setup.init(["bot=bot run --fast", f"other={PY} -c {{prompt}}"])
        agents = json.loads(self.path.read_text())
        self.assertEqual(agents["bot"], {"cmd": ["bot", "run", "--fast", "{prompt}"], "reply": "stdout"})
        self.assertEqual(agents["other"]["cmd"], [PY, "-c", "{prompt}"])  # already has {prompt}, so none added
        self.assertEqual(list(agents), ["bot", "other"])

    def test_windows_paths_keep_their_backslashes(self):
        command = r'"C:\Program Files\Bot\bot.exe" --dir C:\work\repo'
        with mock.patch.object(setup.os, "name", "nt"):
            words = setup.split_command(command)
        self.assertEqual(words, [r"C:\Program Files\Bot\bot.exe", "--dir", r"C:\work\repo"])
        with mock.patch.object(setup.os, "name", "posix"):
            self.assertEqual(setup.split_command(r"bot --say a\ b"), ["bot", "--say", "a b"])  # a backslash escapes there

    @unittest.skipIf(sys.platform == "win32", "POSIX file modes")
    def test_the_file_is_private(self):
        setup.init()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_never_overwrites_unless_forced(self):
        self.path.write_text("{}")
        with self.assertRaises(SystemExit):
            setup.init()
        self.assertEqual(self.path.read_text(), "{}")
        setup.init(force=True)
        self.assertNotEqual(self.path.read_text(), "{}")

    def test_bad_agent_text(self):
        for bad in ("nocommand", "=run", "name="):
            with self.subTest(bad), self.assertRaises(SystemExit):
                setup.parse_agent(bad)

    def test_the_command_line(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["init", "--agent", f"echoer={PY} -c pass"])
        self.assertTrue(self.path.exists())
        with contextlib.redirect_stdout(io.StringIO()) as doc, self.assertRaises(SystemExit) as raised:
            cli.main(["doctor"])
        self.assertEqual(raised.exception.code, 0, doc.getvalue())


class DoctorTest(unittest.TestCase):
    def levels(self, agents):
        return [(level, text) for level, text in setup.check_agents(agents)]

    def errors(self, agents):
        return [text for level, text in setup.check_agents(agents) if level == "error"]

    def test_a_good_agent(self):
        results = self.levels({"a": {"cmd": [PY, "-c", "{prompt}"], "cmd_edit": [PY, "{prompt}"], "parallel": 2}})
        self.assertIn(("ok", "a: read and edit, parallel 2"), results)
        self.assertFalse(self.errors({"a": {"cmd": [PY, "{prompt}"]}}))

    def test_missing_program(self):
        self.assertIn("a.cmd: cannot find the program 'no-such-program-xyz' on PATH",
                      self.errors({"a": {"cmd": ["no-such-program-xyz", "{prompt}"]}}))

    def test_unknown_and_unbalanced_placeholders(self):
        errors = self.errors({"a": {"cmd": [PY, "{promt}", "{"]}})
        self.assertTrue(any("unknown placeholder {promt}" in e for e in errors), errors)
        self.assertTrue(any("a.cmd" in e and "'{'" in e for e in errors), errors)

    def test_literal_braces_are_fine_when_doubled(self):
        self.assertFalse(self.errors({"a": {"cmd": [PY, "-c", "print({{1}})", "{prompt}"]}}))

    def test_reply_modes(self):
        base = {"cmd": [PY, "{prompt}"]}
        self.assertTrue(self.errors({"a": {**base, "reply": "yaml"}}))
        self.assertTrue(self.errors({"a": {**base, "reply": "file"}}))  # never uses {out}
        self.assertFalse(self.errors({"a": {"cmd": [PY, "{out}"], "reply": "file"}}))
        self.assertFalse(self.errors({"a": {**base, "reply": "json:result"}}))
        self.assertTrue(self.errors({"a": {**base, "reply": "json:"}}))

    def test_session_pattern(self):
        base = {"cmd": [PY, "{prompt}"]}
        self.assertTrue(self.errors({"a": {**base, "session": "no group"}}))
        self.assertTrue(self.errors({"a": {**base, "session": "(unclosed"}}))
        results = self.levels({"a": {**base, "session": "id=(\\w+)"}})
        self.assertIn("warn", [level for level, _ in results])

    def test_structure(self):
        self.assertTrue(self.errors({}))
        self.assertTrue(self.errors({"a": {}}))
        self.assertTrue(self.errors({"a": {"cmd": [PY], "cmd_edit": "not a list"}}))

    def test_the_shipped_template_parses_but_needs_real_programs(self):
        template = json.loads(setup.example_text())
        errors = self.errors(template)
        self.assertTrue(errors)
        self.assertTrue(all("cannot find the program" in e for e in errors), errors)

    def test_timeout_must_be_a_positive_number(self):
        ok = {"a": {"cmd": [PY, "{prompt}"], "timeout": 1800}}
        self.assertEqual(self.errors(ok), [])
        self.assertIn(("ok", "a: read only, parallel 1, timeout 1800s"), self.levels(ok))
        for bad in ("1800", 0, -5, True):
            with self.subTest(timeout=bad):
                errors = self.errors({"a": {"cmd": [PY, "{prompt}"], "timeout": bad}})
                self.assertEqual(len(errors), 1)
                self.assertIn("timeout must be a number of seconds above 0", errors[0])

    def test_lists_are_spliced_into_commands(self):
        raw = {"$lists": {"tools": ["--allow", "Read", "Grep"]},
               "a": {"cmd": [PY, "@tools", "{prompt}"], "cmd_edit": [PY, "@tools", "--edit", "{prompt}"], "parallel": 2}}
        expanded = agents.expand(raw)
        self.assertEqual(list(expanded), ["a"])
        self.assertEqual(expanded["a"]["cmd"], [PY, "--allow", "Read", "Grep", "{prompt}"])
        self.assertEqual(expanded["a"]["cmd_edit"], [PY, "--allow", "Read", "Grep", "--edit", "{prompt}"])
        self.assertEqual(expanded["a"]["parallel"], 2)
        self.assertEqual(raw["a"]["cmd"][1], "@tools")  # the input is left alone
        self.assertEqual(self.errors(raw), [])

    def test_a_word_that_is_only_an_at_sign_is_a_word(self):
        self.assertEqual(agents.expand({"a": {"cmd": [PY, "@", "{prompt}"]}})["a"]["cmd"], [PY, "@", "{prompt}"])

    def test_doctor_names_a_missing_or_bad_list(self):
        errors = self.errors({"a": {"cmd": [PY, "@tools", "{prompt}"]}})
        self.assertEqual(errors, ["a.cmd: no list named 'tools' in $lists"])
        errors = self.errors({"$lists": {"tools": "not a list"}, "a": {"cmd": [PY, "{prompt}"]}})
        self.assertEqual(errors, ["$lists.tools must be a list of strings"])
        errors = self.errors({"$lists": {"x": ["@y"], "y": ["z"]}, "a": {"cmd": [PY, "{prompt}"]}})
        self.assertEqual(errors, ["$lists.x: a list may not use another list (@y)"])
        errors = self.errors({"$lists": {"x": ["z"]}})
        self.assertEqual(errors, ["agents.json must be an object with at least one agent"])

    def test_load_agents_expands_and_refuses_a_bad_file(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        path = Path(home.name) / "agents.json"
        with mock.patch.dict(os.environ, {"NEXTRUNNER_HOME": home.name}):
            path.write_text(json.dumps({"$lists": {"t": ["--x"]}, "a": {"cmd": ["prog", "@t", "{prompt}"]}}))
            self.assertEqual(agents.load_agents(), {"a": {"cmd": ["prog", "--x", "{prompt}"]}})
            path.write_text(json.dumps({"a": {"cmd": ["prog", "@nope", "{prompt}"]}}))
            with self.assertRaises(SystemExit) as stop:
                agents.load_agents()
            self.assertIn("no list named 'nope'", str(stop.exception))

    def test_usage_patterns_are_checked(self):
        base = {"cmd": [PY, "{prompt}"]}
        self.assertEqual(self.errors({"a": dict(base, usage={"cost": r"cost=([0-9.]+)"})}), [])
        for bad, expected in (("cost=x", "must be an object"), ({"cost": "cost=x"}, "needs one (group)"),
                              ({"cost": "("}, "bad pattern"), ({"cost": 3}, "must be an object")):
            with self.subTest(usage=bad):
                errors = self.errors({"a": dict(base, usage=bad)})
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(expected, errors[0])

    def test_doctor_says_which_commands_take_the_prompt_on_stdin(self):
        results = setup.check_agents({"a": {"cmd": [PY, "-"], "cmd_edit": [PY, "{prompt}"]}, "b": {"cmd": [PY, "{prompt}"]}})
        self.assertIn(("ok", "a: read and edit, parallel 1, prompt on stdin for cmd"), results)
        self.assertIn(("ok", "b: read only, parallel 1"), results)

    def test_roots_are_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = setup.check_agents({"$roots": [tmp, "/no/such/folder"], "a": {"cmd": [PY, "{prompt}"]}})
        self.assertIn(("warn", "$roots: /no/such/folder is not a folder on this machine"), results)
        self.assertIn(("ok", f"edit tasks may point inside: {tmp}, /no/such/folder"), results)
        self.assertEqual(setup.check_agents({"$roots": "~/code", "a": {"cmd": [PY, "{prompt}"]}}),
                         [("error", "$roots must be a list of folders")])
        self.assertEqual(setup.check_agents({"$roots": ["~/code"]}),
                         [("error", "agents.json must be an object with at least one agent")])

    def test_hooks_are_checked(self):
        good = {"$hooks": {"done": [PY, "-c", "pass", "{id}", "{title}", "{agent}", "{kind}", "{text}"]},
                "a": {"cmd": [PY, "{prompt}"]}}
        self.assertEqual(self.errors(good), [])
        self.assertIn(("ok", "hooks: done"), self.levels(good))
        self.assertEqual(agents.expand(good).keys(), {"a"})  # $hooks is not an agent
        cases = {
            "unknown kind": ({"$hooks": {"started": [PY]}, "a": {"cmd": [PY, "{prompt}"]}}, "unknown; hooks are"),
            "not a list": ({"$hooks": {"done": "say hi"}, "a": {"cmd": [PY, "{prompt}"]}}, "non-empty list"),
            "bad placeholder": ({"$hooks": {"done": [PY, "{prompt}"]}, "a": {"cmd": [PY, "{prompt}"]}}, "unknown placeholder {prompt}"),
            "missing program": ({"$hooks": {"done": ["no-such-prog-xyz", "{id}"]}, "a": {"cmd": [PY, "{prompt}"]}}, "cannot find the program"),
            "not an object": ({"$hooks": ["say"], "a": {"cmd": [PY, "{prompt}"]}}, "must be an object"),
        }
        for name, (raw, expected) in cases.items():
            with self.subTest(name):
                errors = self.errors(raw)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn(expected, errors[0])

    def test_doctor_reports_a_missing_file_and_bad_json(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"NEXTRUNNER_HOME": tmp}):
            lines, errors = setup.doctor()
            self.assertEqual(errors, 1)
            self.assertIn("nextrunner init", lines[0])
            (Path(tmp) / "agents.json").write_text("{nope")
            self.assertEqual(setup.doctor()[1], 1)


if __name__ == "__main__":
    unittest.main()

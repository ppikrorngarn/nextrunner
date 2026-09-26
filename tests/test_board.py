"""Tests for nextrunner. Run: python3 -m unittest -v   (from the repo root, with src on the path: PYTHONPATH=src, or uv run)"""
import contextlib
import io
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nextrunner import agents, board, cli, db, dispatcher, runner, view

PY = sys.executable
OK = {"cmd": [PY, "-c", "print('reply from a working agent')"]}
ECHO = {"cmd": [PY, "-c", "import sys; print(sys.argv[1])", "{prompt}"]}
LIMITED = {"cmd": [PY, "-c", "import sys; sys.exit(\"You've hit your usage limit. Try again at 5pm.\")"]}
BROKEN = {"cmd": [PY, "-c", "import sys; sys.exit('segfault in tool')"]}

RACERS, ROUNDS = 3, 1000


def racer(db_path, name, ids, barrier, results):
    conn = db.connect(db_path)
    won, slowest = 0, 0.0
    for task_id in ids:
        barrier.wait()  # all racers go for the same task at the same moment
        t = time.perf_counter()
        won += bool(board.claim(conn, task_id, name))
        slowest = max(slowest, time.perf_counter() - t)
    results.put((name, won, slowest))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "board.db")
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)

    def status(self, task_id):
        return db.get(self.conn, task_id)["status"]

    def kinds(self, task_id):
        rows = self.conn.execute("SELECT agent, kind FROM events WHERE task_id = ? ORDER BY id", (task_id,))
        return [(r["agent"], r["kind"]) for r in rows]

    def dispatch(self, agents):
        dispatcher.dispatch(self.conn, agents=agents, timeout=30, say=lambda line: None)


class BoardTest(Base):
    def test_claim_note_beat_done(self):
        t = board.add(self.conn, "write the report", by="human")
        self.assertTrue(board.claim(self.conn, t, "alpha"))
        board.note(self.conn, t, "alpha", "checkpoint: outline done")
        self.assertTrue(board.beat(self.conn, t, "alpha"))
        self.assertTrue(board.done(self.conn, t, "alpha", "report is in docs/report.md"))
        task = db.get(self.conn, t)
        self.assertEqual((task["status"], task["result"]), ("done", "report is in docs/report.md"))
        self.assertEqual([k for _, k in self.kinds(t)], ["created", "claimed", "note", "beat", "done"])

    def test_second_claim_is_refused(self):
        t = board.add(self.conn, "one task")
        self.assertTrue(board.claim(self.conn, t, "alpha"))
        self.assertFalse(board.claim(self.conn, t, "beta"))
        self.assertEqual(db.get(self.conn, t)["claimed_by"], "alpha")

    def test_expired_claim_can_be_taken_without_any_daemon(self):
        t = board.add(self.conn, "agent dies mid-task")
        self.assertTrue(board.claim(self.conn, t, "alpha", ttl=0.1))
        time.sleep(0.2)
        self.assertTrue(board.claim(self.conn, t, "beta"))
        # The agent that lost the task cannot finish or extend it any more.
        self.assertFalse(board.done(self.conn, t, "alpha", "late result"))
        self.assertFalse(board.beat(self.conn, t, "alpha"))
        self.assertTrue(board.done(self.conn, t, "beta", "finished by beta"))

    def test_task_for_one_agent_needs_steal(self):
        t = board.add(self.conn, "needs a tool only alpha has", to="alpha")
        self.assertFalse(board.claim(self.conn, t, "beta"))
        self.assertTrue(board.claim(self.conn, t, "beta", steal=True))

    def test_next_takes_oldest_task_meant_for_me(self):
        board.add(self.conn, "for alpha only", to="alpha")
        mine = board.add(self.conn, "for anyone")
        self.assertEqual(board.claim_next(self.conn, "beta")[0], mine)
        self.assertIsNone(board.claim_next(self.conn, "beta"))

    def test_old_claim_cannot_finish_after_same_name_retakes(self):
        t = board.add(self.conn, "claim expires, same name takes it again")
        first = board.claim(self.conn, t, "alpha", ttl=0.1)
        time.sleep(0.2)
        second = board.claim(self.conn, t, "alpha")
        self.assertTrue(second)
        self.assertNotEqual(first, second)
        self.assertFalse(board.done(self.conn, t, "alpha", "stale", token=first))
        self.assertFalse(board.beat(self.conn, t, "alpha", token=first))
        self.assertFalse(board.release(self.conn, t, "alpha", token=first))
        self.assertTrue(board.done(self.conn, t, "alpha", "fresh", token=second))
        self.assertEqual(db.get(self.conn, t)["result"], "fresh")

    def test_release_puts_task_back(self):
        t = board.add(self.conn, "hand back")
        board.claim(self.conn, t, "alpha")
        self.assertFalse(board.release(self.conn, t, "beta"))
        self.assertTrue(board.release(self.conn, t, "alpha", "need a human"))
        self.assertEqual(self.status(t), "ready")

    def test_race_has_exactly_one_winner_per_task(self):
        ids = [board.add(self.conn, f"race {i}") for i in range(ROUNDS)]
        ctx = multiprocessing.get_context("spawn")
        barrier, results = ctx.Barrier(RACERS), ctx.Queue()
        procs = [ctx.Process(target=racer, args=(self.db_path, f"agent{n}", ids, barrier, results)) for n in range(RACERS)]
        started = time.perf_counter()
        for proc in procs:
            proc.start()
        scores = [results.get(timeout=120) for _ in procs]
        for proc in procs:
            proc.join()
        elapsed = time.perf_counter() - started
        holders = [r["claimed_by"] for r in self.conn.execute("SELECT claimed_by FROM tasks")]
        self.assertEqual(sum(won for _, won, _ in scores), ROUNDS)  # no task was won twice
        self.assertTrue(all(holders))                                # and none was left unclaimed
        print(f"\n  race: {ROUNDS} tasks x {RACERS} racers in {elapsed:.2f}s; "
              f"wins {dict((n, w) for n, w, _ in scores)}; slowest single claim {max(s for *_, s in scores) * 1000:.1f} ms")


class DispatchTest(Base):
    def test_reply_is_recorded_word_for_word(self):
        t = board.add(self.conn, "say something")
        self.dispatch({"beta": OK})
        task = db.get(self.conn, t)
        self.assertEqual((task["status"], task["claimed_by"], task["result"]), ("done", "beta", "reply from a working agent"))

    def test_out_of_tokens_reroutes_and_rests_the_agent(self):
        t = board.add(self.conn, "survive a limit")
        self.dispatch({"alpha": LIMITED, "beta": OK})
        self.assertEqual((self.status(t), db.get(self.conn, t)["claimed_by"]), ("done", "beta"))
        self.assertIn(("alpha", "limited"), self.kinds(t))
        self.assertFalse(agents.is_up(self.conn, "alpha"))
        self.assertEqual(db.get(self.conn, t)["attempts"], 0)  # a limit is not the task's fault
        # While alpha rests, new work goes straight to beta without trying alpha.
        t2 = board.add(self.conn, "next task")
        self.dispatch({"alpha": LIMITED, "beta": OK})
        self.assertEqual(self.kinds(t2), [("human", "created"), ("beta", "claimed"), ("beta", "done")])

    def test_next_agent_sees_what_the_last_one_left(self):
        t = board.add(self.conn, "handoff")
        board.note(self.conn, t, "human", "the branch is fix/login")
        self.dispatch({"alpha": BROKEN, "beta": ECHO})
        prompt = db.get(self.conn, t)["result"]
        self.assertIn("- human (note): the branch is fix/login", prompt)
        self.assertIn("- alpha (failed): segfault in tool", prompt)

    def test_strict_task_waits_for_its_agent(self):
        t = board.add(self.conn, "needs a tool only alpha has", to="alpha", strict=True)
        self.dispatch({"alpha": LIMITED, "beta": OK})
        self.assertEqual(self.status(t), "ready")
        self.assertNotIn("beta", [agent for agent, _ in self.kinds(t)])

    def test_strict_task_blocks_after_max_attempts(self):
        t = board.add(self.conn, "always breaks", to="alpha", strict=True)
        self.dispatch({"alpha": BROKEN, "beta": OK})
        task = db.get(self.conn, t)
        self.assertEqual((task["status"], task["attempts"]), ("blocked", agents.MAX_ATTEMPTS))

    def test_task_blocks_when_every_agent_fails_then_reopens(self):
        t = board.add(self.conn, "nobody can do this")
        self.dispatch({"alpha": BROKEN, "beta": BROKEN})
        self.assertEqual(self.status(t), "blocked")
        self.assertTrue(board.reopen(self.conn, t))
        self.dispatch({"alpha": BROKEN, "beta": OK})
        self.assertEqual((self.status(t), db.get(self.conn, t)["claimed_by"]), ("done", "beta"))

    def test_all_agents_resting_leaves_task_waiting(self):
        t = board.add(self.conn, "everyone is out of tokens")
        self.dispatch({"alpha": LIMITED, "beta": LIMITED})
        self.assertEqual(self.status(t), "ready")

    def test_lane_without_a_starter_is_left_for_pulling(self):
        t = board.add(self.conn, "do this in a desktop app", to="desktop")
        self.dispatch({"beta": OK})
        self.assertEqual(self.kinds(t), [("human", "created")])
        self.assertEqual(board.claim_next(self.conn, "desktop")[0], t)

    def test_dead_agents_task_is_picked_up(self):
        t = board.add(self.conn, "claimed, then the agent vanished")
        board.claim(self.conn, t, "alpha", ttl=0.1)
        time.sleep(0.2)
        self.dispatch({"beta": OK})
        self.assertEqual((self.status(t), db.get(self.conn, t)["claimed_by"]), ("done", "beta"))

    def test_result_is_dropped_when_claim_is_lost(self):
        t = board.add(self.conn, "claim taken over while the agent runs")
        # The agent's run ends with its claim replaced by a newer one.
        retaken = {"cmd": [PY, "-c", "import sqlite3, sys; c = sqlite3.connect(sys.argv[1] + '/board.db'); "
                           "c.execute(\"UPDATE tasks SET claim_token = 'newer'\"); c.commit(); print('late reply')",
                           "{board}"]}
        said = []
        dispatcher.dispatch(self.conn, agents={"alpha": retaken}, timeout=30, say=said.append)
        self.assertIn(f"{t} result from alpha was dropped: the claim was lost", said)
        self.assertEqual(self.status(t), "running")
        self.assertIsNone(db.get(self.conn, t)["result"])

    def test_missing_program_counts_as_failure(self):
        t = board.add(self.conn, "agent not installed")
        self.dispatch({"ghost": {"cmd": ["no-such-agent-binary", "{prompt}"]}, "beta": OK})
        self.assertEqual(db.get(self.conn, t)["claimed_by"], "beta")

    def test_limit_inside_a_json_error_reply_rests_the_agent(self):
        # A CLI with JSON output reports its limit in the result field of an error reply.
        # dict() rather than {...}: commands go through str.format, so braces are placeholders.
        script = ("import json; print(json.dumps(dict(is_error=True, "
                  "result=\"You've hit your session limit \\u00b7 resets 11pm (UTC)\")))")
        limited = {"cmd": [PY, "-c", script], "reply": "json:result"}
        t = board.add(self.conn, "limit reported as JSON")
        self.dispatch({"alpha": limited, "beta": OK})
        self.assertIn(("alpha", "limited"), self.kinds(t))
        self.assertEqual(db.get(self.conn, t)["attempts"], 0)

    def test_limit_on_stdout_is_found_when_stderr_has_other_noise(self):
        # Seen with a real CLI: stderr says "Reading additional input from stdin..." while the
        # capacity error is a JSON event on stdout. Looking only at stderr counted it as a failed attempt.
        # dict() rather than {...}: commands go through str.format, so braces are placeholders.
        script = ("import sys, json; sys.stderr.write('Reading additional input from stdin...\\n'); "
                  "print(json.dumps(dict(type='turn.failed', error=dict("
                  "message='Selected model is at capacity. Please try a different model.')))); sys.exit(1)")
        t = board.add(self.conn, "capacity error on stdout")
        self.dispatch({"alpha": {"cmd": [PY, "-c", script]}, "beta": OK})
        self.assertIn(("alpha", "limited"), self.kinds(t))
        self.assertNotIn(("alpha", "failed"), self.kinds(t))
        self.assertEqual(db.get(self.conn, t)["attempts"], 0)
        self.assertFalse(agents.is_up(self.conn, "alpha"))

    def test_a_failure_with_no_limit_text_still_counts(self):
        script = "import sys; sys.stderr.write('noise\\n'); print('segfault in tool'); sys.exit(1)"
        t = board.add(self.conn, "plain failure")
        self.dispatch({"alpha": {"cmd": [PY, "-c", script]}, "beta": OK})
        self.assertIn(("alpha", "failed"), self.kinds(t))
        self.assertTrue(agents.is_up(self.conn, "alpha"))
        failure = [e["text"] for e in self.conn.execute("SELECT text FROM events WHERE task_id = ? AND kind = 'failed'", (t,))][0]
        self.assertEqual(failure, "noise\nsegfault in tool")


class LimitTextTest(unittest.TestCase):
    """LIMIT_RE against text real agent CLIs print. Product names and URLs are replaced."""

    # Usage limits, credits and expired logins: the agent should rest.
    RESTING = [
        "You've hit your usage limit. Upgrade to Pro (https://example.com/pro), visit "
        "https://example.com/usage to purchase more credits or try again at 1:26 AM.",
        "You're out of credits. Your workspace is out of credits. Add credits to continue.",
        "Usage limit reached. You've reached your usage limit. Increase your limits to continue.",
        "Quota exceeded. Check your plan and billing details.",
        "Selected model is at capacity. Please try a different model.",
        "Your access token could not be refreshed because your refresh token has expired. "
        "Please log out and sign in again.",
        "no credentials were found. Run agent login or provide an API key through a supported auth env var.",
        "You've hit your session limit \u00b7 resets 11pm (UTC)",
        "You've hit your weekly limit \u00b7 resets Oct 9, 5pm (UTC)",
        "You've hit your team's shared budget. Switch to another model to continue this chat.",
        "You're out of usage credits. /model to switch models.",
        "You're out of extra usage",
        "Credit balance is too low",
        "Failed to authenticate: OAuth session expired and could not be refreshed",
        "Failed to authenticate. API Error: 401 OAuth access token has expired. Re-authenticate to continue.",
        "Not logged in \u00b7 Run /login",
        "API call failed after 3 retries: HTTP 429: Service capacity or quota was reached. "
        "Slow down and retry shortly.",
        "API call failed after 3 retries: HTTP 429: Your token-plan 1-week quota has been exhausted.",
        "Billing or credits exhausted: HTTP 402: Insufficient available credits for this inference request.",
        "HTTP 401: Your API key is invalid, blocked or out of funds.",
        "HTTP 401: Invalid API key.",
    ]

    # Ordinary failures: these count against the task.
    FAILING = [
        "segfault in tool",
        "timed out after 600s",
        "exit code 1",
        "API call failed after 3 retries: Connection error.",
        "Could not start command 'agent'. Install the CLI or set its path.",
        "agent -z: no final response was produced; treating the run as failed.",
        "Traceback (most recent call last):\n  File \"main.py\", line 3\nSyntaxError: invalid syntax",
        "fatal: not a git repository (or any of the parent directories): .git",
        "error: unknown option '--fast'",
        "Error: ENOENT: no such file or directory, open 'login.py'",
        "tests failed: 2 of 30, see test_session.py",
    ]

    def test_limit_and_login_messages_match(self):
        for text in self.RESTING:
            with self.subTest(text=text):
                self.assertRegex(text, agents.LIMIT_RE)

    def test_ordinary_failures_do_not_match(self):
        for text in self.FAILING:
            with self.subTest(text=text):
                self.assertNotRegex(text, agents.LIMIT_RE)

    def test_reason_is_the_matching_line(self):
        text = "Reading additional input from stdin...\n" + "diff --git a/x b/x\n" * 50 + \
               '{"type":"turn.failed","error":{"message":"Selected model is at capacity."}}'
        self.assertEqual(agents.limit_reason(text), '{"type":"turn.failed","error":{"message":"Selected model is at capacity."}}')
        self.assertEqual(agents.limit_reason("x" * 500 + " quota"), ("x" * 500 + " quota")[:200])


class EditLevelTest(Base):
    READER = {"cmd": [PY, "-c", "print('ran the read command')"]}
    EDITOR = {"cmd": [PY, "-c", "print('ran the read command')"],
              "cmd_edit": [PY, "-c", "print('ran the edit command')"]}

    def result(self, task_id):
        task = db.get(self.conn, task_id)
        return task["status"], task["claimed_by"], task["result"]

    def test_task_is_read_only_unless_added_with_edit(self):
        read = board.add(self.conn, "look at the code")
        edit = board.add(self.conn, "fix the code", edit=True)
        self.dispatch({"alpha": self.EDITOR})
        self.assertEqual(self.result(read), ("done", "alpha", "ran the read command"))
        self.assertEqual(self.result(edit), ("done", "alpha", "ran the edit command"))

    def test_edit_task_skips_agents_without_an_edit_command(self):
        t = board.add(self.conn, "fix the code", to="alpha", edit=True)
        self.dispatch({"alpha": self.READER, "beta": self.EDITOR})
        self.assertEqual(self.result(t), ("done", "beta", "ran the edit command"))
        self.assertNotIn("alpha", [agent for agent, _ in self.kinds(t)])

    def test_edit_task_blocks_when_nobody_may_edit(self):
        t = board.add(self.conn, "fix the code", edit=True)
        strict = board.add(self.conn, "fix it, alpha only", to="alpha", strict=True, edit=True)
        self.dispatch({"alpha": self.READER, "beta": self.READER})
        for task_id in (t, strict):
            self.assertEqual(self.status(task_id), "blocked")
            reason = self.conn.execute("SELECT text FROM events WHERE task_id = ? AND kind = 'blocked'", (task_id,)).fetchone()
            self.assertIn("edit", reason["text"])

    def test_prompt_states_the_level_and_placeholders_are_filled(self):
        folder = Path(self.tmp.name)
        show = {"cmd": [PY, "-c", "import sys; print(sys.argv[1]); print('board=' + sys.argv[2])", "{prompt}", "{board}"]}
        show["cmd_edit"] = show["cmd"]
        read = board.add(self.conn, "look")
        edit = board.add(self.conn, "change", edit=True, cwd=str(folder))
        self.dispatch({"alpha": show})
        self.assertIn("This task is read-only.", db.get(self.conn, read)["result"])
        self.assertIn(f"You may change files inside {folder}.", db.get(self.conn, edit)["result"])
        self.assertTrue(db.get(self.conn, edit)["result"].endswith(f"board={folder.resolve()}"))

    def test_board_made_before_edit_levels_still_opens(self):
        old_path = str(Path(self.tmp.name) / "old.db")
        old = db.connect(old_path)
        old.execute("ALTER TABLE tasks DROP COLUMN edit")
        old.close()
        conn = db.connect(old_path)
        self.addCleanup(conn.close)
        t = board.add(conn, "added after the upgrade")
        self.assertEqual(db.get(conn, t)["edit"], 0)


def meets(me, other):
    """An agent that marks it started, then waits for `other` to start too. Fails if run alone."""
    code = ("import os, sys, time; d = sys.argv[1]; open(os.path.join(d, sys.argv[2]), 'w').close(); "
            "end = time.time() + 3\n"
            "while not os.path.exists(os.path.join(d, sys.argv[3])):\n"
            "    time.sleep(0.02)\n"
            "    if time.time() > end: sys.exit('ran alone')\n"
            "print('met ' + sys.argv[3])")
    return {"cmd": [PY, "-c", code, "{board}", me, other]}


def solo(name):
    """Holds a lock file named after the agent for a moment; a second run of it at the same time fails."""
    code = ("import os, sys, time; p = os.path.join(sys.argv[1], sys.argv[2] + '.lock')\n"
            "try: fd = os.open(p, os.O_CREAT | os.O_EXCL)\n"
            "except FileExistsError: sys.exit('two runs at once')\n"
            "time.sleep(0.3); os.close(fd); os.remove(p); print('ran alone')")
    return {"cmd": [PY, "-c", code, "{board}", name]}


class JobsTest(Base):
    def run_jobs(self, agents, jobs):
        said = []
        dispatcher.dispatch(self.conn, agents=agents, timeout=30, say=said.append, jobs=jobs)
        return said

    def test_two_agents_run_at_the_same_time(self):
        a = board.add(self.conn, "first", to="alpha")
        b = board.add(self.conn, "second", to="beta")
        self.run_jobs({"alpha": meets("a", "b"), "beta": meets("b", "a")}, jobs=2)
        self.assertEqual(db.get(self.conn, a)["result"], "met b")
        self.assertEqual(db.get(self.conn, b)["result"], "met a")

    def test_one_job_still_runs_one_at_a_time(self):
        a = board.add(self.conn, "first", to="alpha")
        board.add(self.conn, "second", to="beta")
        # alpha waits for beta, which cannot start until alpha ends, so alpha fails and beta takes it.
        agents = {"alpha": meets("a", "b"), "beta": OK}
        start = time.time()
        self.run_jobs(agents, jobs=1)
        self.assertIn(("alpha", "failed"), self.kinds(a))
        self.assertGreater(time.time() - start, 2.5)

    def test_same_agent_never_runs_twice_at_once(self):
        ids = [board.add(self.conn, f"task {i}") for i in range(3)]
        self.run_jobs({"alpha": solo("alpha")}, jobs=3)
        for t in ids:
            self.assertEqual((self.status(t), db.get(self.conn, t)["result"]), ("done", "ran alone"))

    def test_task_for_a_busy_agent_waits_instead_of_rerouting(self):
        first = board.add(self.conn, "for anyone")
        second = board.add(self.conn, "for alpha", to="alpha")
        self.run_jobs({"alpha": solo("alpha"), "beta": OK}, jobs=2)
        self.assertEqual(db.get(self.conn, first)["claimed_by"], "alpha")
        self.assertEqual(db.get(self.conn, second)["claimed_by"], "alpha")
        self.assertNotIn("beta", [agent for agent, _ in self.kinds(second)])

    def test_task_for_anyone_goes_to_a_free_agent(self):
        ids = [board.add(self.conn, f"task {i}") for i in range(2)]
        self.run_jobs({"alpha": solo("alpha"), "beta": solo("beta")}, jobs=2)
        self.assertEqual({db.get(self.conn, t)["claimed_by"] for t in ids}, {"alpha", "beta"})

    def test_limits_and_failures_still_reroute_with_jobs(self):
        limited = board.add(self.conn, "survive a limit", to="alpha")
        broken = board.add(self.conn, "survive a crash", to="gamma")
        # alpha rests and gamma fails, so both tasks end with beta.
        self.run_jobs({"alpha": LIMITED, "beta": OK, "gamma": BROKEN}, jobs=3)
        for t in (limited, broken):
            self.assertEqual((self.status(t), db.get(self.conn, t)["claimed_by"]), ("done", "beta"))
            # A limit never counts as an attempt; each failure counts once. Which free agent
            # takes the limited task first depends on timing, so count failures instead of assuming.
            failures = sum(kind == "failed" for _, kind in self.kinds(t))
            self.assertEqual(db.get(self.conn, t)["attempts"], failures)
        self.assertFalse(agents.is_up(self.conn, "alpha"))
        self.assertIn(("alpha", "limited"), self.kinds(limited))
        self.assertIn(("gamma", "failed"), self.kinds(broken))

    def test_strict_task_blocks_after_max_attempts_with_jobs(self):
        t = board.add(self.conn, "always breaks", to="alpha", strict=True)
        other = board.add(self.conn, "fine")
        self.run_jobs({"alpha": BROKEN, "beta": OK}, jobs=2)
        self.assertEqual((self.status(t), db.get(self.conn, t)["attempts"]), ("blocked", agents.MAX_ATTEMPTS))
        self.assertEqual(self.status(other), "done")

    def test_lost_claim_is_still_dropped_with_jobs(self):
        t = board.add(self.conn, "claim taken over while the agent runs")
        retaken = {"cmd": [PY, "-c", "import sqlite3, sys; c = sqlite3.connect(sys.argv[1] + '/board.db'); "
                           "c.execute(\"UPDATE tasks SET claim_token = 'newer'\"); c.commit(); print('late reply')",
                           "{board}"]}
        said = self.run_jobs({"alpha": retaken, "beta": OK}, jobs=2)
        self.assertIn(f"{t} result from alpha was dropped: the claim was lost", said)
        self.assertIsNone(db.get(self.conn, t)["result"])

    def test_jobs_below_one_is_refused(self):
        with mock.patch.dict(os.environ, {"NEXTRUNNER_DB": self.db_path}), self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            cli.main(["dispatch", "--jobs", "0"])


if __name__ == "__main__":
    unittest.main()


# An agent that adds a.txt, changes b.txt, deletes c.txt and also edits notes.txt,
# which already had changes before the task.
EDITOR = {"cmd_edit": [PY, "-c", (
    "from pathlib import Path\n"
    "Path('a.txt').write_text('new\\n'); Path('b.txt').write_text('changed\\n'); Path('c.txt').unlink()\n"
    "Path('notes.txt').write_text(Path('notes.txt').read_text() + 'agent line\\n')\n"
    "print('Done the edits.\\nCOMMIT: Add a, change b, drop c')")],
    "cmd": [PY, "-c", "print('read only')"]}


class CommitTest(Base):
    def setUp(self):
        super().setUp()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        for name in ("b.txt", "c.txt", "notes.txt", "staged.txt"):
            (self.repo / name).write_text(f"{name}\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "start")
        # Work that was already in progress before the task, and must stay out of its commit.
        (self.repo / "notes.txt").write_text("notes.txt\nmy own edit\n")
        (self.repo / "staged.txt").write_text("staged by hand\n")
        self.git("add", "staged.txt")
        (self.repo / "scratch.txt").write_text("untracked\n")

    def git(self, *args):
        return runner.git(self.repo, *args).stdout.strip()

    def committed_files(self):
        return sorted(self.git("show", "--name-only", "--format=", "HEAD").splitlines())

    def test_commits_only_what_the_agent_changed(self):
        t = board.add(self.conn, "edit files", to="alpha", cwd=str(self.repo), commit=True)
        self.dispatch({"alpha": EDITOR})
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Add a, change b, drop c")
        self.assertIn(f"Board task {t}, done by alpha.", self.git("log", "-1", "--format=%b"))
        self.assertEqual(self.committed_files(), ["a.txt", "b.txt", "c.txt"])
        status = runner.git(self.repo, "status", "--porcelain").stdout
        self.assertIn(" M notes.txt", status)   # pre-existing change left alone
        self.assertIn("M  staged.txt", status)  # still staged, not committed
        self.assertIn("?? scratch.txt", status)
        commit_event = self.conn.execute(
            "SELECT text FROM events WHERE task_id = ? AND kind = 'commit'", (t,)).fetchone()["text"]
        self.assertIn("(3 files)", commit_event)
        self.assertIn("notes.txt", commit_event)  # named as left uncommitted

    def test_title_is_the_message_without_a_commit_line(self):
        agent = {"cmd_edit": [PY, "-c", "open('a.txt', 'w').write('x'); print('done')"], "cmd": OK["cmd"]}
        board.add(self.conn, "Write a.txt", to="alpha", cwd=str(self.repo), commit=True)
        self.dispatch({"alpha": agent})
        self.assertEqual(self.git("log", "-1", "--format=%s"), "Write a.txt")
        self.assertEqual(self.committed_files(), ["a.txt"])

    def test_edit_without_commit_leaves_changes_uncommitted(self):
        board.add(self.conn, "edit files", to="alpha", cwd=str(self.repo), edit=True)
        self.dispatch({"alpha": EDITOR})
        self.assertEqual(self.git("log", "-1", "--format=%s"), "start")

    def test_failed_run_commits_nothing(self):
        broken = {"cmd_edit": [PY, "-c", "open('a.txt', 'w').write('x'); raise SystemExit('segfault in tool')"]}
        t = board.add(self.conn, "edit files", to="alpha", strict=True, cwd=str(self.repo), commit=True)
        self.dispatch({"alpha": broken})
        self.assertEqual(self.git("log", "-1", "--format=%s"), "start")
        self.assertNotIn(("dispatcher", "commit"), self.kinds(t))

    def test_outside_a_repo_the_commit_is_skipped(self):
        folder = Path(self.tmp.name) / "plain"
        folder.mkdir()
        agent = {"cmd_edit": [PY, "-c", "open('a.txt', 'w').write('x'); print('done')"]}
        t = board.add(self.conn, "edit files", to="alpha", cwd=str(folder), commit=True)
        self.dispatch({"alpha": agent})
        text = self.conn.execute("SELECT text FROM events WHERE task_id = ? AND kind = 'commit'", (t,)).fetchone()["text"]
        self.assertTrue(text.startswith("skipped: the folder is not in a git repository"))
        self.assertEqual(self.status(t), "done")

    def test_commit_implies_edit_and_tells_the_agent(self):
        t = board.add(self.conn, "edit files", to="alpha", cwd=str(self.repo), commit=True)
        task = db.get(self.conn, t)
        self.assertTrue(task["edit"])
        prompt = runner.build_prompt(self.conn, task, "alpha", str(self.repo))
        self.assertIn("Do not commit", prompt)
        self.assertIn("COMMIT:", prompt)


class WatchTest(Base):
    def test_screen_flags_stale_and_expired_and_resting(self):
        quiet = board.add(self.conn, "quiet task", to="alpha")
        lively = board.add(self.conn, "lively task", to="beta")
        lost = board.add(self.conn, "agent died", to="gamma")
        waiting = board.add(self.conn, "not started")
        board.claim(self.conn, quiet, "alpha", ttl=3600)
        board.claim(self.conn, lively, "beta", ttl=3600)
        board.claim(self.conn, lost, "gamma", ttl=0.01)
        # The quiet task's last event was 20 minutes ago.
        self.conn.execute("UPDATE events SET at = at - 1200 WHERE task_id = ?", (quiet,))
        agents.set_down(self.conn, "delta", 30, "You've hit your usage limit")
        time.sleep(0.05)
        screen = view.render_status(self.conn, stale_min=15, width=200)
        line = {t: next(l for l in screen.splitlines() if l.startswith(t)) for t in (quiet, lively, lost, waiting)}
        self.assertIn("STALE", line[quiet])
        self.assertIn("running", line[lively])
        self.assertIn("left", line[lively])
        self.assertIn("expired", line[lost])
        self.assertIn("ready", line[waiting])
        self.assertIn("resting: delta", screen)
        self.assertIn("1 stale, 1 running, 1 expired, 1 ready", screen)

    def test_done_tasks_show_for_an_hour(self):
        recent, old = board.add(self.conn, "recent"), board.add(self.conn, "old")
        for t in (recent, old):
            board.claim(self.conn, t, "alpha")
            board.done(self.conn, t, "alpha", "ok")
        self.conn.execute("UPDATE tasks SET updated_at = updated_at - 7200 WHERE id = ?", (old,))
        screen = view.render_status(self.conn, width=200)
        self.assertIn(recent, screen.split("latest:")[0])
        self.assertNotIn(old, screen.split("latest:")[0])

    def test_status_prints_the_board(self):
        board.add(self.conn, "a task")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"NEXTRUNNER_DB": self.db_path}), contextlib.redirect_stdout(out):
            cli.main(["status"])
        self.assertIn("1 ready", out.getvalue())


class TaskIdTest(Base):
    def test_format(self):
        task_id = board.new_task_id(board.ID_EPOCH + 36 ** 3)
        self.assertRegex(task_id, r"^t_[0-9a-z]{10}$")
        self.assertEqual(task_id[2:8], "001000")

    def test_ids_sort_by_creation_time(self):
        times = [board.ID_EPOCH + s for s in (0, 1, 35, 36, 3600, 86400 * 365, 36 ** 6 - 1)]
        ids = [board.new_task_id(t) for t in times]
        self.assertEqual([i[:8] for i in ids], sorted(i[:8] for i in ids))

    def test_a_clash_draws_new_random_digits(self):
        first = board.add(self.conn, "first")
        # The first draw repeats the existing ID; the retry draws a fresh one.
        with mock.patch.object(board, "new_task_id", side_effect=[first, "t_retried000"]):
            second = board.add(self.conn, "second")
        self.assertEqual(second, "t_retried000")
        self.assertEqual(db.get(self.conn, first)["title"], "first")
        self.assertEqual(self.kinds(second), [("human", "created")])

    def test_gives_up_after_five_clashes(self):
        first = board.add(self.conn, "first")
        with mock.patch.object(board, "new_task_id", return_value=first):
            with self.assertRaises(sqlite3.IntegrityError):
                board.add(self.conn, "never fits")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_old_style_ids_still_work(self):
        self.conn.execute("INSERT INTO tasks (id, title, created_by, created_at, updated_at) "
                          "VALUES ('t_32d225ac', 'old', 'human', 0, 0)")
        self.assertTrue(board.claim(self.conn, "t_32d225ac", "alpha"))


class ColumnsTest(Base):
    def test_old_and_new_ids_line_up(self):
        self.conn.execute("INSERT INTO tasks (id, title, created_by, created_at, updated_at) "
                          "VALUES ('t_32d225ac', 'old', 'human', ?, ?)", (db.now(), db.now()))
        self.conn.execute("INSERT INTO events (task_id, at, agent, kind) VALUES ('t_32d225ac', ?, 'human', 'created')",
                          (db.now(),))
        board.add(self.conn, "new")
        rows = [l for l in view.render_status(self.conn, width=200).splitlines() if l.startswith("t_")]
        self.assertEqual(len({row.index("ready") for row in rows}), 1)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"NEXTRUNNER_DB": self.db_path}), contextlib.redirect_stdout(out):
            cli.main(["list"])
        self.assertEqual(len({row.index("ready") for row in out.getvalue().splitlines()}), 1)


def crowd(size, limit):
    """Marks itself running, waits up to 1.5 s until `size` runs are going, fails if more than `limit` run at once."""
    code = ("import os, sys, time, secrets; d = os.path.join(sys.argv[1], 'crowd'); os.makedirs(d, exist_ok=True)\n"
            "me = os.path.join(d, secrets.token_hex(4)); open(me, 'w').close(); end = time.time() + 1.5\n"
            "while len(os.listdir(d)) < int(sys.argv[2]) and time.time() < end: time.sleep(0.02)\n"
            "peak = len(os.listdir(d)); time.sleep(0.2); os.remove(me)\n"
            "sys.exit('%d at once' % peak) if peak > int(sys.argv[3]) else print('peak %d' % peak)")
    return [PY, "-c", code, "{board}", str(size), str(limit)]


class ParallelTest(Base):
    """Several runs of one agent at once. (No braces in fake agents' code: the dispatcher fills {placeholders}.)"""
    run_jobs = JobsTest.run_jobs

    def test_one_agent_runs_several_tasks_at_once(self):
        ids = [board.add(self.conn, f"task {i}", to="alpha", strict=True) for i in range(3)]
        self.run_jobs({"alpha": {"cmd": crowd(3, 3), "parallel": 3}}, jobs=5)
        self.assertEqual({db.get(self.conn, t)["result"] for t in ids}, {"peak 3"})

    def test_parallel_is_a_ceiling(self):
        ids = [board.add(self.conn, f"task {i}", to="alpha", strict=True) for i in range(4)]
        self.run_jobs({"alpha": {"cmd": crowd(3, 2), "parallel": 2}}, jobs=5)
        for t in ids:
            self.assertEqual(self.status(t), "done")
            self.assertEqual(db.get(self.conn, t)["result"], "peak 2")

    def test_jobs_still_caps_the_total(self):
        ids = [board.add(self.conn, f"task {i}", to="alpha", strict=True) for i in range(2)]
        self.run_jobs({"alpha": {"cmd": crowd(2, 1), "parallel": 5}}, jobs=1)
        self.assertEqual({db.get(self.conn, t)["result"] for t in ids}, {"peak 1"})


class AgentsCommandTest(Base):
    def test_shows_parallel_per_agent(self):
        path = Path(self.tmp.name) / "agents.json"
        path.write_text('{"alpha": {"cmd": ["x"], "cmd_edit": ["x"], "parallel": 3}, "beta": {"cmd": ["x"]}}')
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"NEXTRUNNER_DB": self.db_path, "NEXTRUNNER_AGENTS": str(path)}), \
                contextlib.redirect_stdout(out):
            cli.main(["agents"])
        lines = out.getvalue().splitlines()
        self.assertRegex(lines[0], r"^alpha\s+read\+edit\s+parallel 3\s+up$")
        self.assertRegex(lines[1], r"^beta\s+read only\s+parallel 1\s+up$")

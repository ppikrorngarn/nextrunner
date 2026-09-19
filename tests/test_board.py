"""Tests for nextrunner. Run: python3 -m unittest -v   (from the repo root, with src on the path: PYTHONPATH=src, or uv run)"""
import contextlib
import io
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nextrunner import agents, board, cli, db, dispatcher

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

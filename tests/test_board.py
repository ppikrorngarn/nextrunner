"""Tests for nextrunner. Run: python3 -m unittest -v   (from the repo root, with src on the path: PYTHONPATH=src, or uv run)"""
import multiprocessing
import tempfile
import time
import unittest
from pathlib import Path

from nextrunner import board, db

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


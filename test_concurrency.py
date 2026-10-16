"""Session files reject stale writers and concurrent turns before external effects."""
import json
import os
import threading
import unittest
from unittest import mock

import cg
from test_loop import Base, message, response, tool_call


class Concurrency(Base):
    def _two_sessions(self, daily):
        self.cfg["daily_budget_usd"] = daily
        self.cfg["account_daily_budget_usd"] = 100
        owner, contender = self.session(max_usd=10), self.session(max_usd=10)
        inside, release = threading.Semaphore(0), threading.Event()
        calls, errors = [], []

        def api(payload, on_text=None):
            calls.append(payload)
            inside.release()
            if not release.wait(5):
                raise TimeoutError("test timed out waiting for release")
            return response(message("done"))

        cg.call_api = api

        def run(session, label):
            try:
                session.turn(label)
            except BaseException as e:
                errors.append(e)
        threads = [threading.Thread(target=run, args=(x, n)) for x, n in ((owner, "first"), (contender, "second"))]
        return owner, threads, inside, release, calls, errors

    def test_a_call_in_flight_counts_against_another_sessions_budget(self):
        worst = cg.cost(self.session().price, 0, 0, self.session().max_out)
        owner, (first, second), inside, release, calls, errors = self._two_sessions(daily=worst * 1.6)
        first.start()
        try:
            self.assertTrue(inside.acquire(timeout=5))         # the first call is in flight
            second.start()
            second.join(5)                                     # refused at once: it does not wait
            self.assertFalse(second.is_alive())
        finally:
            release.set()
            first.join(5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], cg.Budget)
        self.assertEqual(cg.read_reservations(), {})          # nothing left reserved

    def test_sessions_with_room_call_side_by_side(self):
        owner, (first, second), inside, release, calls, errors = self._two_sessions(daily=50)
        first.start()
        second.start()
        try:
            # Both calls are in flight at the same time: neither waits for the other's reply.
            self.assertTrue(inside.acquire(timeout=5))
            self.assertTrue(inside.acquire(timeout=5))
        finally:
            release.set()
            first.join(5)
            second.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(cg.ledger_entries()), 2)
        self.assertEqual(cg.read_reservations(), {})

    def test_a_crashed_sessions_reservation_does_not_block_forever(self):
        self.cfg["daily_budget_usd"] = 1.0
        self.cfg["account_daily_budget_usd"] = 100
        cg.write_reservations({"dead": {"usd": 5.0, "ts": cg.time.time(), "pid": 2 ** 22 + 7},
                               "old": {"usd": 5.0, "ts": cg.time.time() - 3600, "pid": os.getpid()}})
        self.assertEqual(cg.reserved_elsewhere(), 0)

    def test_locked_budget_check_includes_new_tool_output(self):
        self.cfg["daily_budget_usd"] = 0.4
        self.cfg["account_daily_budget_usd"] = 100
        s = self.session(response(message("unused")), max_usd=10)
        s.last_in = 1000
        s.guard()
        # The recheck uses this same new content; do not silently omit it while
        # waiting for another session's API call to finish.
        with self.assertRaisesRegex(cg.Budget, "daily budget"):
            s.call(s.input, s.instructions, budget_extra="large " * 300000)
        self.assertEqual(self.api.requests, [])

    def test_explicit_resume_id_cannot_switch_to_a_different_workspace(self):
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        foreign = self.session()
        foreign.workspace = other
        foreign.save()
        local = self.session()
        local.id = foreign.id[:-1] + ("0" if foreign.id[-1] != "0" else "1")
        local.path = os.path.join(cg.RUNS_DIR, local.id + ".json")
        local.save()
        with mock.patch.object(cg, "claude_sessions", return_value=[]):
            self.assertEqual(cg.resolve_start(self.ws, resume=local.id[:13]), (local.id, None))
            with self.assertRaisesRegex(SystemExit, "no session .* for this directory"):
                cg.resolve_start(self.ws, resume=foreign.id)
            sibling = self.session()
            sibling.id = local.id + "-sibling"
            sibling.path = os.path.join(cg.RUNS_DIR, sibling.id + ".json")
            sibling.save()
            self.assertEqual(cg.resolve_start(self.ws, resume=local.id), (local.id, None))
            with self.assertRaisesRegex(SystemExit, "matches more than one session"):
                cg.resolve_start(self.ws, resume=local.id[:13])

    def test_stale_save_and_turn_do_not_overwrite_or_call_model(self):
        first = self.session()
        first.save()
        second = self.session(response(message("should not run")), resume=first.id)
        first.todos.append("newer state")
        first.save()
        with self.assertRaisesRegex(cg.ApiError, "changed since it was loaded"):
            second.save()
        before = list(second.input)
        with self.assertRaisesRegex(cg.ApiError, "changed since it was loaded"):
            second.turn("stale request")
        self.assertEqual(second.input, before)
        self.assertEqual(self.api.requests, [])
        with open(first.path) as f:
            self.assertEqual(json.load(f)["todos"], ["newer state"])

    def test_busy_turn_does_not_call_model_or_run_tools(self):
        owner = self.session()
        owner.save()
        contender = self.session(response(tool_call("write", path="bad.txt", content="bad")), resume=owner.id)
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def blocked_call(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test timed out waiting for release")
            return response(message("done"))

        owner.call = blocked_call
        def run():
            try:
                owner.turn("first")
            except BaseException as e:
                errors.append(e)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaisesRegex(cg.ApiError, "in use"):
                contender.turn("second")
            self.assertEqual(self.api.requests, [])
            self.assertFalse(os.path.exists(os.path.join(self.ws, "bad.txt")))
            self.assertEqual(contender.input, [])
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_old_format_record_and_repeated_turns(self):
        original = self.session()
        os.makedirs(cg.RUNS_DIR, exist_ok=True)
        with open(original.path, "w") as f:
            json.dump({"id": original.id, "workspace": self.ws, "model": original.model,
                       "spent": 0, "input": []}, f, indent=2)
        resumed = self.session(response(message("one")), response(message("two")), resume=original.id)
        self.assertEqual(resumed.turn("hello"), "one")
        self.assertEqual(resumed.turn("again"), "two")
        self.assertEqual(len(self.api.requests), 2)
        with open(resumed.path) as f:
            self.assertEqual(len([i for i in json.load(f)["input"] if i.get("type") == "message"]), 2)

    def test_legacy_unanswered_tool_is_not_assumed_unstarted(self):
        original = self.session()
        os.makedirs(cg.RUNS_DIR, exist_ok=True)
        with open(original.path, "w") as f:
            json.dump({"id": original.id, "workspace": self.ws, "model": original.model,
                       "spent": 0, "input": [tool_call("write", "c1", path="a.txt", content="changed")]}, f)
        resumed = self.session(resume=original.id)
        self.assertIn("outcome unknown", resumed.input[-1]["output"])
        self.assertNotIn("not run", resumed.input[-1]["output"])

    def test_new_session_collision_is_rejected_and_temps_are_unique(self):
        one = self.session()
        one.save()
        two = self.session()
        self.assertNotEqual(one.id, two.id)
        collision = self.session()
        collision.id, collision.path = one.id, one.path  # simulate an ID collision
        with self.assertRaisesRegex(cg.ApiError, "changed since it was loaded"):
            collision.save()
        paths = []
        mkstemp = cg.tempfile.mkstemp
        def track_temp(*args, **kwargs):
            fd, path = mkstemp(*args, **kwargs)
            paths.append(path)
            return fd, path
        with mock.patch.object(cg.tempfile, "mkstemp", side_effect=track_temp):
            one.save()
            one.save()
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(not os.path.exists(path) for path in paths))

    def test_stale_compact_refuses_before_api_call(self):
        owner = self.session()
        owner.save()
        stale = self.session(response(message("unused")), resume=owner.id)
        owner.todos.append("new state")
        owner.save()
        with self.assertRaisesRegex(cg.ApiError, "changed since it was loaded"):
            stale.compact()
        self.assertEqual(self.api.requests, [])

    def test_stale_handback_cannot_publish_old_state(self):
        owner = self.session()
        owner.claude_session = os.path.join(self.ws, "claude.jsonl")
        owner.input.append(message("first"))
        owner.save()
        stale = self.session(resume=owner.id)
        owner.input.append(message("second"))
        owner.save()
        with mock.patch.object(cg, "HOME", self.tmp.name):
            with self.assertRaisesRegex(cg.ApiError, "changed since it was loaded"):
                cg.write_handback(stale)
            self.assertFalse(os.path.exists(cg.handback_path(self.ws)))


if __name__ == "__main__":
    unittest.main()

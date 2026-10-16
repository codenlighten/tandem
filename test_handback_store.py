"""Regression tests for cross-workspace isolation and handback update delivery."""
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import cg
import handback_store
import on_prompt
from test_loop import Base, message, response


class HandbackStorage(Base):
    def setUp(self):
        super().setUp()
        self.old_home, cg.HOME = cg.HOME, self.tmp.name
        self.hook = os.path.join(os.path.dirname(__file__), "on_prompt.py")

    def tearDown(self):
        cg.HOME = self.old_home
        super().tearDown()

    def prompt(self, workspace, session):
        r = subprocess.run([sys.executable, self.hook],
                           input=json.dumps({"cwd": workspace, "session_id": session}),
                           text=True, capture_output=True, check=True,
                           env={**os.environ, "HOME": self.tmp.name})
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout else None

    def record(self, workspace, text, path=None):
        path = path or cg.handback_path(workspace)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handback_store.write(path, {"workspace": workspace, "record": text, "time_end": time.time(),
                                    "revision": 1, "delivered": {}, "gpt_session": "test"})
        return path

    def test_distinct_workspaces_do_not_share_a_handback(self):
        a = os.path.join(self.tmp.name, "a-b", "c")
        b = os.path.join(self.tmp.name, "a", "b-c")
        for path in (a, b):
            os.makedirs(path)
        self.assertEqual(handback_store.legacy_path(a, self.tmp.name),
                         handback_store.legacy_path(b, self.tmp.name))
        self.assertNotEqual(cg.handback_path(a), cg.handback_path(b))
        self.record(a, "only A")
        self.assertIsNone(self.prompt(b, "session-b"))
        self.record(b, "only B")
        self.assertIn("only B", self.prompt(b, "session-b"))
        self.assertIn("only A", self.prompt(a, "session-a"))

    def test_more_than_eight_sessions_can_each_receive_a_record(self):
        self.record(self.ws, "available")
        for i in range(9):
            self.assertIn("available", self.prompt(self.ws, f"reader-{i}"))
        self.assertIsNone(self.prompt(self.ws, "reader-0"))

    def test_colliding_legacy_record_is_never_delivered(self):
        a = os.path.join(self.tmp.name, "a-b", "c")
        b = os.path.join(self.tmp.name, "a", "b-c")
        for path in (a, b):
            os.makedirs(path)
        self.record(a, "private A", handback_store.legacy_path(a, self.tmp.name))
        self.assertIsNone(self.prompt(b, "session-b"))
        self.assertIn("private A", self.prompt(a, "session-a"))

    def test_new_turn_is_delivered_again_but_unchanged_record_is_not(self):
        s = self.session(response(message("first")), response(message("second")))
        s.claude_session = "original.jsonl"
        self.turn(s, "first request")
        cg.write_handback(s)
        self.assertIn("first", self.prompt(self.ws, "original"))
        self.assertIsNone(self.prompt(self.ws, "original"))
        self.turn(s, "second request")
        cg.write_handback(s)
        self.assertIn("second", self.prompt(self.ws, "original"))
        self.assertIsNone(self.prompt(self.ws, "original"))

    def test_new_turn_delivered_even_when_rendered_record_does_not_change(self):
        s = self.session(response(message("first")), response(message("second")))
        s.claude_session = "original.jsonl"
        with mock.patch.object(cg, "handback_prompt", return_value="clipped"):
            self.turn(s, "first request")
            cg.write_handback(s)
            self.assertIn("clipped", self.prompt(self.ws, "original"))
            self.turn(s, "second request")
            cg.write_handback(s)
        update = self.prompt(self.ws, "original")          # the new turn, not the whole record again
        self.assertIn("second request", update)
        self.assertNotIn("first request", update)

    def test_migration_preserves_legacy_delivery_and_prefers_new_record(self):
        s = self.session(response(message("new reply")))
        s.claude_session = "original.jsonl"
        old = handback_store.legacy_path(self.ws, self.tmp.name)
        self.record(self.ws, "old reply", old)
        self.assertIn("old reply", self.prompt(self.ws, "original"))
        self.turn(s, "new request")
        cg.write_handback(s)
        self.assertIn("new reply", self.prompt(self.ws, "original"))
        self.assertIsNone(self.prompt(self.ws, "original"))

    def test_two_sessions_retain_unread_work_and_independent_revisions(self):
        a = self.session(response(message("reply from A")), response(message("A again")))
        a.claude_session = "original.jsonl"
        self.turn(a, "do A")
        cg.write_handback(a)
        self.assertIn("reply from A", self.prompt(self.ws, "reader-1"))
        b = self.session(response(message("reply from B")))
        b.claude_session = "original.jsonl"
        self.turn(b, "do B")
        cg.write_handback(b)
        self.assertIn("reply from B", self.prompt(self.ws, "reader-1"))
        self.assertIsNone(self.prompt(self.ws, "reader-1"))
        pending = self.prompt(self.ws, "reader-2")
        self.assertIn("reply from A", pending)
        self.assertIn("reply from B", pending)
        self.assertLess(pending.index("reply from A"), pending.index("reply from B"))
        self.assertIsNone(self.prompt(self.ws, "reader-2"))
        a_api = response(message("A again"))
        cg.call_api = lambda payload, on_text=None: a_api
        self.turn(a, "do more A")
        cg.write_handback(a)
        update = self.prompt(self.ws, "reader-2")
        self.assertIn("A again", update)
        self.assertNotIn("reply from B", update)
        self.assertIsNone(self.prompt(self.ws, "reader-2"))
        self.assertIn("reply from B", self.prompt(self.ws, "reader-3"))

    def test_relay_delivery_marks_only_its_archived_session(self):
        a = self.session(response(message("A")))
        a.claude_session = "original.jsonl"
        self.turn(a, "do A")
        cg.write_handback(a)
        b = self.session(response(message("B")))
        b.claude_session = "original.jsonl"
        self.turn(b, "do B")
        cg.write_handback(b)
        cg.mark_handback_delivered(self.ws, a.id, "reader")
        pending = self.prompt(self.ws, "reader")
        self.assertIn("B", pending)
        self.assertNotIn("## What the user asked\n1. do A", pending)
        self.assertIsNone(self.prompt(self.ws, "reader"))
        self.assertIn("do A", self.prompt(self.ws, "another reader"))

    def test_output_failure_does_not_mark_handback_delivered(self):
        path = self.record(self.ws, "pending message")
        event = json.dumps({"cwd": self.ws, "session_id": "reader"})
        with mock.patch.dict(os.environ, {"HOME": self.tmp.name}), \
                mock.patch.object(on_prompt.sys, "stdin", io.StringIO(event)), \
                mock.patch("builtins.print", side_effect=OSError("broken stdout")):
            on_prompt.main()
        self.assertNotIn("reader", handback_store.deliveries(handback_store.read(path)))
        self.assertIn("pending message", self.prompt(self.ws, "reader"))

    def test_bad_archive_does_not_hide_a_valid_handback(self):
        self.record(self.ws, "active handback")
        archived = handback_store.archive_path(cg.handback_path(self.ws), "older")
        with open(archived, "w") as f:
            f.write("{not json")
        self.assertIn("active handback", self.prompt(self.ws, "reader"))

    def test_hook_chooses_new_file_if_legacy_is_migrated_while_waiting(self):
        legacy = handback_store.legacy_path(self.ws, self.tmp.name)
        self.record(self.ws, "legacy", legacy)
        path = cg.handback_path(self.ws)
        seen = []
        with handback_store.locked(path), handback_store.locked(legacy):
            worker = threading.Thread(target=lambda: seen.append(self.prompt(self.ws, "reader")))
            worker.start()
            time.sleep(0.15)
            self.assertTrue(worker.is_alive())
            self.record(self.ws, "migrated")
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(seen), 1)
        self.assertIn("migrated", seen[0])
        self.assertIsNone(self.prompt(self.ws, "reader"))

    def test_symlink_workspace_uses_the_same_record(self):
        link = os.path.join(self.tmp.name, "alias")
        os.symlink(self.ws, link)
        self.record(self.ws, "canonical")
        self.assertIn("canonical", self.prompt(link, "reader"))

    def test_hook_waits_for_writer_and_does_not_restore_stale_record(self):
        path = self.record(self.ws, "old")
        seen = []
        with handback_store.locked(path):
            worker = threading.Thread(target=lambda: seen.append(self.prompt(self.ws, "reader")))
            worker.start()
            time.sleep(0.15)
            self.assertTrue(worker.is_alive())
            record = handback_store.read(path)
            record["record"] = "new"
            record["revision"] += 1
            handback_store.write(path, record)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(seen), 1)
        self.assertIn("new", seen[0])
        self.assertEqual(handback_store.read(path)["record"], "new")


if __name__ == "__main__":
    unittest.main()

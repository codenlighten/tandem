"""The saved agent loop must not replay a tool whose result was lost on interruption."""
import json
import os
import unittest
from unittest import mock

import cg
from test_loop import Base, message, response, tool_call


class Recovery(Base):
    def test_crash_during_model_call_preserves_request_and_warns_before_retry(self):
        def crash(payload):
            with open(s.path) as f:
                saved = json.load(f)
            self.assertEqual(saved["input"], [{"role": "user", "content": "important request"}])
            self.assertTrue(saved["pending_response"])
            raise SystemExit("reply lost after dispatch")

        s = self.session(crash)
        with self.assertRaises(SystemExit):
            self.turn(s, "important request")
        with mock.patch.object(cg, "say") as announce:
            recovered = self.session(response(message("done")), resume=s.id)
        self.assertTrue(recovered.pending_response)
        self.assertIn("no reply is saved", announce.call_args[0][0])
        self.assertEqual(recovered.input, [{"role": "user", "content": "important request"}])
        self.turn(recovered)
        self.assertEqual(self.api.requests[0]["input"], recovered.input[:1])
        with open(s.path) as f:
            self.assertFalse(json.load(f)["pending_response"])
        with mock.patch.object(cg, "say") as announce:
            self.session(resume=s.id)
        self.assertFalse(any("no reply is saved" in call.args[0] for call in announce.call_args_list))

    def test_compaction_crash_retains_original_conversation(self):
        s = self.session(SystemExit("compaction interrupted"))
        s.input = [{"role": "user", "content": "prior work"}]
        s.save()
        with self.assertRaises(SystemExit):
            s.compact()
        with open(s.path) as f:
            saved = json.load(f)
        self.assertEqual(saved["input"], s.input)
        self.assertTrue(saved["pending_response"])

    def test_takeover_budget_after_tool_does_not_erase_work(self):
        s = self.session(response(tool_call("write", path="changed.txt", content="done"),
                                  tokens=(100000, 20000)),
                         response(message("not reached")), max_usd=0.6)
        source = os.path.join(self.ws, "origin.jsonl")
        with mock.patch.object(cg, "handoff_message", return_value=("original request", source)), \
                mock.patch.object(cg, "takeover_snapshot", return_value="snapshot-ref"), \
                mock.patch.object(cg, "claude_processes", return_value=[]), \
                mock.patch.object(cg, "approve_extra", return_value=False):
            cg.take_over(s, "latest")
        self.assertEqual(len(self.api.requests), 1)
        self.assertEqual(s.claude_session, source)
        self.assertEqual(s.snapshot, "snapshot-ref")
        self.assertEqual([i["type"] for i in s.input if i.get("type")],
                         ["function_call", "function_call_output"])
        with open(os.path.join(self.ws, "changed.txt")) as f:
            self.assertEqual(f.read(), "done")
        s.save()  # the final save made by run_task or run_chat must not erase the record
        resumed = self.session(resume=s.id)
        self.assertEqual(resumed.claude_session, source)
        self.assertEqual(resumed.input, s.input)
        self.assertIn("wrote changed.txt", resumed.input[-1]["output"])
        with mock.patch.object(cg, "HOME", self.tmp.name):
            handback = cg.write_handback(resumed)
            with open(handback) as f:
                self.assertIn("changed.txt", json.load(f)["record"])

    def test_refused_takeover_restores_previous_state_without_calls(self):
        s = self.session(response(message("not reached")), max_usd=0.001)
        s.input = [{"role": "user", "content": "earlier work"}]
        s.claude_session = "earlier.jsonl"
        s.snapshot = "earlier-ref"
        s.save()
        with mock.patch.object(cg, "handoff_message", return_value=("new request", "new.jsonl")), \
                mock.patch.object(cg, "takeover_snapshot", return_value="new-ref"), \
                mock.patch.object(cg, "claude_processes", return_value=[]), \
                mock.patch.object(cg, "approve_extra", return_value=False):
            cg.take_over(s, "latest")
        self.assertEqual(self.api.requests, [])
        self.assertEqual(s.input, [{"role": "user", "content": "earlier work"}])
        self.assertEqual((s.claude_session, s.snapshot), ("earlier.jsonl", "earlier-ref"))
        s.save()
        self.assertEqual(self.session(resume=s.id).input, s.input)

    def test_failed_first_takeover_call_does_not_persist_a_false_takeover(self):
        for error in (cg.ApiError("API rejected the request"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                s = self.session(error)
                s.input = [{"role": "user", "content": "earlier work"}]
                s.claude_session = "earlier.jsonl"
                s.snapshot = "earlier-ref"
                s.save()
                with mock.patch.object(cg, "handoff_message", return_value=("new request", "new.jsonl")), \
                        mock.patch.object(cg, "takeover_snapshot", return_value="new-ref"), \
                        mock.patch.object(cg, "claude_processes", return_value=[]):
                    cg.take_over(s, "latest")
                self.assertEqual((s.input, s.claude_session, s.snapshot),
                                 ([{"role": "user", "content": "earlier work"}], "earlier.jsonl", "earlier-ref"))
                self.assertEqual(s.calls, 0)
                s.save()
                recovered = self.session(resume=s.id)
                self.assertEqual((recovered.input, recovered.claude_session, recovered.snapshot),
                                 (s.input, s.claude_session, s.snapshot))

    def test_resume_reconciles_ledgered_call_lost_before_session_save(self):
        s = self.session(response(message("reply lost in crash"), tokens=(100000, 20000)))
        s.input.append({"role": "user", "content": "the original request"})
        s.save()
        s.call(s.input, s.instructions)
        with open(s.path) as f:
            self.assertEqual(json.load(f)["spent"], 0)
        resumed = self.session(resume=s.id)
        self.assertAlmostEqual(resumed.spent, 0.4)
        self.assertAlmostEqual(resumed.spent_before, 0.4)
        self.assertEqual(resumed.input, [{"role": "user", "content": "the original request"}])
        self.assertTrue(resumed.pending_response)  # call() returned, but no reply was saved in input
        resumed.save()
        again = self.session(resume=s.id)
        self.assertAlmostEqual(again.spent, 0.4)  # no duplicate accounting on resume

    def test_resume_counts_both_ledgers_without_discarding_saved_spend(self):
        s = self.session(response(message("ordinary"), tokens=(100000, 20000)),
                         response(message("approved"), tokens=(100000, 20000)), max_usd=5)
        s.save()
        s.call(s.input, s.instructions)
        s.approved_extra = 1.0
        s.call(s.input, s.instructions)
        self.assertEqual(len(cg.ledger_entries(cg.EXTRA_LEDGER_PATH)), 1)
        r = self.session(resume=s.id)
        self.assertAlmostEqual(r.spent, 0.8)
        r.spent = 1.0  # a partial/rotated ledger must not lower a saved session total
        r.save()
        self.assertEqual(self.session(resume=s.id).spent, 1.0)

    def test_crash_in_a_tool_never_replays_it_or_later_calls(self):
        s = self.session(response(tool_call("write", "one", path="changed.txt", content="done"),
                                  tool_call("write", "two", path="later.txt", content="later")))
        def crash(call):
            with open(os.path.join(self.ws, "changed.txt"), "w") as f:
                f.write("done")
            raise SystemExit("simulated crash after side effect")
        s.run_tool = crash
        with self.assertRaises(SystemExit):
            self.turn(s, "do both")
        with open(s.path) as f:
            saved = json.load(f)
        self.assertEqual(saved["inflight"], "one")
        self.assertFalse(any(i.get("type") == "function_call_output" for i in saved["input"]))
        recovered = self.session(response(message("I will inspect before retrying.")), resume=s.id)
        outs = [i for i in recovered.input if i.get("type") == "function_call_output"]
        self.assertEqual([i["call_id"] for i in outs], ["one", "two"])
        self.assertIn("outcome unknown", outs[0]["output"])
        self.assertIn("not run", outs[1]["output"])
        self.assertFalse(os.path.exists(os.path.join(self.ws, "later.txt")))
        self.assertIn("inspect", self.turn(recovered, "continue")[0])

    def test_completed_tool_output_is_saved_before_next_tool_starts(self):
        s = self.session(response(tool_call("read", "one", path="a.txt"),
                                  tool_call("write", "two", path="changed.txt", content="done"),
                                  tool_call("read", "three", path="a.txt")))
        original = s.run_tool
        def maybe_crash(call):
            if call["call_id"] == "two":
                with open(os.path.join(self.ws, "changed.txt"), "w") as f:
                    f.write("done")
                raise SystemExit("crash")
            return original(call)
        s.run_tool = maybe_crash
        with self.assertRaises(SystemExit):
            self.turn(s, "go")
        recovered = self.session(response(message("done")), resume=s.id)
        outs = [i for i in recovered.input if i.get("type") == "function_call_output"]
        self.assertEqual([i["call_id"] for i in outs], ["one", "two", "three"])
        self.assertIn("alpha", outs[0]["output"])
        self.assertIn("outcome unknown", outs[1]["output"])
        self.assertIn("not run", outs[2]["output"])

    def test_interrupt_during_result_save_does_not_duplicate_a_tool_output(self):
        s = self.session(response(tool_call("read", "one", path="a.txt"),
                                  tool_call("read", "two", path="a.txt")))
        original_save = s.save
        interrupted = False
        def interrupted_save():
            nonlocal interrupted
            if not interrupted and s.inflight is None and any(
                    i.get("type") == "function_call_output" for i in s.input):
                interrupted = True  # first result appended, before its save
                raise KeyboardInterrupt()
            return original_save()
        s.save = interrupted_save
        with self.assertRaises(KeyboardInterrupt):
            self.turn(s, "go")
        with open(s.path) as f:
            saved = json.load(f)
        outs = [i for i in saved["input"] if i.get("type") == "function_call_output"]
        self.assertEqual([i["call_id"] for i in outs], ["one", "two"])
        self.assertIn("alpha", outs[0]["output"])
        self.assertIn("not run", outs[1]["output"])

    def test_observer_failure_does_not_break_execution(self):
        s = self.session(response(tool_call("read", path="a.txt")), response(message("done")))
        calls = []
        def broken(kind, fields):
            calls.append(kind)
            raise ValueError("recorder broke")
        s.on_event = broken
        result, shown = self.turn(s, "go")
        self.assertEqual(result, "done")
        self.assertEqual(calls, ["tool_call"])
        self.assertIn("event recorder failed", shown)
        self.assertEqual(s.input[-2]["type"], "function_call_output")

    def test_events_preserve_interleaved_response_item_order(self):
        s = self.session(response(tool_call("read", path="a.txt"), message("I will read it.")),
                         response(message("done")))
        seen = []
        s.on_event = lambda kind, fields: seen.append(kind)
        self.turn(s, "go")
        self.assertEqual(seen, ["tool_call", "assistant_message", "tool_result", "assistant_message"])

    def test_duplicate_call_ids_are_rejected_before_running_tools(self):
        s = self.session(response(tool_call("read", "same", path="a.txt"),
                                  tool_call("write", "same", path="changed.txt", content="bad")))
        with self.assertRaisesRegex(cg.ApiError, "duplicate tool call IDs"):
            self.turn(s, "go")
        self.assertFalse(os.path.exists(os.path.join(self.ws, "changed.txt")))


if __name__ == "__main__":
    unittest.main()

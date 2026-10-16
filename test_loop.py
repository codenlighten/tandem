#!/usr/bin/env python3
"""Controls for the agent loop with the model replaced by a script, so no API call is
made and nothing is spent: tool round trips, cost guards, interrupts, compaction,
overflow recovery, streaming, save and resume.

  python3 test_loop.py < /dev/null
"""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

os.environ["OPENAI_API_KEY"] = "not-a-real-key-tests-make-no-calls"   # never the real one

import added_tools
import cg

# Tools approved on this machine must not change what these tests see or run.
added_tools.STORE = tempfile.mkdtemp(prefix="cg-no-tools.")


def message(text):
    return {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}


def tool_call(tool, call_id="c1", **args):
    return {"type": "function_call", "name": tool, "call_id": call_id,
            "arguments": json.dumps(args)}


def response(*items, tokens=(1000, 100), status="completed"):
    return {"status": status, "output": list(items),
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1],
                      "input_tokens_details": {"cached_tokens": 0}}}


class FakeApi:
    """Stands in for cg.call_api: replays scripted responses and records each request."""

    def __init__(self, *script):
        self.script, self.requests = list(script), []

    def __call__(self, payload, on_text=None, retries=3):
        self.requests.append(json.loads(json.dumps(payload)))
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            step = step(payload)
        if on_text:
            for item in step["output"]:
                for c in item.get("content") or []:
                    on_text(c["text"])
        return step


class Base(unittest.TestCase):
    """A temporary workspace, ledger and runs folder, and a scripted model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cgt.")
        self.ws = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.ws)
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.write("alpha\n")
        self.saved = (cg.call_api, cg.LEDGER_PATH, cg.RUNS_DIR)
        cg.LEDGER_PATH = os.path.join(self.tmp.name, "ledger.jsonl")
        self.saved_extra, cg.EXTRA_LEDGER_PATH = cg.EXTRA_LEDGER_PATH, os.path.join(self.tmp.name, "ledger-extra.jsonl")
        cg.RUNS_DIR = os.path.join(self.tmp.name, "runs")
        self.cfg = cg.load_config()
        self.cfg["other_ledgers"] = []
        # A stand-in for gpt-assist, so the second-opinion tool exists whether or not the
        # real one is installed beside this project.
        self.cfg["gpt_assist"] = os.path.join(self.tmp.name, "assist.py")
        with open(self.cfg["gpt_assist"], "w") as f:
            f.write("# stand-in\n")

    def tearDown(self):
        cg.call_api, cg.LEDGER_PATH, cg.RUNS_DIR = self.saved
        cg.EXTRA_LEDGER_PATH = self.saved_extra
        self.tmp.cleanup()

    def session(self, *script, max_usd=1.0, resume=None, **cfg):
        self.api = cg.call_api = FakeApi(*script)
        self.cfg.update(cfg)
        args = argparse.Namespace(cwd=self.ws, mode="auto", net=False, tier="standard",
                                  model=None, max_usd=max_usd, shm=False,
                                  memory_dir=os.path.join(self.tmp.name, "memory"))
        return cg.Session(self.cfg, args, resume=resume)

    def turn(self, s, text=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = s.turn(text)
        return result, out.getvalue()



class Loop(Base):
    def test_tool_round_trip(self):
        s = self.session(response(tool_call("read", path="a.txt")), response(message("It says alpha.")))
        result, shown = self.turn(s, "what is in a.txt?")
        self.assertEqual(result, "It says alpha.")
        sent = self.api.requests[1]["input"]
        self.assertEqual([i.get("type", i.get("role")) for i in sent],
                         ["user", "function_call", "function_call_output"])
        self.assertIn("alpha", sent[2]["output"])
        self.assertEqual(sent[2]["call_id"], "c1")
        self.assertEqual(shown.count("It says alpha."), 1)  # streamed once, not repeated

    def test_a_recorder_is_told_each_message_call_and_result_in_order(self):
        s = self.session(response(message("Let me look."), tool_call("read", path="a.txt")),
                         response(message("It says alpha.")))
        seen = []
        s.on_event = lambda kind, fields: seen.append((kind, fields))
        self.turn(s, "what is in a.txt?")
        self.assertEqual([k for k, _ in seen], ["assistant_message", "tool_call", "tool_result", "assistant_message"])
        self.assertEqual((seen[0][1]["final"], seen[3][1]), (False, {"text": "It says alpha.", "final": True}))
        self.assertEqual((seen[1][1]["call_id"], seen[1][1]["tool"], json.loads(seen[1][1]["arguments"])),
                         ("c1", "read", {"path": "a.txt"}))
        self.assertEqual(seen[2][1]["call_id"], "c1")
        self.assertIn("alpha", seen[2][1]["output"])

    def test_request_shape(self):
        s = self.session(response(message("hi")))
        self.turn(s, "hello")
        req = self.api.requests[0]
        self.assertIs(req["store"], False)
        self.assertEqual(req["include"], ["reasoning.encrypted_content"])
        self.assertEqual({t["name"] for t in req["tools"]},
                         {"read", "write", "edit", "bash", "glob", "grep", "skill", "todo", "fetch",
                          "ask_gpt", "propose_tool"})
        self.assertIn(self.ws, req["instructions"])
        self.assertNotIn(cg.api_key(), json.dumps(req))

    def test_parallel_calls_each_get_an_output(self):
        s = self.session(response(tool_call("read", "c1", path="a.txt"),
                                  tool_call("glob", "c2", pattern="*.txt"),
                                  tool_call("nope", "c3")), response(message("done")))
        self.turn(s, "go")
        outs = [i for i in self.api.requests[1]["input"] if i.get("type") == "function_call_output"]
        self.assertEqual([o["call_id"] for o in outs], ["c1", "c2", "c3"])
        self.assertIn("no tool named", outs[2]["output"])

    def test_run_cap_stops_before_the_call(self):
        s = self.session(response(message("unused")), max_usd=0.001)
        with self.assertRaises(cg.Budget):
            self.turn(s, "hello")
        self.assertEqual(self.api.requests, [])

    def test_run_cap_stops_a_long_turn(self):
        steps = [response(tool_call("read", f"c{i}", path="a.txt"), tokens=(20000, 2000))
                 for i in range(30)]
        s = self.session(*steps, max_usd=0.50)
        with self.assertRaises(cg.Budget):
            self.turn(s, "loop")
        self.assertLess(s.spent, 0.50)
        self.assertGreater(len(self.api.requests), 1)

    def test_daily_and_account_budgets(self):
        today = cg.dt.date.today().isoformat()
        other = os.path.join(self.tmp.name, "other.jsonl")
        with open(other, "w") as f:
            f.write(json.dumps({"ts": today + "T00:00:00",
                                "cost_usd": self.cfg["account_daily_budget_usd"] - 0.01}) + "\n")
        s = self.session(response(message("x")), other_ledgers=[other], account_reserve_usd=0)
        with self.assertRaisesRegex(cg.Budget, "across tools"):
            self.turn(s, "hello")
        # With a reserve, tandem stops that much earlier and leaves the room to gpt-assist.
        with open(other, "w") as f:
            f.write(json.dumps({"ts": today + "T00:00:00",
                                "cost_usd": self.cfg["account_daily_budget_usd"] - 1.05}) + "\n")
        s = self.session(response(message("fits")), other_ledgers=[other], account_reserve_usd=0)
        self.assertEqual(self.turn(s, "hello")[0], "fits")
        s = self.session(response(message("x")), other_ledgers=[other], account_reserve_usd=1.0)
        with self.assertRaisesRegex(cg.Budget, "reserve for gpt-assist"):
            self.turn(s, "hello")
        with open(cg.LEDGER_PATH, "w") as f:
            f.write(json.dumps({"ts": today + "T00:00:00",
                                "cost_usd": self.cfg["daily_budget_usd"] - 0.01}) + "\n")
        s = self.session(response(message("x")), other_ledgers=[])
        with self.assertRaisesRegex(cg.Budget, "daily budget"):
            self.turn(s, "hello")

    def over_the_limit(self):
        with open(cg.LEDGER_PATH, "w") as f:
            f.write(json.dumps({"ts": cg.dt.date.today().isoformat() + "T00:00:00", "cost_usd": 99.0,
                                "session": "earlier", "model": "gpt-6-sol"}) + "\n")

    def test_spending_the_user_approved_goes_ahead_and_is_logged_apart(self):
        self.over_the_limit()
        s = self.session(response(message("unused")))
        with self.assertRaisesRegex(cg.Budget, "daily budget"):
            self.turn(s, "hello")
        self.assertEqual(self.api.requests, [])
        s.api = cg.call_api = FakeApi(response(message("went ahead"), tokens=(100000, 20000)),   # $0.40
                                      response(message("unused")))
        s.approved_extra = 0.60
        s.run_cap = 5.0
        self.assertEqual(self.turn(s)[0], "went ahead")      # the message refused before is answered now
        self.assertEqual(len(cg.ledger_entries()), 1)        # the ledger other tools count is untouched
        extra = cg.ledger_entries(cg.EXTRA_LEDGER_PATH)
        self.assertEqual((len(extra), extra[0]["cost_usd"]), (1, 0.4))
        self.assertAlmostEqual(s.extra_spent, 0.40)
        with self.assertRaisesRegex(cg.Budget, "you approved beyond today's limits is nearly used"):
            self.turn(s, "and more")                          # $0.20 left, the next call could cost more

    def test_ledger_and_spend(self):
        s = self.session(response(message("x"), tokens=(1_000_000, 100_000)), max_usd=5)
        self.cfg["daily_budget_usd"] = self.cfg["account_daily_budget_usd"] = 100
        self.turn(s, "hello")
        self.assertAlmostEqual(s.spent, 2.0 + 1.0)  # gpt-6-sol: $2 per 1M in, $10 per 1M out
        row = cg.ledger_entries()[-1]
        self.assertEqual((row["session"], row["cost_usd"]), (s.id, 3.0))

    def test_step_limit(self):
        steps = [response(tool_call("read", f"c{i}", path="a.txt")) for i in range(5)]
        s = self.session(*steps, max_steps_per_turn=3)
        result, shown = self.turn(s, "loop")
        self.assertIsNone(result)
        self.assertIn("stopped after 3 steps", shown)
        self.assertEqual(len(self.api.requests), 3)

    def test_interrupt_leaves_a_valid_conversation(self):
        s = self.session(response(tool_call("read", "c1", path="a.txt"),
                                  tool_call("bash", "c2", command="true")),
                         response(message("carrying on")))
        s.tool_bash = lambda **kw: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.turn(s, "go")
        result, _ = self.turn(s, "continue")
        sent = self.api.requests[1]["input"]
        calls = {i["call_id"] for i in sent if i.get("type") == "function_call"}
        outs = {i["call_id"] for i in sent if i.get("type") == "function_call_output"}
        self.assertEqual(calls, outs)
        self.assertEqual(result, "carrying on")

    def test_compaction(self):
        s = self.session(response(tool_call("read", path="a.txt"), tokens=(200000, 10)),
                         response(message("SUMMARY: read a.txt, it says alpha")),
                         response(message("done")), max_usd=5)
        result, shown = self.turn(s, "go")
        self.assertIn("compacted", shown)
        self.assertNotIn("tools", self.api.requests[1])           # the summary call
        final = self.api.requests[2]["input"]
        self.assertEqual(len(final), 1)
        self.assertIn("SUMMARY", final[0]["content"])

    def test_overflow_recovery(self):
        big = [{"role": "user", "content": "earlier"}]
        for i in range(6):
            big += [tool_call("read", f"c{i}", path="a.txt"),
                    {"type": "function_call_output", "call_id": f"c{i}", "output": "x" * 5000}]
        s = self.session(cg.ApiError("API error 400: context length exceeded"),
                         response(message("SUMMARY")), response(message("recovered")), max_usd=5)
        s.input = big
        result, _ = self.turn(s, "next")
        self.assertEqual(result, "recovered")
        summarised = self.api.requests[1]["input"]
        self.assertLess(len(summarised[2]["output"]), 600)        # old results were cut
        self.assertEqual(len(summarised[-3]["output"]), 5000)     # recent ones kept

    def test_other_api_errors_are_raised(self):
        s = self.session(cg.ApiError("API error 401: bad key"))
        with self.assertRaises(cg.ApiError):
            self.turn(s, "hello")

    def test_cut_off_reply_is_reported(self):
        s = self.session(response(message("partial"), status="incomplete"))
        _, shown = self.turn(s, "hello")
        self.assertIn("cut off", shown)

    def test_save_and_resume(self):
        s = self.session(response(message("first")))
        self.turn(s, "remember 42")
        r = self.session(response(message("second")), resume=s.id)
        self.assertEqual(r.spent, s.spent)
        self.turn(r, "what number?")
        sent = self.api.requests[0]["input"]
        self.assertEqual(sent[0]["content"], "remember 42")
        self.assertEqual(sent[-1]["content"], "what number?")

    def test_resumed_session_gets_a_fresh_run_cap(self):
        s = self.session(response(message("first"), tokens=(100000, 20000)), max_usd=0.60)
        self.turn(s, "big one")                       # spends $0.40 of the $0.60 cap
        self.assertAlmostEqual(s.spent, 0.40)
        s.api = cg.call_api = cg.call_api             # same invocation: $0.40 + a worst case of
        with self.assertRaises(cg.Budget):            # $0.32 would pass the cap
            self.turn(s, "more")
        r = self.session(response(message("second")), max_usd=0.60, resume=s.id)
        result, _ = self.turn(r, "more")              # a new invocation starts from zero
        self.assertEqual(result, "second")
        self.assertAlmostEqual(r.spent - r.spent_before, cg.cost(r.price, 1000, 0, 100))

    def test_continue_finds_the_latest_session_for_the_workspace(self):
        a = self.session(response(message("a")))
        self.turn(a, "one")
        os.utime(a.path, (1, 1))
        b = self.session(response(message("b")))
        self.turn(b, "two")
        self.assertEqual(cg.latest_session(self.ws), b.id)
        self.assertIsNone(cg.latest_session("/somewhere/else"))

    def test_task_list(self):
        s = self.session(response(tool_call("todo", items=[{"text": "read the code", "status": "completed"},
                                                         {"text": "fix it", "status": "in_progress"}])),
                         response(message("ok")))
        _, shown = self.turn(s, "go")
        self.assertIn("fix it", shown)
        self.assertIn("2 items, 1 not completed", self.api.requests[1]["input"][-1]["output"])
        r = self.session(response(message("x")), resume=s.id)
        self.assertEqual([i["status"] for i in r.todos], ["completed", "in_progress"])

    def test_fetch_needs_the_network_switch_and_a_public_address(self):
        s = self.session(response(tool_call("fetch", url="https://example.com/")), response(message("ok")))
        self.turn(s, "go")
        self.assertIn("no network", self.api.requests[1]["input"][-1]["output"])
        for url in ("http://localhost/", "http://127.0.0.1:80/x", "https://192.168.0.10/", "http://[::1]/",
                    "https://100.64.0.10/", "http://169.254.169.254/latest/meta-data", "file:///etc/passwd",
                    "ftp://example.com/x", "https://user:pw@example.com/", "https:///nohost",
                    "https://example.com:8443/", "http://example.com:22/"):
            with self.subTest(url=url):
                self.assertIsNotNone(cg.public_url_problem(url))

    def test_fetch_checks_every_redirect(self):
        s = self.session()
        s.net = True
        seen = []

        def fake_run(argv, **kw):
            url = argv[-1]
            seen.append(url)
            pinned = argv[argv.index("--resolve") + 1]
            self.assertIn("93.184.", pinned)           # curl is pinned to the address we checked
            self.assertNotIn("-L", argv)               # and never follows a redirect by itself
            out = b"moved\n302 http://127.0.0.1/admin" if url.endswith("/start") else b"<html><body>hi</body></html>\n200 "
            return cg.subprocess.CompletedProcess(argv, 0, out, b"")

        real_run, real_gai = cg.subprocess.run, cg.socket.getaddrinfo
        cg.subprocess.run = fake_run
        cg.socket.getaddrinfo = lambda host, port, **kw: [(2, 1, 6, "", (
            "127.0.0.1" if host == "127.0.0.1" else "93.184.215.14", port))]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                out = s.tool_fetch("https://example.com/start")
                ok = s.tool_fetch("https://example.com/page")
        finally:
            cg.subprocess.run, cg.socket.getaddrinfo = real_run, real_gai
        self.assertIn("not a public address", out)
        self.assertEqual(seen, ["https://example.com/start", "https://example.com/page"])
        self.assertIn("HTTP 200", ok)
        self.assertTrue(ok.endswith("hi"))

    def test_a_secret_written_mid_session_is_filtered(self):
        s = self.session()
        with contextlib.redirect_stdout(io.StringIO()):
            s.run_tool({"name": "bash", "arguments": json.dumps(
                {"command": "printf 'TOKEN=fresh-secret-value-987654\\n' > .env.local"})})
            out = s.run_tool({"name": "bash", "arguments": json.dumps({"command": "cat .e*"})})
        self.assertNotIn("fresh-secret-value-987654", out)

    def test_a_crashing_tool_does_not_end_the_session(self):
        s = self.session()
        with contextlib.redirect_stdout(io.StringIO()):
            out = s.run_tool({"name": "read", "arguments": json.dumps({"path": "a.txt", "offset": "x"})})
        self.assertIn("error", out)

    def test_second_opinion_tool(self):
        s = self.session()
        self.assertTrue(s.ask_gpt)
        with open(os.path.join(self.ws, ".env"), "w") as f:
            f.write("K=secret-value-123456\n")
        seen = []

        def fake_run(argv, **kw):
            seen.append((argv, kw))
            return cg.subprocess.CompletedProcess(argv, 0, "Verdict: fine.\n", "est...\n[gpt-assist] 10 in + 5 out = $0.0012\n")

        real_run = cg.subprocess.run
        cg.subprocess.run = fake_run
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                out = s.tool_ask_gpt("review", "--allow-expensive", files=["a.txt"], tier="standard")
                refused = s.tool_ask_gpt("ask", "what is in it?", files=[".env"])
                outside = s.tool_ask_gpt("ask", "q", files=["/etc/passwd"])
                bad = s.tool_ask_gpt("ask", "q", tier="deep")
        finally:
            cg.subprocess.run = real_run
        argv, kw = seen[0]
        self.assertEqual(len(seen), 1)                               # the three bad calls never ran
        self.assertEqual(argv[1:7], [s.assist, "review", "--tier", "standard", "--max-output", "4000"])
        self.assertEqual(argv[-2:], ["--", "--allow-expensive"])      # the question cannot be an option
        self.assertEqual(argv[7:9], ["-f", os.path.join(self.ws, "a.txt")])
        self.assertIn("Verdict: fine.", out)
        self.assertIn("$0.0012", out)
        self.assertIn("same family", out)
        self.assertIn("refused", refused)
        self.assertIn("refused", outside)
        self.assertIn("error", bad)

    def test_second_opinion_can_be_switched_off(self):
        s = self.session()
        args = argparse.Namespace(cwd=self.ws, mode="auto", net=False, tier="standard", model=None,
                                  max_usd=1.0, no_ask_gpt=True)
        off = cg.Session(self.cfg, args)
        self.assertNotIn("ask_gpt", [t["name"] for t in off.tools])
        self.assertIn("switched off", off.instructions)
        self.assertIn("ask_gpt tool", s.instructions)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIn("refused", off.tool_ask_gpt("ask", "q"))

    def test_html_to_text(self):
        page = ("<html><head><title>T</title><style>p{color:red}</style></head><body><h1>Title</h1>"
                "<script>steal()</script><p>One &amp; two.</p><ul><li>a</li><li>b</li></ul></body></html>")
        text = cg.html_to_text(page)
        self.assertEqual(text.split(), ["Title", "One", "&", "two.", "a", "b"])
        self.assertNotIn("steal", text)

    def test_strict_secrets_hides_the_files_from_commands(self):
        os.makedirs(os.path.join(self.ws, "deploy", ".ssh"))
        for path in (".env", "deploy/.ssh/id_ed25519", "deploy/server.pem"):
            with open(os.path.join(self.ws, path), "w") as f:
                f.write("x1\n")   # too short for the value filter: only the kernel can hide it
        self.assertEqual(sorted(os.path.relpath(p, self.ws) for p in cg.workspace_secret_paths(self.ws)),
                         [".env", "deploy/.ssh", "deploy/server.pem"])
        s = self.session()
        with contextlib.redirect_stdout(io.StringIO()):
            loose = s.tool_bash("cat .e* deploy/*.pe? deploy/.s*/*")
        self.assertEqual(loose.count("x1"), 3)        # what the name rules alone let through
        s.strict = True
        with contextlib.redirect_stdout(io.StringIO()):
            strict = s.tool_bash("cat .e* deploy/*.pe? deploy/.s*/*; cat a.txt")
        self.assertNotIn("x1", strict)
        self.assertEqual(strict.count("Permission denied"), 3)
        self.assertIn("alpha", strict)

    def test_secret_values_never_enter_a_request(self):
        with open(os.path.join(self.ws, ".env"), "w") as f:
            f.write("TOKEN=canary-value-1234567890\n")
        s = self.session(response(tool_call("bash", command="cat .e*")), response(message("ok")))
        self.turn(s, "go")
        self.assertNotIn("canary-value-1234567890", json.dumps(self.api.requests))

    def test_instructions_carry_memory_and_skills(self):
        mem = os.path.join(self.tmp.name, "memory")
        os.makedirs(mem)
        with open(os.path.join(mem, "MEMORY.md"), "w") as f:
            f.write("- [Tests](tests.md) — run with unittest, never pytest\n")
        skill = os.path.join(self.ws, ".claude", "skills", "changelog")
        os.makedirs(skill)
        with open(os.path.join(skill, "SKILL.md"), "w") as f:
            f.write("---\nname: changelog\ndescription: How to write changelog entries.\n---\nUse CHANGES.md.\n")
        with open(os.path.join(self.ws, "CLAUDE.md"), "w") as f:
            f.write("Project rule: tabs, not spaces.\n")
        s = self.session(response(tool_call("skill", name="changelog")), response(message("ok")))
        self.turn(s, "go")
        text = self.api.requests[0]["instructions"]
        for needle in ("never pytest", "changelog: How to write changelog entries.",
                       "tabs, not spaces", "read-only for you"):
            self.assertIn(needle, text)
        self.assertIn("Use CHANGES.md.", self.api.requests[1]["input"][-1]["output"])


def transcript(path, entries):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def user(text, **extra):
    return {"type": "user", "promptSource": "typed", "message": {"role": "user", "content": text}, **extra}


def claude(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


class Handoff(Base):
    """Taking over from a Claude Code transcript, and handing a session back."""

    def setUp(self):
        super().setUp()
        self.home, cg.HOME = cg.HOME, os.path.join(self.tmp.name, "home")
        self.folder = os.path.join(cg.HOME, ".claude", "projects", cg.re.sub(r"[^A-Za-z0-9]", "-", self.ws))
        os.makedirs(self.folder)

    def tearDown(self):
        cg.HOME = self.home
        super().tearDown()

    def write(self, name="aaaa1111.jsonl", extra=()):
        transcript(os.path.join(self.folder, name), [
            {"type": "ai-title", "aiTitle": "Pager fix"},
            user("Fix the off-by-one in pager.py <system-reminder>hidden harness text</system-reminder>"),
            claude({"type": "thinking", "thinking": "private reasoning"},
                   {"type": "text", "text": "I'll read the file first."},
                   {"type": "tool_use", "name": "Read", "input": {"file_path": "/w/pager.py"}}),
            {"type": "user", "toolUseResult": {"x": 1}, "message": {"role": "user", "content": [
                {"type": "tool_result", "content": "TOOL-OUTPUT-SHOULD-NOT-APPEAR"}]}},
            {"type": "user", "isMeta": True, "promptSource": "system",
             "message": {"role": "user", "content": "background task finished"}},
            claude({"type": "tool_use", "name": "Edit", "input": {"file_path": "/w/pager.py", "old_string": "a"}},
                   {"type": "tool_use", "name": "Bash", "input": {"command": "python3 -m unittest"}}),
            {"type": "user", "isSidechain": True, "promptSource": "typed",
             "message": {"role": "user", "content": "subagent prompt"}},
            user("Now add a changelog entry too"),
            claude({"type": "text", "text": "Fixed pager.py; the changelog entry is still to do."}),
            "not json at all",
            *extra])

    def test_digest_keeps_what_matters_and_drops_the_rest(self):
        self.write()
        digest = cg.claude_digest(cg.claude_sessions(self.ws)[0])
        for needle in ("Pager fix", "1. Fix the off-by-one in pager.py", "2. Now add a changelog entry too",
                       "- /w/pager.py", "[tool] Bash: python3 -m unittest",
                       "Fixed pager.py; the changelog entry is still to do."):
            self.assertIn(needle, digest)
        for absent in ("hidden harness text", "private reasoning", "TOOL-OUTPUT-SHOULD-NOT-APPEAR",
                       "background task finished", "subagent prompt"):
            self.assertNotIn(absent, digest)
        self.assertEqual(digest.count("- /w/pager.py"), 1)

    def test_digest_starts_from_claudes_own_summary_when_there_is_one(self):
        self.write(extra=[
            {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "SUMMARY: pager fixed, changelog pending"}},
            user("Carry on with the changelog"), claude({"type": "text", "text": "Writing it now."})])
        digest = cg.claude_digest(cg.claude_sessions(self.ws)[0])
        self.assertIn("SUMMARY: pager fixed, changelog pending", digest)
        self.assertIn("1. Carry on with the changelog", digest)
        self.assertNotIn("Fix the off-by-one", digest.split("## The user's messages")[1].split("##")[0])

    def test_digest_respects_its_budget(self):
        self.write(extra=[user("long " * 20000), claude({"type": "text", "text": "reply " * 20000})])
        self.assertLessEqual(len(cg.claude_digest(cg.claude_sessions(self.ws)[0], budget=20000)), 20000)

    def test_latest_session_is_chosen_and_a_prefix_selects_another(self):
        self.write("aaaa1111.jsonl")
        self.write("bbbb2222.jsonl", extra=[user("the newer session")])
        os.utime(os.path.join(self.folder, "aaaa1111.jsonl"), (1, 1))
        self.assertTrue(cg.handoff_message(self.ws, "latest")[1].endswith("bbbb2222.jsonl"))
        self.assertTrue(cg.handoff_message(self.ws, "aaaa")[1].endswith("aaaa1111.jsonl"))
        with self.assertRaises(SystemExit):
            cg.handoff_message(self.ws, "zzzz")
        with self.assertRaises(SystemExit):
            cg.handoff_message(os.path.join(self.tmp.name, "elsewhere"), "latest")

    def test_take_over_sends_the_digest_as_the_first_message_without_secrets(self):
        with open(os.path.join(self.ws, ".env"), "w") as f:
            f.write("TOKEN=handoff-secret-value-123\n")
        self.write(extra=[user("my token is handoff-secret-value-123, use it")])
        s = self.session(response(message("The pager is fixed; next is the changelog.")))
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            reply = cg.take_over(s, "latest", then="Just report.")
        first = self.api.requests[0]["input"][0]["content"]
        self.assertTrue(first.startswith("You are taking over a Claude Code session"))
        self.assertIn("2. Now add a changelog entry too", first)
        self.assertTrue(first.rstrip().endswith("Just report."))
        self.assertNotIn("handoff-secret-value-123", json.dumps(self.api.requests))
        self.assertIn("taking over from Claude Code session", shown.getvalue())
        self.assertEqual(reply, "The pager is fixed; next is the changelog.")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.ws, capture_output=True,
                              text=True, check=True).stdout.strip()

    def init_git(self):
        self.git("init", "-q")
        self.git("config", "user.name", "Test User")
        self.git("config", "user.email", "test@example.com")
        with open(os.path.join(self.ws, "gone.txt"), "w") as f:
            f.write("old file\n")
        self.git("add", "a.txt", "gone.txt")
        self.git("commit", "-qm", "base")

    def test_snapshot_does_not_disturb_git_state(self):
        self.init_git()
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.write("stashed\n")
        self.git("stash", "push", "-qm", "preexisting")
        with open(os.path.join(self.ws, ".gitignore"), "w") as f:
            f.write("ignored.txt\n")
        with open(os.path.join(self.ws, "ignored.txt"), "w") as f:
            f.write("ignore me\n")
        with open(os.path.join(self.ws, "staged.txt"), "w") as f:
            f.write("staged\n")
        self.git("add", "staged.txt")
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.write("changed\n")
        os.unlink(os.path.join(self.ws, "gone.txt"))
        with open(os.path.join(self.ws, "untracked.txt"), "w") as f:
            f.write("untracked\n")
        before = [self.git(*cmd) for cmd in (("status", "--porcelain"), ("rev-parse", "HEAD"),
                                              ("stash", "list"), ("ls-files", "--stage"))]
        self.write()
        s = self.session(response(message("done")))
        with contextlib.redirect_stdout(io.StringIO()):
            cg.take_over(s, "latest")
        after = [self.git(*cmd) for cmd in (("status", "--porcelain"), ("rev-parse", "HEAD"),
                                             ("stash", "list"), ("ls-files", "--stage"))]
        self.assertEqual(before, after)
        self.assertEqual(self.git("rev-parse", f"refs/tandem/takeover/{s.id}"), s.snapshot)
        names = self.git("ls-tree", "-r", "--name-only", s.snapshot).splitlines()
        self.assertIn("untracked.txt", names)
        self.assertIn("staged.txt", names)
        self.assertNotIn("gone.txt", names)
        self.assertNotIn("ignored.txt", names)
        s.save()
        with open(s.path) as f:
            self.assertEqual(json.load(f)["snapshot"], s.snapshot)

    def test_handback_diff_includes_modified_untracked_and_deleted(self):
        self.init_git()
        self.write()
        s = self.session(response(message("done")))
        with contextlib.redirect_stdout(io.StringIO()):
            cg.take_over(s, "latest")
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.write("new text\n")
        with open(os.path.join(self.ws, "added.txt"), "w") as f:
            f.write("brand new\n")
        os.unlink(os.path.join(self.ws, "gone.txt"))
        s.save()
        with open(s.path) as f:
            prompt = cg.handback_prompt(json.load(f))
        section = prompt.split("## What changed since the takeover", 1)[1]
        for needle in ("a.txt", "added.txt", "gone.txt", "+new text", "+brand new", "-old file"):
            self.assertIn(needle, section)
        self.assertIn("3 files changed", section)

    def test_handback_diff_fits_budget_and_explains_truncation(self):
        self.init_git()
        self.write()
        s = self.session(response(message("done")))
        with contextlib.redirect_stdout(io.StringIO()):
            cg.take_over(s, "latest")
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.writelines(f"line {i}\n" for i in range(2000))
        prompt = cg.handback_prompt({"id": s.id, "workspace": self.ws, "snapshot": s.snapshot}, budget=900)
        self.assertLessEqual(len(prompt), 900)
        self.assertIn("## What changed since the takeover", prompt)
        self.assertIn("The diff was cut here", prompt)
        self.assertIn(f"git diff {s.snapshot}", prompt)

    def test_a_small_diff_leaves_the_record_whole(self):
        self.init_git()
        self.write()
        s = self.session(response(message("done")))
        with contextlib.redirect_stdout(io.StringIO()):
            cg.take_over(s, "latest")
        with open(os.path.join(self.ws, "a.txt"), "a") as f:
            f.write("one more line\n")
        # A record of about 7,000 characters: more than half the budget, less than all of it.
        saved = {"id": s.id, "workspace": self.ws, "snapshot": s.snapshot, "input": [
            {"role": "user", "content": "a" * 1400}, {"role": "user", "content": "b" * 1400},
            {"type": "message", "content": [{"type": "output_text", "text": "x" * 3900 + " END-OF-REPLY"}]}]}
        prompt = cg.handback_prompt(saved)
        self.assertIn("+one more line", prompt)
        self.assertIn("END-OF-REPLY", prompt)        # a one-line diff does not cost the record half its room
        self.assertLessEqual(len(prompt), 12000)

    def test_a_changed_secret_file_is_named_but_never_shown(self):
        self.init_git()
        # A tracked file that an ignore rule also matches, as on a machine whose global
        # gitignore lists .env: the snapshot must still see it change.
        with open(os.path.join(self.tmp.name, "ignore"), "w") as f:
            f.write(".env\n")
        self.git("config", "core.excludesFile", os.path.join(self.tmp.name, "ignore"))
        with open(os.path.join(self.ws, ".env"), "w") as f:
            f.write("TOKEN=before\n")
        self.git("add", "-f", ".env")
        self.git("commit", "-qm", "track the env file")
        self.write()
        s = self.session(response(message("done")))
        with contextlib.redirect_stdout(io.StringIO()):
            cg.take_over(s, "latest")
        with open(os.path.join(self.ws, ".env"), "w") as f:
            f.write("TOKEN=written-during-the-stint-123456\n")
        with open(os.path.join(self.ws, "a.txt"), "a") as f:
            f.write("ordinary change\n")
        prompt = cg.handback_prompt({"id": s.id, "workspace": self.ws, "snapshot": s.snapshot})
        self.assertIn("Changed, content not shown (secret files): .env", prompt)
        self.assertNotIn("written-during-the-stint-123456", prompt)
        self.assertIn("+ordinary change", prompt)

    def test_the_snapshot_needs_no_git_identity(self):
        self.init_git()
        subprocess.run("git config --unset user.name; git config --unset user.email", shell=True, cwd=self.ws)
        self.write()
        s = self.session(response(message("done")))
        saved = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")}
        os.environ.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cg.take_over(s, "latest")
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.assertRegex(s.snapshot or "", r"^[0-9a-f]{40}")
        who = subprocess.run(["git", "log", "-1", "--format=%an <%ae>", s.snapshot], cwd=self.ws,
                             capture_output=True, text=True).stdout.strip()
        self.assertEqual(who, "tandem snapshot <snapshot@tandem.invalid>")

    def test_non_git_takeover_still_hands_back(self):
        self.write()
        s = self.session(response(message("done")))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cg.take_over(s, "latest"), "done")
        self.assertIsNone(s.snapshot)
        s.save()
        with open(s.path) as f:
            self.assertIn("No diff is available", cg.handback_prompt(json.load(f)))

    def test_failed_git_snapshot_does_not_prevent_takeover(self):
        self.init_git()
        self.write()
        s = self.session(response(message("done")))
        with mock.patch.object(cg, "git_tree", return_value=None), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cg.take_over(s, "latest"), "done")
        self.assertIsNone(s.snapshot)
        self.assertIn("No diff is available", cg.handback_prompt({"id": s.id, "workspace": self.ws}))

    def start(self, **kw):
        """resolve_start's result, or the message it exits with."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                return cg.resolve_start(self.ws, **kw), out.getvalue()
            except SystemExit as e:
                return str(e.code), out.getvalue()

    def test_resume_with_a_claude_code_id_takes_over_from_that_session(self):
        # What a person does first: paste the id Claude Code printed after "resume with".
        self.write("c54732ba-1111-4222-8333-444455556666.jsonl")
        (ours, theirs), said = self.start(resume="c54732ba-1111-4222-8333-444455556666")
        self.assertEqual((ours, theirs), (None, "c54732ba-1111-4222-8333-444455556666"))
        self.assertIn("is a Claude Code session, not a tandem one: taking over from it", said)
        (ours, theirs), _ = self.start(resume="c54732ba")              # the start of the id is enough
        self.assertEqual(theirs, "c54732ba-1111-4222-8333-444455556666")

    def test_resume_and_continue_find_our_own_sessions(self):
        s = self.session(response(message("hello")))
        self.turn(s, "hi")
        self.write("c54732ba-1111.jsonl")
        for kw in ({"resume": s.id}, {"resume": s.id[:13]}, {"resume": "latest"}, {"cont": True}):
            with self.subTest(kw=kw):
                self.assertEqual(self.start(**kw)[0], (s.id, None))

    def test_dead_ends_say_what_to_do_in_plain_words(self):
        message, _ = self.start(resume="nope")                            # nothing here at all
        self.assertIn("no session 'nope'", message)
        self.assertIn("Start a new one with: tandem", message)
        self.assertIn("no session to continue", self.start(cont=True)[0])
        self.write("c54732ba-1111.jsonl")
        message, _ = self.start(resume="zzzz")                            # a Claude session exists
        self.assertIn("Claude Code sessions here: c54732ba", message)
        self.assertIn("--from-claude", message)
        message, _ = self.start(cont=True)                                # nobody at the terminal to ask
        self.assertIn("No tandem session here yet. There is a Claude Code session, c54732ba", message)
        self.assertIn("tandem --from-claude", message)
        self.write("c54732bb-2222.jsonl")
        self.assertIn("matches more than one session", self.start(resume="c54732b")[0])
        for message in (self.start(resume="nope")[0], self.start(resume="zzzz")[0]):
            self.assertNotIn("Traceback", message)

    def test_plain_start_resolves_to_nothing(self):
        self.write()
        self.assertEqual(self.start()[0], (None, None))

    def hook(self, event):
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(cg.__file__)), "on_limit.py")],
                           input=json.dumps(event), text=True, capture_output=True,
                           env={"HOME": cg.HOME, "PATH": "/nonexistent"})   # no notifier on this PATH
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_the_limit_hook_points_the_takeover_at_the_session_that_stopped(self):
        self.write("aaaa1111.jsonl")
        self.write("bbbb2222.jsonl", extra=[user("the session that hit the limit")])
        stopped = os.path.join(self.folder, "bbbb2222.jsonl")
        os.utime(os.path.join(self.folder, "aaaa1111.jsonl"))          # touched later, e.g. by a reader
        self.assertIsNone(cg.limit_marker(self.ws))
        self.hook({"hook_event_name": "StopFailure", "error": "rate_limit", "cwd": self.ws,
                   "session_id": "bbbb2222", "transcript_path": stopped})
        record = cg.limit_marker(self.ws)
        self.assertEqual((record["error"], record["transcript_path"]), ("rate_limit", stopped))
        self.assertEqual(cg.handoff_message(self.ws, "latest")[1], stopped)
        self.assertIsNone(cg.limit_marker(self.ws))                      # used once, then quiet
        self.assertTrue(cg.handoff_message(self.ws, "latest")[1].endswith(".jsonl"))

    def test_an_old_limit_record_or_a_newer_session_wins_over_the_hook(self):
        self.write("aaaa1111.jsonl")
        old = os.path.join(self.folder, "aaaa1111.jsonl")
        self.hook({"error": "billing_error", "cwd": self.ws, "transcript_path": old})
        self.write("cccc3333.jsonl")                                     # the user went back to Claude since
        newer = os.path.join(self.folder, "cccc3333.jsonl")
        future = cg.time.time() + 3600
        os.utime(newer, (future, future))
        self.assertEqual(cg.handoff_message(self.ws, "latest")[1], newer)
        record = cg.limit_marker(self.ws)
        with open(record["marker"], "w") as f:
            json.dump({**record, "time": cg.time.time() - 3 * 24 * 3600}, f)
        self.assertIsNone(cg.limit_marker(self.ws))

    def test_the_hook_survives_bad_input(self):
        for text in ("", "not json", "[]", '{"cwd": 5}'):
            r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(cg.__file__)), "on_limit.py")],
                               input=text, text=True, capture_output=True, env={"HOME": cg.HOME, "PATH": "/nonexistent"})
            self.assertEqual(r.returncode, 0, text)

    def test_a_takeover_refused_by_the_budget_leaves_nothing_behind(self):
        self.write()
        with open(cg.LEDGER_PATH, "w") as f:
            f.write(json.dumps({"ts": cg.dt.date.today().isoformat() + "T00:00:00", "cost_usd": 99.0,
                                "session": "earlier", "model": "gpt-6-sol"}) + "\n")
        s = self.session(response(message("unused")))
        with contextlib.redirect_stdout(io.StringIO()) as shown:
            self.assertIsNone(cg.take_over(s, "latest"))      # nobody at the terminal to approve more
        self.assertIn("The takeover has not started", shown.getvalue())
        self.assertEqual((s.input, s.claude_session, self.api.requests), ([], "", []))
        s.save()
        self.assertIsNone(cg.gpt_session_for(self.ws, cg.claude_sessions(self.ws)[0]))

    def prompt_hook(self, session_id, cwd=None):
        """What on_prompt.py prints for a prompt typed in a Claude Code session."""
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(cg.__file__)), "on_prompt.py")],
                           input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": session_id,
                                             "cwd": cwd or self.ws, "prompt": "review any changes made"}),
                           text=True, capture_output=True, env={"HOME": cg.HOME, "PATH": "/usr/bin:/bin"})
        self.assertEqual(r.returncode, 0)
        return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None

    def test_claude_is_told_what_was_done_the_next_time_the_user_types(self):
        # What happened on the first real round trip: the user went back to Claude and typed,
        # without pasting any command, and Claude had no idea whose edits it was looking at.
        self.init_git()
        self.write("c54732ba-1111.jsonl")
        s = self.session(response(tool_call("write", path="preview.py", content="print(1)\n")),
                         response(message("Added preview.py; could not run npm test here, so it is untested.")))
        with contextlib.redirect_stdout(io.StringIO()):
            cg.take_over(s, "latest")
        self.assertIsNone(self.prompt_hook("c54732ba-1111"))            # nothing is left until asked to
        self.assertTrue(cg.write_handback(s))
        told = self.prompt_hook("c54732ba-1111")
        self.assertEqual(told["hookEventName"], "UserPromptSubmit")
        text = told["additionalContext"]
        for needle in ("It is not from the user", "tandem (an OpenAI model", "started from a digest of this Claude Code session",
                       "It wrote or edited: preview.py", "its work, not an unknown session's",
                       "could not run npm test here, so it is untested", "## What changed since the takeover",
                       "+print(1)"):
            self.assertIn(needle, text)
        self.assertIsNone(self.prompt_hook("c54732ba-1111"))            # once per session, not on every prompt
        other = self.prompt_hook("ffff0000-2222")["additionalContext"]  # another session in the same directory
        self.assertIn("another Claude Code session here (c54732ba), not this one", other)
        self.assertIsNone(self.prompt_hook("eeee0000-3333", cwd=os.path.join(self.tmp.name, "elsewhere")))

    def test_the_record_is_kept_current_and_old_ones_go_quiet(self):
        self.write("c54732ba-1111.jsonl")
        s = self.session(response(message("first reply")), response(message("second reply")))
        with contextlib.redirect_stdout(io.StringIO()):
            cg.take_over(s, "latest")
        cg.write_handback(s)
        self.assertIn("first reply", self.prompt_hook("c54732ba-1111")["additionalContext"])
        self.turn(s, "and another thing")
        cg.write_handback(s)
        # Told already: it hears only what is new, not the whole record again.
        update = self.prompt_hook("c54732ba-1111")["additionalContext"]
        self.assertIn("Only what is new since that note follows", update)
        self.assertIn("and another thing", update)
        self.assertIn("second reply", update)
        self.assertNotIn("first reply", update)
        self.assertIsNone(self.prompt_hook("c54732ba-1111"))             # and only once
        full = self.prompt_hook("aaaa0000-9999")["additionalContext"]    # never told: the whole record
        self.assertIn("first reply", full)
        self.assertIn("second reply", full)
        path = cg.handback_path(self.ws)
        with open(path) as f:
            record = json.load(f)
        with open(path, "w") as f:
            json.dump({**record, "time_end": cg.time.time() - 4 * 86400}, f)
        self.assertIsNone(self.prompt_hook("bbbb0000-8888"))             # four days old: history, not news

    def test_no_record_without_a_takeover_or_without_any_work(self):
        plain = self.session(response(message("hi")))
        self.turn(plain, "hello")
        self.assertIsNone(cg.write_handback(plain))                       # an ordinary session hands nothing back
        self.write("c54732ba-1111.jsonl")
        with open(cg.LEDGER_PATH, "w") as f:
            f.write(json.dumps({"ts": cg.dt.date.today().isoformat() + "T00:00:00", "cost_usd": 99.0}) + "\n")
        refused = self.session(response(message("unused")))
        with contextlib.redirect_stdout(io.StringIO()):
            cg.take_over(refused, "latest")
        self.assertIsNone(cg.write_handback(refused))                     # a takeover that never started
        self.assertFalse(os.path.exists(cg.handback_path(self.ws)))

    def test_the_prompt_hook_survives_bad_input(self):
        os.makedirs(os.path.dirname(cg.handback_path(self.ws)))
        with open(cg.handback_path(self.ws), "w") as f:
            f.write("{ not json")
        script = os.path.join(os.path.dirname(os.path.abspath(cg.__file__)), "on_prompt.py")
        for text in ("", "not json", "[]", '{"cwd": 5}', json.dumps({"cwd": self.ws, "session_id": "x"})):
            r = subprocess.run([sys.executable, script], input=text, text=True, capture_output=True,
                               env={"HOME": cg.HOME, "PATH": "/usr/bin:/bin"})
            self.assertEqual((r.returncode, r.stdout), (0, ""), text)

    def test_export_is_a_readable_record_for_the_way_back(self):
        self.write()
        s = self.session(response(tool_call("write", "c1", path="CHANGES.md", content="- fix\n"),
                                  tool_call("bash", "c2", command="python3 -m unittest"),
                                  tool_call("todo", "c3", items=[{"text": "changelog", "status": "completed"}])),
                         response(message("Added the changelog entry; tests pass.")),
                         response(message("Version bumped.")))
        with contextlib.redirect_stdout(io.StringIO()):
            cg.take_over(s, "latest")
            s.turn("thanks, also bump the version")
        s.save()
        with open(s.path) as f:
            text = cg.session_export(json.load(f))
        for needle in ("1. thanks, also bump the version", "- CHANGES.md", "- `python3 -m unittest`",
                       "- [x] changelog", "Added the changelog entry; tests pass.", "standing in for Claude Code"):
            self.assertIn(needle, text)
        self.assertNotIn("You are taking over", text)   # the digest is not echoed back
        quiet = self.session(response(message("Finished what Claude had started.")))
        with contextlib.redirect_stdout(io.StringIO()):
            cg.take_over(quiet, "latest")
        quiet.save()
        with open(quiet.path) as f:
            self.assertIn("nothing new: it carried on with what had already been asked", cg.session_export(json.load(f)))


class Setup(unittest.TestCase):
    """The installer, run against a made-up home directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cgt.")
        self.home, cg.HOME = cg.HOME, os.path.join(self.tmp.name, "home")
        os.makedirs(os.path.join(cg.HOME, ".claude"))
        self.settings = os.path.join(cg.HOME, ".claude", "settings.json")

    def tearDown(self):
        cg.HOME = self.home
        self.tmp.cleanup()

    def setup(self, **flags):
        out = io.StringIO()
        args = argparse.Namespace(**{"check": False, "yes": True, **flags})
        code = 0
        with contextlib.redirect_stdout(out):
            try:
                cg.run_setup(args, cg.load_config())
            except SystemExit as e:
                code = e.code
        return code, out.getvalue()

    def test_installs_links_and_hook_and_keeps_other_settings(self):
        with open(self.settings, "w") as f:
            json.dump({"theme": "dark", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}, f)
        code, shown = self.setup()
        self.assertEqual(code, 0, shown)
        for name in ("tandem", "tandem-relay"):
            self.assertEqual(os.path.realpath(os.path.join(cg.HOME, ".local", "bin", name)),
                             os.path.join(cg.HERE, "cg.py"))
        with open(self.settings) as f:
            after = json.load(f)
        self.assertEqual(after["theme"], "dark")
        self.assertEqual(len(after["hooks"]["Stop"]), 1)                    # someone else's hook kept
        entry = after["hooks"]["StopFailure"][0]
        self.assertEqual(entry["matcher"], "rate_limit|billing_error")
        self.assertEqual(entry["hooks"][0]["command"], os.path.join(cg.HERE, "on_limit.py"))
        prompt = after["hooks"]["UserPromptSubmit"][0]
        self.assertNotIn("matcher", prompt)
        self.assertEqual(prompt["hooks"][0]["command"], os.path.join(cg.HERE, "on_prompt.py"))
        backups = [n for n in os.listdir(os.path.dirname(self.settings)) if ".bak-" in n]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(os.path.dirname(self.settings), backups[0])) as f:
            self.assertNotIn("StopFailure", f.read())

    def test_adds_the_second_hook_where_only_the_first_was_installed(self):
        with open(self.settings, "w") as f:      # a machine set up before the hand-back hook existed
            json.dump({"hooks": {"StopFailure": [{"matcher": "rate_limit|billing_error", "hooks": [
                {"type": "command", "command": os.path.join(cg.HERE, "on_limit.py"), "timeout": 10}]}]}}, f)
        code, shown = self.setup()
        self.assertIn("hooks: added UserPromptSubmit", shown)
        with open(self.settings) as f:
            after = json.load(f)
        self.assertEqual(len(after["hooks"]["StopFailure"]), 1)
        self.assertEqual(len(after["hooks"]["UserPromptSubmit"]), 1)

    def test_running_it_twice_changes_nothing_more(self):
        self.setup()
        with open(self.settings) as f:
            first = f.read()
        code, shown = self.setup()
        with open(self.settings) as f:
            self.assertEqual(f.read(), first)
        self.assertIn("hooks: already there", shown)

    def test_check_only_reports(self):
        code, shown = self.setup(check=True)
        self.assertIn("command tandem: not installed", shown)
        self.assertIn("not installed", shown.split("hooks in")[1])
        self.assertFalse(os.path.exists(os.path.join(cg.HOME, ".local")))
        self.assertFalse(os.path.exists(self.settings))
        self.setup()
        code, shown = self.setup(check=True)
        self.assertIn("command tandem-relay: installed", shown)
        self.assertEqual(shown.split("hooks in")[1].strip().split(": ")[1], "installed")

    def test_leaves_a_broken_settings_file_and_foreign_links_alone(self):
        with open(self.settings, "w") as f:
            f.write("{ not json")
        os.makedirs(os.path.join(cg.HOME, ".local", "bin"))
        other = os.path.join(self.tmp.name, "someone-elses-tool")
        with open(other, "w") as f:
            f.write("#!/bin/sh\n")
        os.symlink(other, os.path.join(cg.HOME, ".local", "bin", "tandem"))
        code, shown = self.setup()
        with open(self.settings) as f:
            self.assertEqual(f.read(), "{ not json")
        self.assertIn("is not valid JSON", shown)
        self.assertEqual(os.path.realpath(os.path.join(cg.HOME, ".local", "bin", "tandem")), other)
        self.assertIn("command tandem: left alone", shown)


class ResetTime(unittest.TestCase):
    def at(self, y, m, d, hh, mm):
        return cg.dt.datetime(y, m, d, hh, mm).timestamp()

    def test_reads_the_time_a_limit_message_gives(self):
        said = self.at(2026, 10, 9, 14, 10)                      # a Friday afternoon
        for message, want in (
                ("You've hit your session limit · resets 3:45pm", self.at(2026, 10, 9, 15, 45)),
                ("limit reached - resets 1am", self.at(2026, 10, 10, 1, 0)),          # tomorrow
                ("You've hit your weekly limit · resets Mon 12:00am", self.at(2026, 10, 12, 0, 0)),
                ("resets Fri 2:00pm", self.at(2026, 10, 16, 14, 0)),                  # already past today
                ("Resets at 12:30 PM", self.at(2026, 10, 10, 12, 30))):
            with self.subTest(message=message):
                self.assertEqual(cg.reset_time(message, said), want)

    def test_says_nothing_when_no_time_is_given(self):
        for message in ("", None, "You've hit your monthly spend limit · raise it at the console",
                        "resets soon", "resets 25:00pm", "API Error: Rate limit reached"):
            with self.subTest(message=message):
                self.assertIsNone(cg.reset_time(message, 0))


class Stream(unittest.TestCase):
    def lines(self, *events):
        return [f"data: {json.dumps(e)}\n".encode() for e in events]

    def test_text_and_final_response(self):
        got = []
        final = cg.read_stream(self.lines(
            {"type": "response.created"},
            {"type": "response.output_text.delta", "delta": "Hel"},
            {"type": "response.output_text.delta", "delta": "lo"},
            {"type": "response.completed", "response": {"status": "completed", "output": []}}),
            got.append)
        self.assertEqual("".join(got), "Hello")
        self.assertEqual(final["status"], "completed")

    def test_failure_and_truncation(self):
        with self.assertRaises(cg.ApiError):
            cg.read_stream(self.lines({"type": "response.failed",
                                       "response": {"error": {"message": "boom"}}}), print)
        with self.assertRaises(cg.ApiError):
            cg.read_stream(self.lines({"type": "response.output_text.delta", "delta": "x"}),
                           lambda d: None)


if __name__ == "__main__":
    unittest.main()

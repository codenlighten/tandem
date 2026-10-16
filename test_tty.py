#!/usr/bin/env python3
"""Controls for the approval prompts, driven through a real pseudo-terminal the way a
person at the keyboard would. The model is replaced by a script, so nothing is spent.

  python3 test_tty.py
"""
import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

os.environ["OPENAI_API_KEY"] = "not-a-real-key-tests-make-no-calls"   # never the real one

HERE = os.path.dirname(os.path.abspath(__file__))

DRIVER = r'''
import sys, os
sys.path.insert(0, {here!r})
import cg
from test_loop import FakeApi, response, message, tool_call
cg.LEDGER_PATH = os.path.join({tmp!r}, "ledger.jsonl")
cg.RUNS_DIR = os.path.join({tmp!r}, "runs")
cg.spent_today_elsewhere = lambda cfg: 0.0
cg.call_api = FakeApi(
    response(tool_call("write", path="a.txt", content="one\n")), response(message("REPLY-1")),
    response(tool_call("write", path="b.txt", content="two\n")), response(message("REPLY-2")),
    response(tool_call("bash", command="ls")), response(message("REPLY-3")),
    response(tool_call("bash", command="touch c.txt")),
    response(tool_call("bash", command="touch d.txt")),
    response(tool_call("bash", command="git push")), response(message("REPLY-4")),
)
sys.argv = ["cg.py", "chat", "--cwd", {ws!r}, "--memory-dir", os.path.join({tmp!r}, "mem")]
cg.main()
'''


class Terminal:
    def __init__(self, argv, cwd=None, env=None):
        self.master, slave = pty.openpty()
        # A real terminal has a size; without one, line editing wraps the prompt oddly.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                     start_new_session=True, close_fds=True, cwd=cwd,
                                     env=dict(env if env is not None else os.environ, TERM="dumb"))
        os.close(slave)
        self.seen, self.pos = "", 0

    def expect(self, text, timeout=20):
        """Wait for text to appear after what earlier calls consumed; return what came before it
        together with it. The position moves to the end of the match, not to the end of what
        has been read: one read can bring a reply and the prompt after it, and a search that
        started at the end of the buffer would then wait for a prompt that had already come."""
        start, deadline = self.pos, time.time() + timeout
        while text not in self.seen[start:]:
            if time.time() > deadline:
                raise AssertionError(f"timed out waiting for {text!r}; saw:\n{self.seen[start:]}")
            if select.select([self.master], [], [], 0.2)[0]:
                try:
                    self.seen += os.read(self.master, 4096).decode(errors="replace")
                except OSError:   # the program exited: a missing text is a failure, not a pass
                    if text in self.seen[start:]:
                        break
                    raise AssertionError(f"the program ended before {text!r}; saw:\n{self.seen[start:]}")
        self.pos = self.seen.index(text, start) + len(text)
        return self.seen[start:self.pos]

    def send(self, line):
        os.write(self.master, (line + "\n").encode())

    def close(self):
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        os.close(self.master)


class Approvals(unittest.TestCase):
    def test_prompts_at_a_terminal(self):
        with tempfile.TemporaryDirectory(prefix="cgt.", dir="/tmp") as tmp:
            ws = os.path.join(tmp, "ws")
            os.makedirs(ws)
            exists = lambda name: os.path.exists(os.path.join(ws, name))
            t = Terminal([sys.executable, "-c", DRIVER.format(here=HERE, tmp=tmp, ws=ws)])
            try:
                t.expect("mode=ask")
                t.expect("> ")

                t.send("make a")                       # an edit, refused by the person
                shown = t.expect("[a]uto mode: ")
                self.assertIn("+one", shown)           # the diff is shown before asking
                self.assertFalse(exists("a.txt"))      # nothing written while waiting
                t.send("n")
                t.expect("REPLY-1")
                self.assertFalse(exists("a.txt"))

                t.send("make b")                       # an edit, approved
                t.expect("[a]uto mode: ")
                t.send("y")
                t.expect("REPLY-2")
                self.assertTrue(exists("b.txt"))

                t.send("list")                         # read-only: no prompt at all
                shown = t.expect("REPLY-3")
                self.assertNotIn("allow", shown)

                t.send("touch things")                 # 'always' switches to auto
                shown = t.expect("[a]uto mode: ")
                self.assertIn("touch c.txt", shown)
                t.send("a")
                shown = t.expect("[y]es / [n]o: ")     # d.txt ran unasked; git push still asks
                self.assertNotIn("[a]lways this session", shown)
                self.assertIn("git push", shown)
                self.assertTrue(exists("c.txt") and exists("d.txt"))
                t.send("n")
                shown = t.expect("REPLY-4")
                self.assertIn("did not approve", shown)

                t.send("/mode")
                t.expect("mode: auto")
                t.send("/cost")
                t.expect("calls this session")
                t.send("/exit")
                t.expect("Resume it with")
            finally:
                t.close()


TAKEOVER_DRIVER = r'''
import sys, os, json, re, time
sys.path.insert(0, {here!r})
import cg
from test_loop import FakeApi, response, message, transcript, user, claude
cg.LEDGER_PATH = os.path.join({tmp!r}, "ledger.jsonl")
cg.EXTRA_LEDGER_PATH = os.path.join({tmp!r}, "ledger-extra.jsonl")
cg.RUNS_DIR = os.path.join({tmp!r}, "runs")
cg.HOME = os.path.join({tmp!r}, "home")
cg.spent_today_elsewhere = lambda cfg: 0.0
slug = re.sub(r"[^A-Za-z0-9]", "-", {ws!r})
folder = os.path.join(cg.HOME, ".claude", "projects", slug)
os.makedirs(folder)
stopped = os.path.join(folder, "abcd1234.jsonl")
transcript(stopped, [user("Fix the pager"), claude({{"type": "text", "text": "Halfway through the pager fix."}})])
os.makedirs(os.path.join(cg.HOME, ".tandem", "limit"))
with open(os.path.join(cg.HOME, ".tandem", "limit", slug + ".json"), "w") as f:
    json.dump({{"time": time.time() - 300, "cwd": {ws!r}, "error": "rate_limit", "transcript_path": stopped}}, f)
api = cg.call_api = FakeApi(response(message("TOOK-OVER: the pager fix is half done.")))
sys.argv = ["cg.py", "chat", "--cwd", {ws!r}, "--memory-dir", os.path.join({tmp!r}, "mem")]
try:
    cg.main()
finally:
    first = api.requests[0]["input"][0]["content"] if api.requests else ""
    print("DIGEST-HAD-USER-REQUEST" if "1. Fix the pager" in first else "NO-DIGEST")
'''


class Takeover(unittest.TestCase):
    def test_offer_to_take_over_after_a_limit(self):
        with tempfile.TemporaryDirectory(prefix="cgt.") as tmp:
            ws = os.path.join(tmp, "ws")
            os.makedirs(ws)
            t = Terminal([sys.executable, "-c", TAKEOVER_DRIVER.format(here=HERE, tmp=tmp, ws=ws)])
            try:
                shown = t.expect("take over from that session? [y]es / [n]o: ")
                self.assertIn("Claude Code stopped on its usage limit here 5 min ago", shown)
                t.send("y")
                shown = t.expect("TOOK-OVER: the pager fix is half done.")
                self.assertIn("taking over from Claude Code session abcd1234", shown)
                t.expect("> ")
                t.send("/exit")
                t.expect("DIGEST-HAD-USER-REQUEST")
            finally:
                t.close()


FAKE_CLAUDE = r'''#!/usr/bin/env python3
# Stands in for the claude command: the first run "hits its limit" (it fires the hook
# script the way Claude Code would, then exits when the user quits); later runs just
# record how they were started.
import json, os, subprocess, sys
log = os.path.join({tmp!r}, "claude-runs.json")
runs = json.load(open(log)) if os.path.exists(log) else []
runs.append(sys.argv[1:])
json.dump(runs, open(log, "w"))
print(f"FAKE-CLAUDE run {{len(runs)}}", flush=True)
if len(runs) <= {limited_runs}:
    subprocess.run([sys.executable, os.path.join({here!r}, "on_limit.py")], text=True, input=json.dumps(
        {{"hook_event_name": "StopFailure", "error": "rate_limit", "cwd": os.getcwd(), "session_id": "sid-1234",
          "transcript_path": {transcript!r},
          "last_assistant_message": "You've hit your session limit - resets 3:45pm"}}))
'''

FAKE_GPT = r'''#!/usr/bin/env python3
# Stands in for cg.py in the relay's GPT step: the real program with a scripted model.
import sys
sys.path.insert(0, {here!r})
import cg
from test_loop import FakeApi, response, message, tool_call
cg.spent_today_elsewhere = lambda cfg: 0.0
cg.call_api = FakeApi(response(message("GPT-TOOK-OVER: the pager fix is half done.")),
                      response(tool_call("todo", items=[{{"text": "changelog", "status": "completed"}}])),
                      response(message("GPT-DID-IT: wrote the changelog entry.")),
                      response(message("GPT-AGAIN: still here.")))
sys.argv = ["cg.py"] + sys.argv[1:] + ["--memory-dir", {tmp!r} + "/mem", "--mode", "auto"]
cg.main()
'''


class BudgetAtTheTerminal(unittest.TestCase):
    """Greg's first real takeover was refused because the day's budget was spent. The person at
    the terminal is the one who decides about spending, so a refusal now asks them."""

    def test_refusal_offers_more_and_then_takes_over(self):
        with tempfile.TemporaryDirectory(prefix="cgt.") as tmp:
            ws = os.path.join(tmp, "ws")
            os.makedirs(ws)
            driver = TAKEOVER_DRIVER.format(here=HERE, tmp=tmp, ws=ws).replace(
                "cg.spent_today_elsewhere = lambda cfg: 0.0", "cg.spent_today_elsewhere = lambda cfg: 99.0")
            driver = driver.replace('"--memory-dir", os.path.join', '"--from-claude", "--memory-dir", os.path.join')
            t = Terminal([sys.executable, "-c", driver])
            try:
                shown = t.expect("Type an amount in dollars (for example 2), or Enter for no: ")
                self.assertIn("tandem stops at", shown)
                t.send("2")
                shown = t.expect("TOOK-OVER: the pager fix is half done.")
                self.assertIn("approved: up to $2.00 more for this session", shown)
                t.expect("\n> ")
                t.send("/cost")
                t.send("/exit")
                t.expect("DIGEST-HAD-USER-REQUEST")
            finally:
                t.close()
            with open(os.path.join(tmp, "ledger-extra.jsonl")) as f:
                self.assertEqual(len(f.readlines()), 1)                 # logged apart from the shared ledger
            self.assertFalse(os.path.exists(os.path.join(tmp, "ledger.jsonl")))

    def test_declining_leaves_the_takeover_unstarted(self):
        with tempfile.TemporaryDirectory(prefix="cgt.") as tmp:
            ws = os.path.join(tmp, "ws")
            os.makedirs(ws)
            driver = TAKEOVER_DRIVER.format(here=HERE, tmp=tmp, ws=ws).replace(
                "cg.spent_today_elsewhere = lambda cfg: 0.0", "cg.spent_today_elsewhere = lambda cfg: 99.0")
            driver = driver.replace('"--memory-dir", os.path.join', '"--from-claude", "--memory-dir", os.path.join')
            t = Terminal([sys.executable, "-c", driver])
            try:
                t.expect("or Enter for no: ")
                t.send("")
                t.expect("The takeover has not started. Once the budget allows, run the same command again.")
                t.expect("\n> ")
                t.send("/exit")
                t.expect("NO-DIGEST")
            finally:
                t.close()


class ResumeMeansTakeover(unittest.TestCase):
    """What Greg did on his first real try: --resume with the id Claude Code had printed, and
    --continue in a directory with no tandem session yet. Both used to be dead ends."""

    def start(self, tmp, *flags, limit=False):
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        driver = TAKEOVER_DRIVER.format(here=HERE, tmp=tmp, ws=ws)
        if not limit:   # no limit was hit: there is only the Claude session
            driver = driver.replace('os.makedirs(os.path.join(cg.HOME, ".tandem", "limit"))', "import io")
            driver = re.sub(r'with open\(os\.path\.join\(cg\.HOME, "\.tandem".*?\n    json\.dump\(.*?\n', "",
                            driver, flags=re.S)
        driver = driver.replace('"--memory-dir", os.path.join', f'*{list(flags)!r}, "--memory-dir", os.path.join')
        return Terminal([sys.executable, "-c", driver])

    def test_resume_with_the_id_claude_printed(self):
        with tempfile.TemporaryDirectory(prefix="cgt.") as tmp:
            t = self.start(tmp, "--resume", "abcd1234")
            try:
                shown = t.expect("TOOK-OVER: the pager fix is half done.")
                self.assertIn("abcd1234 is a Claude Code session, not a tandem one: taking over from it", shown)
                self.assertNotIn("Traceback", shown)
                t.expect("> ")
                t.send("/exit")
                t.expect("DIGEST-HAD-USER-REQUEST")
            finally:
                t.close()

    def test_continue_with_only_a_claude_session_offers_the_takeover(self):
        with tempfile.TemporaryDirectory(prefix="cgt.") as tmp:
            t = self.start(tmp, "--continue")
            try:
                shown = t.expect("Take over from it? [Enter = yes, n = no] ")
                self.assertIn("No tandem session here yet. There is a Claude Code session, abcd1234", shown)
                t.send("")
                t.expect("TOOK-OVER: the pager fix is half done.")
                t.expect("> ")
                t.send("/exit")
                t.expect("DIGEST-HAD-USER-REQUEST")
            finally:
                t.close()

    def test_plain_start_mentions_the_claude_session(self):
        with tempfile.TemporaryDirectory(prefix="cgt.") as tmp:
            t = self.start(tmp)
            try:
                t.expect("Claude Code was last active here 0 min ago; start with --from-claude")
                t.expect("\n> ")
                t.send("/exit")
                t.expect("NO-DIGEST")
            finally:
                t.close()


class Relay(unittest.TestCase):
    def start(self, tmp, limited_runs):
        ws, home = os.path.join(tmp, "ws"), os.path.join(tmp, "home")
        os.makedirs(ws)
        folder = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(ws)))
        os.makedirs(folder)
        transcript = os.path.join(folder, "sid-1234.jsonl")
        with open(transcript, "w") as f:
            f.write(json.dumps({"type": "user", "promptSource": "typed",
                                "message": {"role": "user", "content": "Fix the pager"}}) + "\n")
        for name, text in (("claude", FAKE_CLAUDE.format(tmp=tmp, here=HERE, transcript=transcript,
                                                         limited_runs=limited_runs)),
                           ("gpt", FAKE_GPT.format(tmp=tmp, here=HERE))):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(text)
            os.chmod(os.path.join(tmp, name), 0o755)
        env = {"HOME": home, "TANDEM_STATE": os.path.join(tmp, "state"), "PATH": "/usr/bin:/bin",
               "OPENAI_API_KEY": "not-a-real-key-tests-make-no-calls",
               "TANDEM_CLAUDE": os.path.join(tmp, "claude"),
               "TANDEM_SELF": f"{sys.executable} {os.path.join(tmp, 'gpt')}"}
        return Terminal([sys.executable, os.path.join(HERE, "cg.py"), "relay", "--model", "opus"],
                        cwd=ws, env=env)

    def runs(self, tmp):
        with open(os.path.join(tmp, "claude-runs.json")) as f:
            return json.load(f)

    def test_limit_then_gpt_then_back_to_the_same_claude_session(self):
        with tempfile.TemporaryDirectory(prefix="cgt.") as tmp:
            t = self.start(tmp, limited_runs=1)
            try:
                shown = t.expect("Carry on here with GPT (tandem)? [Enter = yes, n = no] ")
                self.assertIn("FAKE-CLAUDE run 1", shown)
                self.assertIn("Claude stopped on its usage limit (You've hit your session limit - resets 3:45pm).", shown)
                t.send("")                                             # one key: Enter
                shown = t.expect("GPT-TOOK-OVER: the pager fix is half done.")
                self.assertIn("Claude's limit is due to reset at 15:45; /back returns to it.", shown)
                self.assertIn("taking over from Claude Code session sid-1234", shown)
                t.expect("> ")
                t.send("add the changelog entry")
                t.expect("GPT-DID-IT: wrote the changelog entry.")
                t.expect("> ")
                t.send("/back")                                        # and one command to return
                t.expect("FAKE-CLAUDE run 2")
            finally:
                t.close()
            first, second = self.runs(tmp)
            self.assertEqual(first, ["--model", "opus"])               # Claude's own arguments pass through
            self.assertEqual(second[:2], ["--resume", "sid-1234"])     # the SAME session is resumed
            record = second[2]
            self.assertTrue(record.startswith("While this Claude Code session was not in use"))
            for needle in ("1. add the changelog entry", "- [x] changelog", "GPT-DID-IT: wrote the changelog entry."):
                self.assertIn(needle, record)
            self.assertLessEqual(len(record), 12000)
            self.assertEqual(t.proc.returncode, 0)                     # run 2 exited normally: relay ends

    def test_still_limited_on_return_continues_the_same_gpt_session(self):
        with tempfile.TemporaryDirectory(prefix="cgt.") as tmp:
            t = self.start(tmp, limited_runs=2)
            try:
                t.expect("Carry on here with GPT")
                t.send("y")
                t.expect("GPT-TOOK-OVER")
                t.expect("> ")
                t.send("/exit")
                t.expect("Go back to Claude now, with a record of what GPT did? [Enter = yes, n = no] ")
                t.send("")
                t.expect("FAKE-CLAUDE run 2")                           # resumed, and limited again
                t.expect("Carry on here with GPT")
                t.send("")
                shown = t.expect("> ")
                self.assertIn("continuing the GPT session from before", shown)
                self.assertNotIn("taking over from Claude Code session", shown)   # no second digest
                t.send("/exit")
                t.expect("Go back to Claude now")
                t.send("n")
                t.expect("later: claude --resume sid-1234")
            finally:
                t.close()
            self.assertEqual(len(self.runs(tmp)), 2)
            sessions = [n for n in os.listdir(os.path.join(tmp, "state", "runs")) if n.endswith(".json")]
            self.assertEqual(len(sessions), 1)                           # one GPT session, reused (not its .lock)

    def test_declining_and_ordinary_exits_do_nothing(self):
        with tempfile.TemporaryDirectory(prefix="cgt.") as tmp:
            t = self.start(tmp, limited_runs=1)
            try:
                shown = t.expect("Carry on here with GPT")
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")) as f:
                    how = (json.load(f).get("other_claude") or {}).get("how")
                if how:   # a second Claude account is configured: offered, and not as a continuation
                    self.assertIn(how, shown)
                    self.assertIn("it starts fresh: it will not have this conversation", shown)
                    self.assertIn("here, with this conversation: GPT (tandem)", shown)
                t.send("n")
                if how:
                    t.expect("for a new Claude session on")
            finally:
                t.close()
            self.assertEqual(len(self.runs(tmp)), 1)
            self.assertFalse(os.path.exists(os.path.join(tmp, "state", "runs")))
        with tempfile.TemporaryDirectory(prefix="cgt.") as tmp:
            t = self.start(tmp, limited_runs=0)                        # Claude just exits: no offer
            try:
                t.expect("FAKE-CLAUDE run 1")
            finally:
                t.close()
            self.assertNotIn("usage limit", t.seen)
            self.assertEqual(t.proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()

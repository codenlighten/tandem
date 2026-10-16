#!/usr/bin/env python3
"""Controls for what the user sees: colour only on a colour terminal, the working indicator
cleared before anything else is printed, a pasted block sent as one message, commands told
apart from paths, and the short result lines under tool calls. The model is a script.

  python3 test_ui.py
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

os.environ["OPENAI_API_KEY"] = "not-a-real-key-tests-make-no-calls"   # never the real one

import cg
import ui
from test_tty import HERE, Terminal

DRIVER = r'''
import json, os, sys, time
sys.path.insert(0, {here!r})
import cg
from test_loop import FakeApi, response, message, tool_call
cg.LEDGER_PATH = os.path.join({tmp!r}, "ledger.jsonl")
cg.RUNS_DIR = os.path.join({tmp!r}, "runs")
cg.spent_today_elsewhere = lambda cfg: 0.0
slow = lambda r: (lambda payload: (time.sleep(1.5), r)[1])
api = cg.call_api = FakeApi(
    slow(response(tool_call("write", path="a.txt", content="one\n"))),
    response(message("REPLY-1")),
    response(message("REPLY-2")),
    response(message("REPLY-3")),
)
sys.argv = ["cg.py", "chat", "--cwd", {ws!r}, "--memory-dir", os.path.join({tmp!r}, "mem")]
try:
    cg.main()
finally:
    with open(os.path.join({tmp!r}, "sent.json"), "w") as f:
        json.dump([r["input"][-1].get("content") for r in api.requests
                   if r["input"] and r["input"][-1].get("role") == "user"], f)
'''


def screen(raw):
    """What a person would see: carriage returns and line erases applied, colour removed."""
    lines = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        line = re.sub(r".*\r\x1b\[2K", "", line).split("\r")[-1]
        lines.append(re.sub(r"\x1b\[[0-9;]*m", "", line))
    return "\n".join(lines)


class AtAColourTerminal(unittest.TestCase):
    def test_a_session_as_a_person_sees_it(self):
        with tempfile.TemporaryDirectory(prefix="cgu.", dir="/tmp") as tmp:
            ws = os.path.join(tmp, "ws")
            os.makedirs(ws)
            script = os.path.join(tmp, "driver.py")
            with open(script, "w") as f:
                f.write(DRIVER.format(here=HERE, tmp=tmp, ws=ws))
            env = {k: v for k, v in os.environ.items() if k != "NO_COLOR"}
            # Terminal() forces TERM=dumb; this run needs a colour terminal.
            t = ColourTerminal([sys.executable, script], cwd=ws, env=dict(env, TERM="xterm-256color"))
            try:
                t.expect("> ")
                t.send("make a")
                raw = t.expect("[a]uto mode: ")
                self.assertIn("\x1b[", raw)                         # coloured
                self.assertIn("thinking", raw)                      # the indicator ran ...
                seen = screen(raw)
                self.assertNotIn("thinking", seen)                  # ... and left nothing behind
                self.assertIn("● write a.txt", seen)
                self.assertIn("+one", seen)
                self.assertEqual(seen.count("write a.txt"), 1)      # shown once, not again in the question
                t.send("y")
                t.expect("REPLY-1")
                t.expect("this turn")                               # the footer
                t.expect("> ")
                os.write(t.master, b"first line\nsecond line\n")    # a paste
                t.expect("REPLY-2")
                t.expect("> ")
                t.send("/bogus")
                t.expect("is not a command")
                t.expect("> ")
                t.send("/home/someone/x.py is broken")              # a path, not a command
                t.expect("REPLY-3")
                t.expect("> ")
                t.send("/exit")
                t.expect("Resume it with")
            finally:
                t.close()
            with open(os.path.join(tmp, "sent.json")) as f:
                sent = json.load(f)
            self.assertEqual(sent, ["make a", "first line\nsecond line", "/home/someone/x.py is broken"])


AGAIN = r'''
import os, sys
sys.path.insert(0, {here!r})
import cg
from test_loop import FakeApi, response, message, tool_call
cg.LEDGER_PATH = os.path.join({tmp!r}, "ledger.jsonl")
cg.RUNS_DIR = os.path.join({tmp!r}, "runs")
cg.spent_today_elsewhere = lambda cfg: 0.0
cg.call_api = FakeApi(
    response(tool_call("bash", command="touch x.txt")), response(message("REPLY-1")),
    response(tool_call("bash", command="touch x.txt")), response(message("REPLY-2")),
    response(tool_call("bash", command="touch y.txt")), response(message("REPLY-3")),
    response(tool_call("bash", command="git push")), response(message("REPLY-4")),
)
sys.argv = ["cg.py", "chat", "--cwd", {ws!r}, "--memory-dir", os.path.join({tmp!r}, "mem")]
cg.main()
'''


class DontAskAgain(unittest.TestCase):
    def test_only_that_exact_command_and_never_what_must_ask(self):
        with tempfile.TemporaryDirectory(prefix="cgu.", dir="/tmp") as tmp:
            ws = os.path.join(tmp, "ws")
            os.makedirs(ws)
            script = os.path.join(tmp, "driver.py")
            with open(script, "w") as f:
                f.write(AGAIN.format(here=HERE, tmp=tmp, ws=ws))
            t = Terminal([sys.executable, script], cwd=ws)
            try:
                t.expect("> ")
                t.send("one")
                t.expect("[d]on't ask again for this command / [a]uto mode: ")
                t.send("d")
                t.expect("REPLY-1")
                t.send("two")                                   # the same command: not asked
                self.assertNotIn("allow?", t.expect("REPLY-2"))
                t.send("three")                                 # another command: asked
                t.expect("[d]on't ask again for this command / [a]uto mode: ")
                t.send("n")
                t.expect("REPLY-3")
                t.send("four")                                  # publishing: asked, no 'again'
                shown = t.expect("allow?")
                shown += t.expect(": ")
                self.assertNotIn("don't ask again", shown)
                t.send("n")
                t.expect("REPLY-4")
                t.send("/exit")
                t.expect("Resume it with")
            finally:
                t.close()
            self.assertTrue(os.path.exists(os.path.join(ws, "x.txt")))
            self.assertFalse(os.path.exists(os.path.join(ws, "y.txt")))


class ColourTerminal(Terminal):
    def __init__(self, argv, cwd=None, env=None):
        import fcntl, pty, struct, subprocess, termios
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                     start_new_session=True, close_fds=True, cwd=cwd, env=env)
        os.close(slave)
        self.seen, self.pos = "", 0


class Pieces(unittest.TestCase):
    def test_no_colour_unless_a_colour_terminal(self):
        with mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(ui.paint("x", "red"), "x")             # not a terminal
        tty = mock.Mock(isatty=lambda: True)
        with mock.patch("sys.stdout", tty):
            with mock.patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": ""}):
                self.assertNotEqual(ui.paint("x", "red"), "x")
            with mock.patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": "1"}):
                self.assertEqual(ui.paint("x", "red"), "x")
            with mock.patch.dict(os.environ, {"TERM": "dumb", "NO_COLOR": ""}):
                self.assertEqual(ui.paint("x", "red"), "x")

    def test_result_lines(self):
        self.assertIsNone(ui.result("bash", "refused: no"))           # shown when it happened
        self.assertIn("3 lines", ui.result("read", "1\ta\n2\tb\n3\tc"))
        self.assertIn("0 files", ui.result("glob", "(no files match)"))
        self.assertIn("2 matching lines", ui.result("grep", "a:1:x\nb:2:y"))
        out = ui.result("bash", "\n".join(f"line {i}" for i in range(10)) + "\n[exit 2]")
        self.assertIn("line 0", out)
        self.assertNotIn("line 5", out)
        self.assertIn("+6 lines", out)
        self.assertIn("exit 2", out)
        self.assertNotIn("exit 0", ui.result("bash", "ok\n[exit 0]"))
        self.assertIn("done", ui.result("bash", "[exit 0]"))
        self.assertIn("killed", ui.result("bash", "x\n[killed after the 120 s timeout]"))
        self.assertIn("error", ui.result("edit", "error: `old` was not found"))

    def test_commands_and_paths(self):
        for text in ("/help", "/mode auto", "/bogus", "/?"):
            self.assertTrue(cg.is_command(text), text)
        for text in ("/home/me/x.py fails", "//help", "fix /tmp/a", "", "/ x"):
            self.assertFalse(cg.is_command(text), text)

    def test_glob_braces(self):
        self.assertEqual(cg.braces("src/*.{py,md}"), ["src/*.py", "src/*.md"])
        self.assertEqual(sorted(cg.braces("{a,b}/{c,d}")), ["a/c", "a/d", "b/c", "b/d"])
        self.assertEqual(cg.braces("plain/*.py"), ["plain/*.py"])

    def test_anything_printed_clears_the_indicator_first(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        out = Tty()
        with mock.patch("sys.stdout", out), mock.patch.dict(os.environ, {"TERM": "xterm", "NO_COLOR": ""}):
            ui.spin("thinking")
            import time
            time.sleep(0.35)
            ui.out("hello")
            time.sleep(0.25)                           # a stopped indicator writes nothing more
        text = out.getvalue()
        self.assertIn("thinking", text)
        self.assertTrue(screen(text).strip().endswith("hello"), repr(text))
        self.assertEqual(screen(text).count("thinking"), 0)

    def test_spinner_is_silent_off_a_terminal(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            ui.spin("thinking")
            ui.out("hello")
        self.assertEqual(out.getvalue(), "hello\n")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Controls for tools the model proposes: nothing is installed or run without the user's
yes, an approved tool cannot be changed afterwards, and a sandboxed one stays sandboxed.
No API call is made; the store is a temporary folder.

  python3 test_added_tools.py < /dev/null
"""
import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

os.environ["OPENAI_API_KEY"] = "not-a-real-key-tests-make-no-calls"   # never the real one

import added_tools
import cg

ECHO = "import json, sys\na = json.load(sys.stdin)\nprint('got', a['x'])\n"


def session(workspace, mode="auto", **extra):
    args = argparse.Namespace(cwd=workspace, mode=mode, net=False, tier="quick", model=None,
                              max_usd=0.0, shm=False, memory_dir=None, read=None, **extra)
    with contextlib.redirect_stdout(io.StringIO()):
        return cg.Session(cg.load_config(), args)


def tool(tool_name, s, /, **args):
    with contextlib.redirect_stdout(io.StringIO()):
        return s.run_tool({"name": tool_name, "arguments": json.dumps(args)})


def defn(name="echo", code=ECHO, **kw):
    kw.setdefault("ask_each_call", False)   # so a test's call runs without a person to ask
    return added_tools.make(name, "prints its argument x", {"type": "object", "properties": {
        "x": {"type": "string"}}}, code, **kw)


class AddedTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cgat.", dir="/tmp")
        self.ws = os.path.join(self.tmp.name, "ws")
        self.outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(self.ws)
        os.makedirs(self.outside)
        self.store = os.path.join(self.tmp.name, "store")
        patch = mock.patch.object(added_tools, "STORE", self.store)
        patch.start()
        self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, s):
        return {t["name"] for t in s.tools}

    def test_bad_definitions_are_refused(self):
        s = session(self.ws)
        for args, why in [
            (dict(name="Bad-Name"), "name"), (dict(name="bash"), "built-in"),
            (dict(parameters={"type": "string"}), "parameters"), (dict(code=" "), "code"),
            (dict(sandboxed="no"), "sandboxed"), (dict(description=""), "description"),
        ]:
            call = dict(name="echo", description="d", parameters={"type": "object"}, code=ECHO)
            call.update(args)
            out = tool("propose_tool", s, **call)
            self.assertIn("error", out)
            self.assertIn(why, out)
        self.assertEqual(added_tools.load(), ({}, []))

    def test_nothing_installed_without_a_person(self):
        s = session(self.ws, mode="auto")          # stdin is not a terminal in these tests
        out = tool("propose_tool", s, name="echo", description="d",
                   parameters={"type": "object"}, code=ECHO)
        self.assertIn("not installed", out)
        self.assertNotIn("echo", self.names(s))
        self.assertIn("no tool named", tool("echo", s, x="1"))
        self.assertEqual(added_tools.load(), ({}, []))
        self.assertEqual([d["name"] for d in added_tools.proposed()], ["echo"])
        self.assertNotIn("echo", self.names(session(self.ws)))

    def test_approved_in_the_session_works_at_once_and_later(self):
        s = session(self.ws)
        with mock.patch.object(cg.Session, "permit", return_value=True) as permit:
            out = tool("propose_tool", s, name="echo", description="prints x",
                       parameters={"type": "object"}, code=ECHO, ask_each_call=False)
        self.assertTrue(permit.call_args.kwargs.get("must_ask"))
        self.assertIn("installed", out)
        self.assertEqual([t["name"] for t in s.tools].count("echo"), 1)   # offered to the model now
        self.assertIn("got hi", tool("echo", s, x="hi"))
        s2 = session(self.ws)
        self.assertIn("echo", self.names(s2))
        self.assertIn("got again", tool("echo", s2, x="again"))
        self.assertEqual(added_tools.proposed(), [])

    def test_a_changed_tool_is_not_loaded_or_run(self):
        added_tools.approve(defn())
        s = session(self.ws)
        self.assertIn("got 1", tool("echo", s, x="1"))
        path = os.path.join(self.store, "approved", "echo.json")
        with open(path) as f:
            d = json.load(f)
        d["code"] = "print('changed')"
        with open(path, "w") as f:
            json.dump(d, f)
        out = tool("echo", s, x="1")
        self.assertIn("no longer matches", out)
        self.assertNotIn("changed", out.replace("no longer matches", ""))
        self.assertNotIn("echo", self.names(session(self.ws)))
        self.assertEqual(added_tools.load(), ({}, ["echo"]))

    def test_the_session_cannot_write_the_store(self):
        added_tools.approve(defn())
        s = session(self.ws)
        index = os.path.join(self.store, "approved.json")
        with open(index) as f:
            before = f.read()
        tool("write", s, path=index, content="{}")
        tool("bash", s, command=f"echo '{{}}' > {index}")
        with open(index) as f:
            self.assertEqual(f.read(), before)

    def test_store_inside_the_workspace_turns_them_off(self):
        with mock.patch.object(added_tools, "STORE", os.path.join(self.ws, "store")):
            added_tools.approve(defn())
            s = session(self.ws)
            self.assertNotIn("echo", self.names(s))
            with mock.patch.object(cg.Session, "permit", return_value=True):
                out = tool("propose_tool", s, name="echo2", description="d",
                           parameters={"type": "object"}, code=ECHO)
            self.assertIn("refused", out)

    def test_a_sandboxed_tool_stays_in_the_sandbox(self):
        target = os.path.join(self.outside, "escaped")
        code = (f"open({target!r}, 'w').write('x')\n")
        added_tools.approve(defn("writer", code=code))
        out = tool("writer", session(self.ws))
        self.assertFalse(os.path.exists(target), out)
        self.assertIn("PermissionError", out)          # it ran, and the sandbox stopped it
        net = "import urllib.request\nurllib.request.urlopen('http://1.1.1.1', timeout=5)\nprint('net ok')\n"
        added_tools.approve(defn("netless", code=net))
        out = tool("netless", session(self.ws))
        self.assertNotIn("net ok", out)
        self.assertIn("Traceback", out)

    def test_outside_the_sandbox_asks_a_person_each_call(self):
        target = os.path.join(self.outside, "done")
        code = f"open({target!r}, 'w').write('x')\nprint('ran')\n"
        added_tools.approve(defn("outer", code=code, sandboxed=False, ask_each_call=True))
        out = tool("outer", session(self.ws, mode="auto"))
        self.assertIn("refused", out)
        self.assertFalse(os.path.exists(target))
        s = session(self.ws, mode="auto")
        with mock.patch.object(cg.Session, "permit", return_value=True) as permit:
            self.assertIn("ran", tool("outer", s))
        self.assertTrue(permit.call_args.kwargs.get("must_ask"))
        self.assertTrue(os.path.exists(target))

    def test_outside_without_asking_only_when_approved_so_and_without_the_api_key(self):
        code = "import os\nprint('key' if 'OPENAI_API_KEY' in os.environ else 'no key')\n"
        added_tools.approve(defn("quiet", code=code, sandboxed=False, ask_each_call=False))
        self.assertIn("no key", tool("quiet", session(self.ws, mode="ask")))

    def test_summary_says_what_it_can_reach(self):
        self.assertIn("OUTSIDE the sandbox", added_tools.summary(defn(sandboxed=False)))
        self.assertIn("no network", added_tools.summary(defn()))
        self.assertIn("web access", added_tools.summary(defn(net=True)))
        self.assertFalse(defn(sandboxed=False, net=True)["net"])
        self.assertIn("print('got', a['x'])", added_tools.summary(defn()))

    def test_ask_each_call_asks_a_person_even_in_auto_mode(self):
        added_tools.approve(defn(ask_each_call=True))
        s = session(self.ws, mode="auto")
        self.assertIn("refused", tool("echo", s, x="1"))
        with mock.patch.object(cg.Session, "permit", return_value=True) as permit:
            self.assertIn("got 1", tool("echo", s, x="1"))
        self.assertTrue(permit.call_args.kwargs.get("must_ask"))

    def test_the_user_sees_every_argument(self):
        added_tools.approve(defn(ask_each_call=True))
        s = session(self.ws, mode="auto")
        with mock.patch.object(cg.Session, "permit", return_value=True) as permit:
            self.assertIn("at most", tool("echo", s, x="a" * 5000))
            permit.assert_not_called()
            tool("echo", s, x="harmless" + " " * 700 + "\x1b[2Kthe real target")
        asked = permit.call_args.args[1]
        self.assertIn("the real target", asked)
        self.assertNotIn("\x1b", asked)

    def test_control_characters_cannot_rewrite_the_approval(self):
        d = defn(code="print('fine')\r\x1b[1A\x1b[2Kimport os  # \u202e evil\n",
                 sandboxed=False)
        text = added_tools.summary(d)
        for c in "\r\x1b\u202e":
            self.assertNotIn(c, text)
        self.assertIn("\\x1b", text)
        self.assertIn("control characters", text)
        self.assertIn("OUTSIDE the sandbox", text.splitlines()[0])

    def test_risky_code_is_pointed_out(self):
        text = added_tools.summary(defn(code="import subprocess\nexec(open('x.py').read())\n",
                                        sandboxed=False))
        self.assertIn("runs other programs", text)
        self.assertIn("evaluates or imports", text)
        self.assertIn("can change after you say yes", text)
        self.assertNotIn("look closely", added_tools.summary(defn()))

    def test_unreviewable_sizes_are_refused(self):
        with self.assertRaises(ValueError):
            defn(code="x = 1\n" * 5000)
        with self.assertRaises(ValueError):
            added_tools.make("big", "d", {"type": "object", "description": "x" * 6000}, ECHO)

    def test_links_in_the_store_are_not_followed(self):
        added_tools.approve(defn())
        path = os.path.join(self.store, "approved", "echo.json")
        elsewhere = os.path.join(self.outside, "echo.json")
        os.replace(path, elsewhere)
        os.symlink(elsewhere, path)
        self.assertEqual(added_tools.load(), ({}, ["echo"]))

    def add(self, path, answer):
        args = argparse.Namespace(action="add", name=path)
        out = io.StringIO()
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value=answer), contextlib.redirect_stdout(out):
            try:
                cg.run_tools(args, cg.load_config())
            except SystemExit as e:
                out.write(str(e.code))
        return out.getvalue()

    def test_tools_add_installs_a_reviewed_definition_on_yes(self):
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "tools", "github.json")
        self.assertIn("not installed", self.add(path, "n"))
        self.assertEqual(added_tools.load(), ({}, []))
        out = self.add(path, "y")
        self.assertIn("OUTSIDE the sandbox", out)
        self.assertIn("installed github", out)
        tools, bad = added_tools.load()
        with open(os.path.join(here, "tools", "github.py")) as f:
            self.assertEqual(tools["github"]["code"], f.read())       # the program itself, pinned
        self.assertIn("already installed", self.add(path, "y"))
        self.assertIn("github", {t["name"] for t in session(self.ws).tools})

    def test_remove(self):
        added_tools.approve(defn())
        self.assertTrue(added_tools.remove("echo"))
        self.assertEqual(added_tools.load(), ({}, []))
        self.assertFalse(added_tools.remove("echo"))


if __name__ == "__main__":
    unittest.main()

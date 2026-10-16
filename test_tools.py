#!/usr/bin/env python3
"""Controls for the tools as the model reaches them, with no API call: each tool is
driven directly on a temporary workspace that holds a made-up secret.

  python3 test_tools.py < /dev/null
"""
import argparse
import base64
import contextlib
import io
import json
import os
import tempfile
import unittest

os.environ["OPENAI_API_KEY"] = "not-a-real-key-tests-make-no-calls"   # never the real one

import added_tools
import cg

# Tools approved on this machine must not change what these tests see or run.
added_tools.STORE = tempfile.mkdtemp(prefix="cg-no-tools.")

CANARY = "sk-CANARY-not-a-real-key-0123456789"


def session(workspace, mode, read=None):
    args = argparse.Namespace(cwd=workspace, mode=mode, net=False, tier="quick", model=None,
                              max_usd=0.0, shm=False, memory_dir=None, read=read)
    return cg.Session(cg.load_config(), args)


class Tools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cgt.", dir="/tmp")
        self.ws = os.path.join(self.tmp.name, "ws")
        self.outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(os.path.join(self.ws, "sub"))
        os.makedirs(self.outside)
        for p in (".env", "sub/.env.local"):
            with open(os.path.join(self.ws, p), "w") as f:
                f.write(f"# comment\nOPENAI_API_KEY={CANARY}\nSHORT=abc\n")
        with open(os.path.join(self.ws, "a.py"), "w") as f:
            f.write("x = 1\ny = 1\n")
        self.s = session(self.ws, "auto")

    def tearDown(self):
        self.tmp.cleanup()

    def tool(self, name, s=None, **args):
        with contextlib.redirect_stdout(io.StringIO()):
            return (s or self.s).run_tool({"name": name, "arguments": json.dumps(args)})

    def test_secret_never_returned(self):
        attempts = [
            ("read", {"path": ".env"}), ("read", {"path": "sub/.env.local"}),
            ("bash", {"command": "cat .env"}), ("bash", {"command": "cat .e*"}),
            ("bash", {"command": "cat sub/.e*"}),
            ("bash", {"command": "python3 -c \"print(open('.' + 'env').read())\""}),
            ("bash", {"command": "grep -r CANARY ."}), ("bash", {"command": "tar c . | strings"}),
            ("bash", {"command": "grep -h OPENAI .e* | cut -d= -f2 | tr -d '\\n' | base64 -w0"}),
            ("bash", {"command": "grep -h OPENAI .e* | cut -d= -f2 | tr -d '\\n' | xxd -p -c 256"}),
            ("bash", {"command": "grep -h OPENAI .e* | cut -d= -f2 | rev"}),
            ("grep", {"pattern": "CANARY"}), ("grep", {"pattern": "CANARY", "glob": ".env"}),
            ("grep", {"pattern": "CANARY", "path": ".env"}), ("glob", {"pattern": "**/.env*"}),
            ("glob", {"pattern": ".*"}),
        ]
        for name, args in attempts:
            with self.subTest(tool=name, args=args):
                out = self.tool(name, **args)
                self.assertNotIn(CANARY, out)
                self.assertNotIn("CANARY", out)
                self.assertNotIn("YRANAC", out)
                self.assertNotIn(base64.b64encode(CANARY.encode()).decode()[:-4], out)
                self.assertNotIn(CANARY.encode().hex(), out)
        self.assertNotIn(cg.api_key(), self.tool("bash", command="env"))

    def test_writes_stay_inside(self):
        for name, args in (("write", {"path": self.outside + "/x", "content": "x"}),
                           ("write", {"path": "../outside/x", "content": "x"}),
                           ("write", {"path": "~/cg-test-x", "content": "x"}),
                           ("write", {"path": ".env", "content": "x"}),
                           ("edit", {"path": "/etc/hostname", "old": "a", "new": "b"}),
                           ("bash", {"command": f"echo x > {self.outside}/x"})):
            with self.subTest(tool=name, args=args):
                out = self.tool(name, **args)
                self.assertTrue("refused" in out or "denied" in out.lower(), out)
        self.assertEqual(os.listdir(self.outside), [])
        self.assertFalse(os.path.exists(os.path.expanduser("~/cg-test-x")))

    def test_edit_and_write(self):
        self.assertIn("occurs 2 times", self.tool("edit", path="a.py", old="= 1", new="= 2"))
        self.assertIn("edited", self.tool("edit", path="a.py", old="x = 1", new="x = 5"))
        self.assertIn("not found", self.tool("edit", path="a.py", old="zzz", new="q"))
        self.assertIn("wrote", self.tool("write", path="new/b.txt", content="hello\n"))
        self.assertIn("     1\tx = 5", self.tool("read", path="a.py"))
        self.assertIn("[exit 0]", self.tool("bash", command="cat new/b.txt && python3 a.py"))

    def test_links_out_of_the_workspace(self):
        target = os.path.join(self.outside, "t.txt")
        with open(target, "w") as f:
            f.write("OUTSIDE")
        os.symlink(self.outside, os.path.join(self.ws, "dirlink"))
        os.symlink(target, os.path.join(self.ws, "filelink"))
        os.link(target, os.path.join(self.ws, "hardlink"))
        for name, args in (("write", {"path": "dirlink/t.txt", "content": "x"}),
                           ("write", {"path": "filelink", "content": "x"}),
                           ("write", {"path": "hardlink", "content": "x"}),
                           ("edit", {"path": "hardlink", "old": "OUT", "new": "x"}),
                           ("read", {"path": "dirlink/t.txt"}), ("read", {"path": "filelink"})):
            with self.subTest(tool=name, args=args):
                out = self.tool(name, **args)
                self.assertTrue("refused" in out or "error" in out, out)
                self.assertNotIn("OUTSIDE", out)
        with open(target) as f:
            self.assertEqual(f.read(), "OUTSIDE")
        # The kernel check itself, with the path check bypassed as a race would.
        import policy
        with self.assertRaises(OSError):
            policy.open_beneath(self.ws, os.path.join(self.ws, "dirlink", "t.txt"), os.O_RDONLY)
        with self.assertRaises(OSError):
            policy.open_beneath(self.ws, os.path.join(self.ws, "filelink"), os.O_WRONLY)
        os.symlink("a.py", os.path.join(self.ws, "inner"))
        self.assertIn("x = 1", self.tool("read", path="inner"))

    def test_git_hook_change_is_flagged(self):
        os.makedirs(os.path.join(self.ws, ".git", "hooks"))
        out = self.tool("bash", command="printf '#!/bin/sh\n' > .git/hooks/pre-commit")
        self.assertIn("warning shown to the user", out)
        self.assertNotIn("warning", self.tool("bash", command="echo hi > note.txt"))

    def test_nothing_outlives_a_command(self):
        # A process that starts its own session leaves the process group; only the
        # cgroup still holds it. It records its own PID, so no other process is mistaken
        # for it.
        code = ("import os, sys, time\n"
                "if os.fork(): sys.exit()\n"
                "os.setsid(); open('pid', 'w').write(str(os.getpid())); time.sleep(300)\n")
        with open(os.path.join(self.ws, "detach.py"), "w") as f:
            f.write(code)
        out = self.tool("bash", command="python3 detach.py > /dev/null 2>&1; sleep 0.5; cat pid")
        with open(os.path.join(self.ws, "pid")) as f:
            pid = int(f.read())
        import time
        time.sleep(0.5)
        try:
            with open(f"/proc/{pid}/stat") as f:
                state = f.read().rsplit(")", 1)[1].split()[0]
        except OSError:
            state = "gone"
        self.assertIn(state, ("gone", "Z"), out)

    def test_linked_dependencies_are_readable_and_nothing_else_outside(self):
        # A project whose node_modules is a link to a shared folder elsewhere: its tests could
        # not run in the sandbox, which is what stopped the first real hand-over's verification.
        shared = os.path.join(self.outside, "shared", "node_modules")
        os.makedirs(os.path.join(shared, "left-pad"))
        with open(os.path.join(shared, "left-pad", "index.js"), "w") as f:
            f.write("module.exports = 'LINKED-DEP';\n")
        with open(os.path.join(self.outside, "private.txt"), "w") as f:
            f.write("PRIVATE\n")
        tool_dir = os.path.join(self.outside, "tools")
        os.makedirs(tool_dir)
        with open(os.path.join(tool_dir, "verify.sh"), "w") as f:
            f.write("#!/bin/sh\necho EXTERNAL-TOOL-RAN\n")
        os.chmod(os.path.join(tool_dir, "verify.sh"), 0o755)
        os.symlink(shared, os.path.join(self.ws, "node_modules"))
        os.symlink(self.outside, os.path.join(self.ws, "sneaky"))        # not a dependency name: not followed
        before = self.tool("bash", command="cat node_modules/left-pad/index.js")
        self.assertIn("LINKED-DEP", self.tool("bash", s=session(self.ws, "auto"), command="cat node_modules/left-pad/index.js"))
        self.assertNotIn("LINKED-DEP", before)                             # the session made before the link
        s = session(self.ws, "auto", read=[tool_dir])
        self.assertIn("EXTERNAL-TOOL-RAN", self.tool("bash", s=s, command=f"{tool_dir}/verify.sh"))
        self.assertNotIn("PRIVATE", self.tool("bash", s=s, command=f"cat sneaky/private.txt {self.outside}/private.txt"))
        self.assertIn("Permission denied", self.tool("bash", s=s, command=f"touch {shared}/left-pad/x"))   # read, not write
        with self.assertRaises(SystemExit):
            session(self.ws, "auto", read=[os.path.expanduser("~/.ssh")])

    def test_denied_commands_do_not_run(self):
        marker = os.path.join(self.ws, "ran")
        for cmd in (f"touch {marker}; pkill -f nothing-matches-this",
                    f"touch {marker} && docker ps", f"touch {marker}; sudo -n true"):
            with self.subTest(cmd=cmd):
                self.assertIn("refused", self.tool("bash", command=cmd))
                self.assertFalse(os.path.exists(marker))

    def test_nobody_to_ask(self):
        # stdin is not a terminal here, so anything that needs a yes is refused.
        ask = session(self.ws, "ask")
        self.assertIn("refused", self.tool("bash", s=ask, command="touch made"))
        self.assertIn("refused", self.tool("write", s=ask, path="made", content="x"))
        self.assertIn("[exit 0]", self.tool("bash", s=ask, command="ls"))
        self.assertIn("refused", self.tool("bash", command="git push"))
        self.assertFalse(os.path.exists(os.path.join(self.ws, "made")))

    def test_timeout_and_bad_calls(self):
        self.assertIn("killed after the 1 s timeout", self.tool("bash", command="sleep 20", timeout=1))
        self.assertIn("no tool named", self.tool("nope"))
        self.assertIn("bad arguments", self.tool("read", wrong="x"))
        out = self.tool("bash", command="python3 -c \"print('a' * 100000)\"")
        self.assertLess(len(out), 31000)


if __name__ == "__main__":
    unittest.main()

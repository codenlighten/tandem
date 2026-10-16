#!/usr/bin/env python3
"""The evaluation's grader runs what the agent wrote, so it must run confined.

Each test plants code in a workspace that tries to reach outside it, runs it the way the grader
does (evals.confined, evals.sh, evals.tests_pass), and checks it could not. A control runs the
same code unconfined to show the attempt is real.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import evals


class GraderSandbox(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(t.cleanup)
        self.ws = os.path.join(t.name, "ws")
        self.outside = os.path.join(t.name, "outside")
        os.makedirs(self.ws)
        os.makedirs(self.outside)

    def test_cannot_write_outside_the_workspace(self):
        target = os.path.join(self.outside, "pwned")
        code = f"open({target!r}, 'w').write('x')"
        subprocess.run([sys.executable, "-c", code])               # control: unconfined it works
        self.assertTrue(os.path.exists(target))
        os.unlink(target)
        rc, _, err = evals.confined(self.ws, [sys.executable, "-c", code])
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(target))
        self.assertIn("Permission", err)

    def test_can_write_in_the_workspace_and_its_temp_folder(self):
        code = ("import tempfile, pathlib; pathlib.Path('inside.txt').write_text('ok'); "
                "t = tempfile.NamedTemporaryFile(); print('temp ok')")
        rc, out, err = evals.confined(self.ws, [sys.executable, "-c", code])
        self.assertEqual((rc, out.strip()), (0, "temp ok"), err)
        self.assertTrue(os.path.exists(os.path.join(self.ws, "inside.txt")))

    def test_cannot_read_other_folders(self):
        secret = os.path.join(self.outside, "secret.txt")
        with open(secret, "w") as f:
            f.write("hunter2\n")
        rc, out, _ = evals.confined(self.ws, ["cat", secret])
        self.assertNotEqual(rc, 0)
        self.assertNotIn("hunter2", out)

    def test_no_credentials_in_the_environment(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-leak", "PYTHONPATH": "/x",
                                          "LD_PRELOAD": "/y", "DATABASE_URL": "postgres://u:p@h/db"}):
            rc, out, _ = evals.confined(self.ws, "env")
        self.assertEqual(rc, 0)
        for leak in ("sk-test-leak", "PYTHONPATH", "LD_PRELOAD", "DATABASE_URL"):
            self.assertNotIn(leak, out)

    def test_no_network(self):
        code = ("import socket\ntry:\n    socket.create_connection(('127.0.0.1', 9), 2)\n"
                "except OSError as e:\n    print('blocked', type(e).__name__)\n")
        rc, out, _ = evals.confined(self.ws, [sys.executable, "-c", code])
        self.assertIn("blocked", out)

    def test_timeout_kills_the_command_and_its_children(self):
        marker = os.path.join(self.ws, "late")
        rc, _, _ = evals.confined(self.ws, f"(sleep 4; touch {marker}) & sleep 30", timeout=1)
        self.assertEqual(rc, 124)
        import time
        time.sleep(4.5)
        self.assertFalse(os.path.exists(marker))

    def test_a_detached_child_is_killed_too(self):
        marker = os.path.join(self.ws, "survivor")
        code = ("import os, time\nif os.fork() == 0:\n    os.setsid()\n    time.sleep(3)\n"
                f"    open({marker!r}, 'w').write('x')\n    os._exit(0)\n")
        rc, _, _ = evals.confined(self.ws, [sys.executable, "-c", code], timeout=60)
        import time
        time.sleep(4)
        if evals.cg.cgroup_available():
            self.assertFalse(os.path.exists(marker), "a child that left the process group survived")

    def test_output_is_capped(self):
        rc, out, _ = evals.confined(self.ws, [sys.executable, "-c", "print('a' * 5_000_000)"], limit=1000)
        self.assertEqual(len(out), 1000)

    def test_tests_that_attack_fail_to_do_harm(self):
        target = os.path.join(self.outside, "from-test")
        with open(os.path.join(self.ws, "test_x.py"), "w") as f:
            f.write("import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n"
                    f"        open({target!r}, 'w').write('x')\n")
        self.assertFalse(evals.tests_pass(self.ws))
        self.assertFalse(os.path.exists(target))

    def test_git_in_a_planted_repo_cannot_escape(self):
        marker = os.path.join(self.outside, "filter-ran")
        evals.git_init(self.ws)
        subprocess.run(["git", "config", "filter.x.clean", f"touch {marker}; cat"], cwd=self.ws, check=True)
        with open(os.path.join(self.ws, ".gitattributes"), "w") as f:
            f.write("* filter=x\n")
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.write("a\n")
        evals.sh(self.ws, "git status --short; git add -A")
        self.assertFalse(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main()

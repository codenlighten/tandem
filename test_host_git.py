#!/usr/bin/env python3
"""Git run on the host (outside the sandbox) must not run anything the workspace's config names.

The agent can edit .git/config, so a clean filter, an fsmonitor command, a hook or a textconv
or external diff driver planted there would run unsandboxed when a hand-back is built. Each
test plants one, calls the real function, and checks that the marker file was not created.
"""
import importlib.util
import os
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cg", os.path.join(HERE, "cg.py"))
import sys
sys.path.insert(0, HERE)
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, check=True).stdout


class HostGit(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.ws = os.path.join(self.t.name, "ws")
        os.mkdir(self.ws)
        self.marker = os.path.join(self.t.name, "ran")
        git(self.ws, "init", "-q", "-b", "main")
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.write("one\n")
        git(self.ws, "add", "-A")
        git(self.ws, "commit", "-qm", "first")

    def plant(self, key, value=None):
        git(self.ws, "config", key, value or f"touch {self.marker}; cat")

    def assertNotRun(self):
        self.assertFalse(os.path.exists(self.marker), "a command named in the workspace's config ran")

    def test_clean_filter_does_not_run(self):
        self.plant("filter.x.clean")
        with open(os.path.join(self.ws, ".gitattributes"), "w") as f:
            f.write("* filter=x\n")
        with open(os.path.join(self.ws, "b.txt"), "w") as f:
            f.write("two\n")
        tree = cg.git_tree(self.ws)
        self.assertTrue(tree)
        self.assertNotRun()

    def test_fsmonitor_and_hooks_do_not_run(self):
        self.plant("core.fsmonitor", f"touch {self.marker}; echo")
        hooks = os.path.join(self.t.name, "hooks")
        os.mkdir(hooks)
        hook = os.path.join(hooks, "reference-transaction")
        with open(hook, "w") as f:
            f.write(f"#!/bin/sh\ntouch {self.marker}\n")
        os.chmod(hook, 0o755)
        self.plant("core.hooksPath", hooks)
        cg.takeover_snapshot(self.ws, "s1")
        self.assertNotRun()

    def test_textconv_and_external_diff_do_not_run(self):
        snap = cg.takeover_snapshot(self.ws, "s2")
        oid = git(self.ws, "rev-parse", "refs/tandem/takeover/s2").strip()
        self.assertEqual(snap, oid)
        self.plant("diff.x.textconv")
        self.plant("diff.external", f"{self.marker}.sh")
        with open(os.path.join(self.ws, ".gitattributes"), "w") as f:
            f.write("* diff=x\n")
        with open(os.path.join(self.ws, "a.txt"), "w") as f:
            f.write("changed\n")
        out = cg.takeover_diff({"snapshot": oid, "workspace": self.ws})
        self.assertIn("a.txt", out)
        self.assertNotRun()

    def test_tree_is_real_and_in_the_repository(self):
        with open(os.path.join(self.ws, "new.txt"), "w") as f:
            f.write("untracked\n")
        tree = cg.git_tree(self.ws)
        names = git(self.ws, "ls-tree", "-r", "--name-only", tree).split()
        self.assertEqual(sorted(names), ["a.txt", "new.txt"])
        self.assertEqual(git(self.ws, "status", "--porcelain").strip(), "?? new.txt")  # real index untouched

    def test_ignore_rules_and_force_added_files(self):
        with open(os.path.join(self.ws, ".gitignore"), "w") as f:
            f.write("*.log\n")
        with open(os.path.join(self.ws, ".git", "info", "exclude"), "a") as f:
            f.write("private.txt\n")
        for name in ("x.log", "private.txt", "kept.env"):
            with open(os.path.join(self.ws, name), "w") as f:
                f.write("data\n")
        git(self.ws, "add", "-f", "kept.env")
        names = git(self.ws, "ls-tree", "-r", "--name-only", cg.git_tree(self.ws)).split()
        self.assertIn("kept.env", names)          # tracked although it would be ignored
        self.assertNotIn("x.log", names)          # .gitignore honoured
        self.assertNotIn("private.txt", names)    # .git/info/exclude honoured

    def test_linked_worktree(self):
        other = os.path.join(self.t.name, "wt")
        git(self.ws, "worktree", "add", "-q", "-b", "side", other)
        with open(os.path.join(other, "w.txt"), "w") as f:
            f.write("in the worktree\n")
        self.plant("filter.x.clean")
        with open(os.path.join(other, ".gitattributes"), "w") as f:
            f.write("* filter=x\n")
        names = git(self.ws, "ls-tree", "-r", "--name-only", cg.git_tree(other)).split()
        self.assertIn("w.txt", names)
        self.assertNotRun()

    def test_core_worktree_cannot_redirect_the_root(self):
        elsewhere = os.path.join(self.t.name, "elsewhere")
        os.mkdir(elsewhere)
        with open(os.path.join(elsewhere, "secret.txt"), "w") as f:
            f.write("not in the workspace\n")
        self.plant("core.worktree", elsewhere)
        self.assertIsNone(cg.git_workspace(self.ws))

    def test_global_ignore_file_is_honoured(self):
        xdg = os.path.join(self.t.name, "xdg")
        os.makedirs(os.path.join(xdg, "git"))
        with open(os.path.join(xdg, "git", "ignore"), "w") as f:
            f.write("*.secret\n")
        with open(os.path.join(self.ws, "k.secret"), "w") as f:
            f.write("x\n")
        from unittest import mock
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": xdg}):
            names = git(self.ws, "ls-tree", "-r", "--name-only", cg.git_tree(self.ws)).split()
        self.assertNotIn("k.secret", names)

    def test_not_a_repository(self):
        plain = os.path.join(self.t.name, "plain")
        os.mkdir(plain)
        self.assertIsNone(cg.git_tree(plain))


if __name__ == "__main__":
    unittest.main()

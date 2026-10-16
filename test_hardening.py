#!/usr/bin/env python3
"""Smaller hardening rules: the environment commands get, the read-only classifier, a session
that could write the harness itself, and the exit status of an unattended run."""
import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cg
import policy


class Environment(unittest.TestCase):
    def test_loader_and_startup_hooks_are_dropped(self):
        env = {"PATH": "/usr/bin", "LD_PRELOAD": "/x.so", "BASH_ENV": "/x", "PYTHONSTARTUP": "/x",
               "NODE_OPTIONS": "--require /x", "GIT_SSH_COMMAND": "x", "LANG": "C"}
        self.assertEqual(policy.scrub_env(env), {"PATH": "/usr/bin", "LANG": "C"})

    def test_a_password_in_a_url_is_dropped_whatever_the_name(self):
        env = {"DATABASE_URL": "postgres://app:hunter2@db.internal/app", "REDIS": "redis://u:p@h:6379",
               "HOMEPAGE": "https://example.com/docs", "API_BASE": "http://localhost:8080"}
        self.assertEqual(sorted(policy.scrub_env(env)), ["API_BASE", "HOMEPAGE"])

    def test_normal_variables_survive(self):
        env = {"PYTHONPATH": "/work/lib", "EDITOR": "vim", "HOME": "/home/u"}
        self.assertEqual(policy.scrub_env(env), env)


class Classifier(unittest.TestCase):
    def verdict(self, command):
        return policy.check_command(command)[0]

    def test_commands_that_write_or_run_something_are_not_read_only(self):
        for command in ("sort -o out.txt in.txt", "sort -ofile in.txt", "sort -uo out.txt in.txt",
                        "sort --output=out.txt in.txt", "sort --compress-program=/tmp/x in.txt",
                        "find . -fls out.txt", "find . -fprint out.txt", "find . -okdir rm {} ;",
                        "rg --pre ./x foo .", "rg --pre=./x foo ."):
            with self.subTest(command=command):
                self.assertNotEqual(self.verdict(command), "readonly")

    def test_plain_reads_still_are(self):
        for command in ("sort in.txt", "sort -u in.txt", "find . -name '*.py'", "rg foo .", "grep -rn foo ."):
            with self.subTest(command=command):
                self.assertEqual(self.verdict(command), "readonly")


class HarnessWrites(unittest.TestCase):
    def test_a_session_that_could_write_the_harness_does_not_start(self):
        with self.assertRaises(SystemExit) as e:
            cg.refuse_harness_writes([os.path.dirname(cg.HERE)])
        self.assertIn("harness", str(e.exception))
        with self.assertRaises(SystemExit):
            cg.refuse_harness_writes([cg.HERE])

    def test_the_state_folder_counts_too(self):
        with tempfile.TemporaryDirectory() as t, mock.patch.object(cg, "STATE", t):
            with self.assertRaises(SystemExit):
                cg.refuse_harness_writes([t])

    def test_an_unrelated_workspace_is_fine(self):
        with tempfile.TemporaryDirectory() as t:
            cg.refuse_harness_writes([t, "/tmp/scratch"])

    def test_a_checkout_inside_the_harness_folder_is_fine(self):
        cg.refuse_harness_writes([os.path.join(cg.HERE, "lab")])

    def test_the_flag_allows_it_with_a_warning(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cg.refuse_harness_writes([cg.HERE], allowed=True)
        self.assertIn("warning", out.getvalue())


class ExitStatus(unittest.TestCase):
    def run_task(self, outcome):
        session = mock.MagicMock(spent=0, calls=0, id="x")
        if isinstance(outcome, BaseException):
            session.turn.side_effect = outcome
        else:
            session.turn.return_value = outcome
        args = argparse.Namespace(from_claude=False, claude_session=None, task="do it")
        with mock.patch.object(cg, "preflight"), mock.patch.object(cg, "Session", return_value=session), \
                mock.patch.object(cg, "banner"), mock.patch.object(cg, "write_handback"), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as e:
            cg.run_task(args, {})
        return e.exception.code

    def test_finished_is_zero(self):
        self.assertEqual(self.run_task("done"), 0)

    def test_stopped_without_finishing_is_not_zero(self):
        self.assertEqual(self.run_task(None), 1)

    def test_budget_and_interrupt(self):
        self.assertEqual(self.run_task(cg.Budget("over")), 1)
        self.assertEqual(self.run_task(KeyboardInterrupt()), 130)


if __name__ == "__main__":
    unittest.main()

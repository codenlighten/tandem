#!/usr/bin/env python3
"""Controls for tools/github.py, with a local bare repository standing in for GitHub and
a fake gh on PATH: nothing here reaches the network or a real account.

  python3 test_github_tool.py
"""
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("github_tool", os.path.join(HERE, "tools", "github.py"))
gt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gt)
REAL_ACCOUNT = gt.account

FAKE_GH = """#!/bin/sh
printf '%s\\n' "$@" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "pr create") echo https://github.com/octocat/demo/pull/7 ;;
  "pr list") echo '[{"number":7,"title":"t","state":"OPEN","headRefName":"b","baseRefName":"main","url":"u"}]' ;;
esac
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "octocat",
                               "GIT_AUTHOR_EMAIL": "greg@smartledger.technology",
                               "GIT_COMMITTER_NAME": "octocat",
                               "GIT_COMMITTER_EMAIL": "greg@smartledger.technology"}).stdout.strip()


class GitHubTool(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cggh.")
        t = self.tmp.name
        self.hub = os.path.join(t, "hub.git")
        git(t, "init", "-q", "--bare", "-b", "main", self.hub)
        self.ws = os.path.join(t, "ws")
        git(t, "init", "-q", "-b", "main", self.ws)
        git(self.ws, "remote", "add", "origin", "git@github.com:octocat/demo.git")
        self.commit("a.txt", "one\n", "First")
        bin_dir = os.path.join(t, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "gh"), "w") as f:
            f.write(FAKE_GH)
        os.chmod(os.path.join(bin_dir, "gh"), 0o755)
        self.gh_log = os.path.join(t, "gh.log")
        self.marker = os.path.join(t, "ran")
        for p in (mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ["PATH"],
                                               "FAKE_GH_LOG": self.gh_log}),
                  mock.patch.object(gt, "ssh_url", lambda owner, name: self.hub),
                  mock.patch.object(gt, "logins", lambda: ("octocat", "octocat")),
                  mock.patch.object(gt, "account", lambda: "octocat")):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, path, text, message, ws=None):
        ws = ws or self.ws
        with open(os.path.join(ws, path), "w") as f:
            f.write(text)
        git(ws, "add", path)
        git(ws, "commit", "-q", "-m", message)
        return git(ws, "rev-parse", "HEAD")

    def call(self, ws=None, **a):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = gt.main(io.StringIO(json.dumps(a)), ws or self.ws)
        return code, out.getvalue()

    def on_hub(self, branch="main"):
        r = subprocess.run(["git", "-C", self.hub, "rev-parse", "--verify", "-q", branch],
                           capture_output=True, text=True)
        return r.stdout.strip() or None

    def test_push_a_new_branch_then_a_fast_forward(self):
        code, out = self.call(action="push", branch="main")
        self.assertEqual(code, 0, out)
        self.assertIn("a new branch on GitHub", out)
        self.assertEqual(self.on_hub(), git(self.ws, "rev-parse", "HEAD"))
        self.assertIn(f"git update-ref refs/remotes/origin/main {self.on_hub()}", out)
        self.assertFalse(os.path.exists(os.path.join(self.ws, ".git", "refs", "remotes")))
        head = self.commit("a.txt", "two\n", "Second")
        code, out = self.call(action="push", branch="main")
        self.assertEqual(code, 0, out)
        self.assertIn("fast-forward", out)
        self.assertEqual(self.on_hub(), head)
        self.assertIn("already on GitHub", self.call(action="push", branch="main")[1])

    def test_status_shows_what_would_go_and_sends_nothing(self):
        code, out = self.call(action="status", branch="main")
        self.assertEqual(code, 0, out)
        self.assertIn("octocat/demo", out)
        self.assertIn("1 new commits", out)
        self.assertIn("First", out)
        self.assertIsNone(self.on_hub())

    def test_no_force_push(self):
        self.call(action="push", branch="main")
        theirs = self.on_hub()
        git(self.ws, "commit", "-q", "--amend", "-m", "Rewritten")
        code, out = self.call(action="push", branch="main")
        self.assertEqual(code, 1)
        self.assertIn("force", out)
        self.assertEqual(self.on_hub(), theirs)

    def test_commits_that_credit_an_ai_are_refused(self):
        for message in ("Fix\n\nCo-Authored-By: Claude Opus <noreply@anthropic.com>",
                        "Fix\n\n\U0001f916 Generated with [Claude Code](https://claude.com)",
                        "Fix\n\nAssisted-by: gpt-6-sol", "Fix\n\nWritten with Claude",
                        "Fix\n\nCo-authored-by: Gemini <g@example.com>", "AI-assisted refactor",
                        "Fix\n\nBuilt using ChatGPT", "Fix\n\nDrafted with the help of an LLM",
                        # a separator in the message must not hide the credit line from the scan
                        "Fix\x1eCo-Authored-By: Claude\x1fa <a@b>\x1fa <a@b>\x1fplain"):
            with self.subTest(message=message):
                git(self.ws, "commit", "-q", "--allow-empty", "-m", message)
                code, out = self.call(action="push", branch="main")
                self.assertEqual(code, 1, out)
                self.assertIn("credit", out)
                self.assertIsNone(self.on_hub())
                git(self.ws, "reset", "-q", "--hard", "HEAD~1")
        self.commit("NOTES.md", "Generated with Claude Code\n", "Notes")
        self.assertEqual(self.call(action="push", branch="main")[0], 1)
        git(self.ws, "reset", "-q", "--hard", "HEAD~1")
        subprocess.run(["git", "-c", "user.name=Claude", "-c", "user.email=noreply@anthropic.com",
                        "commit", "-q", "--allow-empty", "-m", "x"], cwd=self.ws, check=True,
                       env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
        code, out = self.call(action="push", branch="main")
        self.assertEqual(code, 1)
        self.assertIn("identity", out)
        self.assertIsNone(self.on_hub())
        git(self.ws, "reset", "-q", "--hard", "HEAD~1")
        subprocess.run(["git", "-c", "user.name=AI", "-c", "user.email=x@example.com",
                        "commit", "-q", "--allow-empty", "-m", "x"], cwd=self.ws, check=True,
                       env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")})
        self.assertEqual(self.call(action="push", branch="main")[0], 1)

    def test_plain_mentions_are_not_credits(self):
        self.commit("README.md", "tandem stands in for Claude when credits run out.\n",
                    "Describe tandem, which runs on OpenAI models")
        code, out = self.call(action="push", branch="main")
        self.assertEqual(code, 0, out)

    def test_only_the_owners_repositories(self):
        for url in ("git@github.com:smartledger/demo.git", "https://gitlab.com/octocat/demo",
                    "/tmp/elsewhere.git", "git@github.com:octocat-x/demo.git"):
            with self.subTest(url=url):
                git(self.ws, "remote", "set-url", "origin", url)
                code, out = self.call(action="push", branch="main")
                self.assertEqual(code, 1, out)
        self.assertIsNone(self.on_hub())

    def test_no_account_set_is_refused(self):
        with mock.patch.object(gt, "account", REAL_ACCOUNT), \
                mock.patch.dict(os.environ, {"TANDEM_GITHUB_ACCOUNT": ""}), \
                mock.patch.object(gt, "ACCOUNT_FILE", "/nonexistent/github_account"):
            code, out = self.call(action="push", branch="main")
        self.assertEqual(code, 1)
        self.assertIn("no GitHub account is set", out)
        self.assertIsNone(self.on_hub())

    def test_only_the_account(self):
        for logins in (("someone", "octocat"), ("octocat", "someone"), (None, None)):
            with self.subTest(logins=logins), mock.patch.object(gt, "logins", lambda: logins):
                code, out = self.call(action="push", branch="main")
                self.assertEqual(code, 1)
                self.assertIn("must be octocat", out)
        self.assertIsNone(self.on_hub())

    def test_the_workspace_config_runs_nothing(self):
        script = os.path.join(self.tmp.name, "evil.sh")
        with open(script, "w") as f:
            f.write(f"#!/bin/sh\ntouch {self.marker}\nexit 0\n")
        os.chmod(script, 0o755)
        hooks = os.path.join(self.ws, ".git", "hooks")
        for hook in ("pre-push", "reference-transaction", "post-checkout"):
            with open(os.path.join(hooks, hook), "w") as f:
                f.write(f"#!/bin/sh\ntouch {self.marker}\n")
            os.chmod(os.path.join(hooks, hook), 0o755)
        for key in ("core.sshCommand", "core.fsmonitor", "uploadpack.packObjectsHook",
                    "core.alternateRefsCommand", "remote.origin.uploadpack",
                    "remote.origin.receivepack", "credential.helper", "core.pager"):
            git(self.ws, "config", key, script)
        git(self.ws, "config", "url./tmp/elsewhere.git.insteadOf", "git@github.com:octocat/demo.git")
        # an alternate, so that core.alternateRefsCommand has something to be asked about
        other = os.path.join(self.tmp.name, "other")
        git(self.tmp.name, "init", "-q", other)
        self.commit("o.txt", "o\n", "Other", ws=other)
        with open(os.path.join(self.ws, ".git", "objects", "info", "alternates"), "w") as f:
            f.write(os.path.join(other, ".git", "objects") + "\n")
        code, out = self.call(action="push", branch="main")
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(self.marker))
        self.assertIsNotNone(self.on_hub())

    def test_a_worktree_workspace(self):
        self.call(action="push", branch="main")
        wt = os.path.join(self.tmp.name, "wt")
        git(self.ws, "worktree", "add", "-q", "-b", "side", wt)
        self.commit("b.txt", "b\n", "Side work", ws=wt)
        code, out = self.call(ws=wt, action="push", branch="side")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.on_hub("side"), git(wt, "rev-parse", "HEAD"))

    def test_bad_names(self):
        for branch in ("-x", "a..b", "nope", "", "a b"):
            with self.subTest(branch=branch):
                self.assertEqual(self.call(action="push", branch=branch)[0], 1)
        for remote in ("../x", "..", ".hidden", "-x"):
            self.assertEqual(self.call(action="push", branch="main", remote=remote)[0], 1)
        self.assertEqual(self.call(action="delete_repo")[0], 1)

    def test_pull_requests(self):
        self.call(action="push", branch="main")
        code, out = self.call(action="pull_request", branch="main", base="main", title="T",
                              body="Done.\n\n\U0001f916 Generated with Claude Code")
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.gh_log))
        code, out = self.call(action="pull_request", branch="main", base="main", title="T", body="Done.")
        self.assertEqual(code, 0, out)
        self.assertIn("pull/7", out)
        with open(self.gh_log) as f:
            sent = f.read().splitlines()
        self.assertEqual(sent[sent.index("--repo") + 1], "octocat/demo")
        code, out = self.call(action="list_pull_requests")
        self.assertIn("#7 [OPEN]", out)

    def test_installs_as_an_added_tool(self):
        import added_tools
        with open(os.path.join(HERE, "tools", "github.json")) as f:
            d = json.load(f)
        with open(os.path.join(HERE, "tools", d.pop("code_file"))) as f:
            d["code"] = f.read()
        d = added_tools.make(**d)
        self.assertFalse(d["sandboxed"])
        self.assertTrue(d["ask_each_call"])
        self.assertNotIn("evaluates", added_tools.summary(d))


if __name__ == "__main__":
    unittest.main()

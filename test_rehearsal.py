"""Read-only checks for rehearsal source inspection."""
import os
import sys
import subprocess
import tempfile
import unittest
from unittest import mock

import rehearsal


class Inspection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TANDEM_SCRATCH_BASE"))
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.git("init", "-q")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.org",
                 "commit", "-q", "--allow-empty", "-m", "start")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.PIPE)

    def committed(self, path, content=b"hello"):
        target = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(content)
        self.git("add", "-f", "--", path)   # -f: not subject to this machine's global ignore rules
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.org",
                 "commit", "-qm", "add")

    def test_committed_tree_excludes_dirty_untracked_and_ignored(self):
        self.committed("a.txt")
        first = rehearsal.inspect(self.repo)
        with open(os.path.join(self.repo, "a.txt"), "wb") as f:
            f.write(b"changed")
        with open(os.path.join(self.repo, "untracked"), "wb") as f:
            f.write(b"not in tree")
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "w") as f:
            f.write("ignored\n")
        with open(os.path.join(self.repo, "ignored"), "wb") as f:
            f.write(b"excluded")
        second = rehearsal.inspect(self.repo)
        self.assertEqual(first["tree"], second["tree"])
        self.assertEqual(second["files"], 1)
        self.assertTrue(second["uncommitted_excluded"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "untracked", ".git")))

    def test_nested_files_are_inspected_and_path_with_trailing_space(self):
        # -rz includes both recursion (-r) and NUL termination (-z).
        self.committed("src/regular.txt")
        self.assertEqual(rehearsal.inspect(self.repo)["files"], 1)
        self.committed("nested/.env", b"dummy")
        with self.assertRaisesRegex(rehearsal.InspectionError, "secret"):
            rehearsal.inspect(self.repo)
        other = os.path.join(self.repo, "ends in space ")
        os.makedirs(other)
        subprocess.check_call(["git", "init", "-q", other])
        subprocess.check_call(["git", "-C", other, "-c", "user.name=Test",
                               "-c", "user.email=test@example.org", "commit", "-q",
                               "--allow-empty", "-m", "start"])
        self.assertEqual(rehearsal.inspect(other)["root"], other)

    def test_cli_reports_inspection_without_creating_trial(self):
        self.committed("src/regular.txt")
        result = subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "cg.py"),
                                 "rehearse", "inspect", "--cwd", self.repo],
                                capture_output=True, text=True, check=True)
        self.assertIn(rehearsal.inspect(self.repo)["tree"], result.stdout)
        self.assertIn("no trial repository was created", result.stdout)
        self.assertEqual(set(os.listdir(self.repo)), {".git", "src"})

    def test_reject_known_secret_and_symlink(self):
        self.committed(".env", b"dummy")
        with self.assertRaisesRegex(rehearsal.InspectionError, "secret"):
            rehearsal.inspect(self.repo)
        self.git("rm", "-q", ".env")
        os.symlink(self.repo, os.path.join(self.repo, "link"))
        self.git("add", "link")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.org",
                 "commit", "-qm", "link")
        with self.assertRaisesRegex(rehearsal.InspectionError, "symlink"):
            rehearsal.inspect(self.repo)

    def test_historical_secret_not_copied_or_checked(self):
        self.committed(".env", b"dummy")
        self.git("rm", "-q", ".env")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.org",
                 "commit", "-qm", "remove")
        self.assertEqual(rehearsal.inspect(self.repo)["files"], 0)
        self.committed("innocent.txt", b"looks like a token, but path-only inspection cannot tell")
        self.assertEqual(rehearsal.inspect(self.repo)["files"], 1)

    def test_submodule_entry_and_invalid_repository(self):
        oid = self.git("rev-parse", "HEAD").strip().decode()
        self.git("update-index", "--add", "--cacheinfo", "160000," + oid + ",module")
        tree = self.git("write-tree").strip().decode()
        commit = self.git("-c", "user.name=Test", "-c", "user.email=test@example.org",
                          "commit-tree", tree, "-m", "submodule").strip().decode()
        self.git("update-ref", "HEAD", commit)
        with self.assertRaisesRegex(rehearsal.InspectionError, "submodule"):
            rehearsal.inspect(self.repo)
        with self.assertRaises(rehearsal.InspectionError):
            rehearsal.inspect(os.path.join(self.repo, "nonexistent"))

    def test_rejects_git_metadata_name_in_tree(self):
        with mock.patch.object(rehearsal, "git", side_effect=[b"/repo\n", b"a" * 40 + b"\n",
                                                     b"b" * 40 + b"\n",
                                                     b"100644 blob " + b"c" * 40 + b"\t.git/config\0"]):
            with self.assertRaisesRegex(rehearsal.InspectionError, "metadata"):
                rehearsal.inspect(self.repo)


if __name__ == "__main__":
    unittest.main()

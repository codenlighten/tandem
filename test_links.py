#!/usr/bin/env python3
"""A link planted in a repository must not widen what the harness reads or the sandbox allows.

A workspace is data an agent (or a cloned project) may have written. Links named in the
config (node_modules, a venv), skill folders and CLAUDE.md files are followed so projects work,
but not to /, the home directory, a parent of it, a secret, or out of the project.
"""
import importlib.util
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
spec = importlib.util.spec_from_file_location("cg", os.path.join(HERE, "cg.py"))
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)


def write(path, text="x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class Links(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.t = os.path.realpath(t.name)
        self.ws = os.path.join(self.t, "ws")
        self.deps = os.path.join(self.t, "shared-deps")
        os.makedirs(self.ws)
        os.makedirs(self.deps)

    def link(self, name, target):
        os.symlink(target, os.path.join(self.ws, name))

    def test_broad_roots(self):
        for path in ("/", "/home", "/usr", "/etc", cg.HOME, os.path.dirname(cg.HOME)):
            with self.subTest(path=path):
                self.assertTrue(cg.broad_root(path))
        self.assertFalse(cg.broad_root(self.deps))

    def test_ordinary_dependency_link_is_followed(self):
        self.link("node_modules", self.deps)
        self.assertEqual(cg.automatic_read_roots(self.ws, ["node_modules"]), [self.deps])

    def test_links_to_broad_or_secret_places_are_not(self):
        os.makedirs(os.path.join(self.t, ".ssh"))
        cases = {"node_modules": "/", ".venv": cg.HOME, "venv": os.path.dirname(cg.HOME),
                 "env": os.path.join(self.t, ".ssh")}
        for name, target in cases.items():
            self.link(name, target)
        self.assertEqual(cg.automatic_read_roots(self.ws, list(cases)), [])

    def test_link_inside_the_workspace_adds_nothing(self):
        os.makedirs(os.path.join(self.ws, "real"))
        self.link("node_modules", os.path.join(self.ws, "real"))
        self.assertEqual(cg.automatic_read_roots(self.ws, ["node_modules"]), [])

    def test_project_skill_cannot_link_out(self):
        good = os.path.join(self.ws, ".claude", "skills", "good", "SKILL.md")
        write(good, "---\nname: good\ndescription: ok\n---\n")
        outside = os.path.join(self.t, "outside-skill")
        write(os.path.join(outside, "SKILL.md"), "---\nname: evil\ndescription: x\n---\n")
        os.symlink(outside, os.path.join(self.ws, ".claude", "skills", "evil"))
        write(os.path.join(self.t, "secret.txt"), "key\n")
        os.makedirs(os.path.join(self.ws, ".claude", "skills", "file"))
        os.symlink(os.path.join(self.t, "secret.txt"), os.path.join(self.ws, ".claude", "skills", "file", "SKILL.md"))
        names = set(cg.find_skills(self.ws))
        self.assertIn("good", names)
        self.assertNotIn("evil", names)
        self.assertNotIn("file", names)

    def test_project_claude_md_cannot_link_out(self):
        outside = os.path.join(self.t, "private.txt")
        write(outside, "secret instructions\n")
        os.symlink(outside, os.path.join(self.ws, "CLAUDE.md"))
        found = [p for p in cg.project_instruction_files(self.ws) if p.startswith(self.ws)]
        self.assertEqual(found, [])
        os.unlink(os.path.join(self.ws, "CLAUDE.md"))
        write(os.path.join(self.ws, "CLAUDE.md"), "real\n")
        found = [p for p in cg.project_instruction_files(self.ws) if p.startswith(self.ws)]
        self.assertEqual(found, [os.path.join(self.ws, "CLAUDE.md")])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Controls for sandbox.py: each thing the sandbox must stop is attempted in a
harmless form against targets this test creates itself, and must fail; ordinary
work inside the workspace must still succeed. Nothing outside the test's own
temporary directory is touched, and no real service is contacted.

  python3 test_sandbox.py
"""
import os
import socket
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM = ["/usr", "/bin", "/lib", "/lib64", "/etc", "/proc", "/dev"]


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="cgt.", dir="/tmp")
        self.ws = os.path.join(self.tmp.name, "ws")
        self.outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(self.ws)
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "private.txt"), "w") as f:
            f.write("PRIVATE")

    def tearDown(self):
        self.tmp.cleanup()

    def run_in(self, command, extra=()):
        argv = [sys.executable, os.path.join(HERE, "sandbox.py")]
        for p in SYSTEM:
            argv += ["--ro", p]
        argv += ["--rw", "/dev/null", "--rw", self.ws, *extra, "--", "bash", "-c", command]
        r = subprocess.run(argv, cwd=self.ws, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def py(self, code, extra=()):
        return self.run_in(f"python3 -c {subprocess.list2cmdline([code])}", extra)

    def test_work_inside(self):
        rc, out = self.run_in("echo hi > a.txt && cat a.txt && mkdir d && mv a.txt d/ && "
                              "rm -r d && echo x | tr x y && python3 -c 'print(6*7)'")
        self.assertEqual(rc, 0, out)
        self.assertIn("42", out)

    def test_write_outside(self):
        for cmd in (f"touch {self.outside}/new", f"echo x >> {self.outside}/private.txt",
                    f"rm {self.outside}/private.txt", f"mv {self.outside}/private.txt .",
                    f"ln -s {self.outside} l && touch l/new", "touch ../outside/new",
                    f"mkdir {self.outside}/d"):
            with self.subTest(cmd=cmd):
                rc, out = self.run_in(cmd)
                self.assertNotEqual(rc, 0, out)
        self.assertEqual(sorted(os.listdir(self.outside)), ["private.txt"])
        with open(os.path.join(self.outside, "private.txt")) as f:
            self.assertEqual(f.read(), "PRIVATE")

    def test_read_outside(self):
        for cmd in (f"cat {self.outside}/private.txt", f"ls {self.outside}",
                    "cat ~/.ssh/id_rsa", "ls ~/.ssh", "ls ~",
                    f"ln -s {self.outside}/private.txt l && cat l"):
            with self.subTest(cmd=cmd):
                rc, out = self.run_in(cmd)
                self.assertNotEqual(rc, 0, out)
                self.assertNotIn("PRIVATE", out)

    def test_signal_outside(self):
        # kill -0 delivers nothing; it only asks whether signalling would be allowed.
        rc, out = self.run_in(f"kill -0 {os.getpid()}")
        self.assertNotEqual(rc, 0, out)
        rc, out = self.run_in("sleep 30 & kill $! && echo killed-own-child")
        self.assertIn("killed-own-child", out)

    def test_no_network(self):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        code = ("import socket\n"
                "for fam, typ in ((socket.AF_INET, socket.SOCK_STREAM), "
                "(socket.AF_INET, socket.SOCK_DGRAM), (socket.AF_INET6, socket.SOCK_STREAM)):\n"
                "    try:\n        socket.socket(fam, typ); print('CREATED')\n"
                "    except OSError as e: print('refused', e.errno)\n")
        rc, out = self.py(code)
        self.assertNotIn("CREATED", out)
        self.assertEqual(out.count("refused 13"), 3, out)
        # With web ports allowed, a local port that is not on the list is still closed.
        code = (f"import socket\ns = socket.socket()\n"
                f"try:\n    s.connect(('127.0.0.1', {port})); print('CONNECTED')\n"
                f"except OSError as e: print('refused', e.errno)\n")
        rc, out = self.py(code, extra=("--tcp", "443"))
        self.assertIn("refused 13", out)
        rc, out = self.py("import socket\ns = socket.socket()\ntry:\n    s.bind(('127.0.0.1', 0)); "
                          "print('BOUND')\nexcept OSError as e: print('refused', e.errno)\n",
                          extra=("--tcp", "443"))
        self.assertIn("refused 13", out)
        srv.close()

    def test_unix_socket(self):
        # Stands in for the Docker socket: a listening Unix socket of our own.
        for where in (self.outside, self.ws):
            path = os.path.join(where, "s.sock")
            srv = socket.socket(socket.AF_UNIX)
            srv.bind(path)
            srv.listen(1)
            code = (f"import socket\ntry:\n    s = socket.socket(socket.AF_UNIX); "
                    f"s.connect({path!r}); print('CONNECTED')\n"
                    f"except OSError as e: print('refused', e.errno)\n")
            with self.subTest(where=where):
                rc, out = self.py(code)
                self.assertIn("refused 13", out)
                self.assertNotIn("CONNECTED", out)
            srv.close()
        rc, out = self.py("import socket\na, b = socket.socketpair(); a.send(b'x'); "
                          "print(b.recv(1))")
        self.assertIn("b'x'", out)

    def test_datagram_socketpair(self):
        # A datagram pair could be re-aimed at a socket path, so only stream pairs exist.
        rc, out = self.py("import socket\ntry:\n    socket.socketpair(socket.AF_UNIX, "
                          "socket.SOCK_DGRAM); print('CREATED')\n"
                          "except OSError as e: print('refused', e.errno)\n")
        self.assertIn("refused 13", out)

    def test_other_socket_families(self):
        rc, out = self.py("import socket\nfor fam in (16, 17, 38, 40):\n    try:\n"
                          "        socket.socket(fam, socket.SOCK_RAW); print('CREATED')\n"
                          "    except OSError as e: print('refused', e.errno)\n")
        self.assertEqual(out.count("refused 13"), 4, out)

    def test_other_processes_are_closed(self):
        env = dict(os.environ, CG_PROBE="outsider")
        other = subprocess.Popen(["sleep", "30"], env=env)
        try:
            rc, out = self.run_in(f"cat /proc/{other.pid}/environ; ls /proc/{other.pid}/cwd/")
            self.assertNotIn("outsider", out)
            self.assertEqual(out.count("Permission denied"), 2, out)
        finally:
            other.kill()
            other.wait()

    def test_only_standard_descriptors(self):
        with open(os.path.join(self.outside, "private.txt")) as held:  # must not leak in
            os.set_inheritable(held.fileno(), True)
            rc, out = self.run_in("ls /proc/self/fd")
        self.assertEqual(out.split(), ["0", "1", "2", "3"], out)  # 3 is ls reading the dir

    def test_resource_limits(self):
        rc, out = self.run_in("head -c 3000000 /dev/zero > big; echo rc=$?; "
                              "python3 -c 'x = bytearray(2 * 2**30)'; echo rc=$?; nice",
                              extra=("--fsize-mb", "1", "--mem-gb", "1", "--nice", "19"))
        self.assertLessEqual(os.path.getsize(os.path.join(self.ws, "big")), 2 ** 20)
        self.assertIn("MemoryError", out)
        self.assertIn("\n19\n", out)

    def test_total_limits(self):
        # Four workers of 150 MB each fit a per-process cap but not a 300 MB total.
        with open(os.path.join(self.ws, "w.py"), "w") as f:
            f.write("import time\nx = bytearray(150 * 2**20)\n"
                    "for i in range(0, len(x), 4096): x[i] = 1\ntime.sleep(1.5)\nprint('DONE')\n")
        pool = "for i in 1 2 3 4; do python3 w.py & done; wait"
        rc, out = self.run_in(pool, extra=("--mem-gb", "1"))
        self.assertEqual(out.count("DONE"), 4, out)
        rc, out = self.run_in(pool, extra=("--mem-gb", "1", "--total-mem-gb", "0.3"))
        self.assertLess(out.count("DONE"), 4, out)
        rc, out = self.run_in("for i in $(seq 40); do sleep 2 & done; wait; echo ALL-STARTED",
                              extra=("--max-tasks", "20"))
        self.assertTrue("Resource temporarily unavailable" in out or "fork" in out, out)
        # The other restrictions still hold inside the cgroup.
        rc, out = self.run_in(f"touch {self.outside}/new", extra=("--max-tasks", "50"))
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "new")))

    def test_hidden_file(self):
        secret = "HIDDEN-" + "VALUE-12345"
        os.makedirs(os.path.join(self.ws, "src"))
        os.makedirs(os.path.join(self.ws, "conf", "deep"))
        for path, text in ((".env", secret), ("conf/deep/key.pem", secret), ("a.txt", "plain-a"),
                           ("src/b.txt", "plain-b"), ("conf/c.txt", "plain-c")):
            with open(os.path.join(self.ws, path), "w") as f:
                f.write(text + "\n")
        hide = ("--hide", os.path.join(self.ws, ".env"), "--hide", os.path.join(self.ws, "conf/deep/key.pem"))
        for cmd in ("cat .env", "cat .e*", "cat conf/deep/key.pem", "cat conf/deep/*",
                    "python3 -c \"print(open('.env').read())\"", "cp .env copy; cat copy",
                    "mv .env src/moved; cat src/moved", "mv conf/deep/key.pem src/k; cat src/k",
                    "ln .env hard; cat hard", "ln -s .env soft; cat soft",
                    "ln -s conf/deep d; cat d/key.pem", "tar c . | strings", "grep -r HIDDEN .",
                    "dd if=.env 2>&1", "base64 .env", "head -c 99 < .env",
                    "mv conf/deep src/deep; cat src/deep/key.pem",
                    "cp -r conf/deep src/deep2; cat src/deep2/key.pem"):
            with self.subTest(cmd=cmd):
                rc, out = self.run_in(cmd, extra=hide)
                self.assertNotIn("HIDDEN", out)
                self.assertNotIn("SElEREVO", out)  # base64 of HIDDEN
        for path in (".env", "conf/deep/key.pem"):   # and nothing moved or destroyed them
            with open(os.path.join(self.ws, path)) as f:
                self.assertEqual(f.read(), secret + "\n")
        # Everything else still works, with the documented cost at the secret's own level.
        rc, out = self.run_in("cat a.txt src/b.txt conf/c.txt && echo new > src/n.txt && cat src/n.txt "
                              "&& mkdir src/d && echo deep > src/d/x && cat src/d/x && ls -a . conf/deep "
                              "&& echo x >> .env.example && python3 -c 'print(6*7)'", extra=hide)
        self.assertEqual(rc, 0, out)
        for want in ("plain-a", "plain-b", "plain-c", "new", "deep", ".env", "key.pem", "42"):
            self.assertIn(want, out)
        rc, out = self.run_in("echo fresh > top.txt && cat top.txt", extra=hide)
        self.assertNotEqual(rc, 0, out)               # written, but not readable in this command
        rc, out = self.run_in("cat top.txt", extra=hide)
        self.assertIn("fresh", out)                   # readable in the next one
        rc, out = self.run_in("mv a.txt src/ && cat src/a.txt", extra=hide)
        self.assertIn("plain-a", out)                 # mv falls back to a copy

    def test_hidden_file_with_other_names(self):
        path = os.path.join(self.ws, ".env")
        os.makedirs(os.path.join(self.ws, "src"))
        with open(path, "w") as f:
            f.write("HIDDEN-X\n")
        os.link(path, os.path.join(self.ws, "src", "innocent.txt"))   # a name in the workspace
        rc, out = self.run_in("cat src/innocent.txt; cat src/*; echo still-ran", extra=("--hide", path))
        self.assertNotIn("HIDDEN", out)
        self.assertIn("still-ran", out)
        os.link(path, os.path.join(self.outside, "elsewhere"))        # a name we cannot account for
        rc, out = self.run_in("echo ran", extra=("--hide", path))
        self.assertEqual(rc, 126, out)
        self.assertNotIn("ran\n", out)

    def test_hidden_directory(self):
        os.makedirs(os.path.join(self.ws, ".iptrust", "v2"))
        with open(os.path.join(self.ws, ".iptrust", "v2", "opening.json"), "w") as f:
            f.write("HIDDEN-OPENING\n")
        hide = ("--hide", os.path.join(self.ws, ".iptrust"))
        for cmd in ("cat .iptrust/v2/opening.json", "cat .iptrust/*/*", "cp -r .iptrust c; cat c/v2/*",
                    "mv .iptrust moved; cat moved/v2/opening.json", "tar c .iptrust | strings"):
            with self.subTest(cmd=cmd):
                rc, out = self.run_in(cmd, extra=hide)
                self.assertNotIn("HIDDEN", out)

    def test_filter_jumps(self):
        # Every jump in the socket filter lands inside the program, for any list size.
        import sandbox
        for n in range(0, 5):
            prog = sandbox.socket_filter(list(range(n)))
            for i, (code, jt, jf, k) in enumerate(prog):
                if code in (sandbox.JEQ, sandbox.JGE):
                    self.assertLess(i + 1 + jt, len(prog))
                    self.assertLess(i + 1 + jf, len(prog))
            self.assertEqual([c for c, *_ in prog[-2:]], [sandbox.RET, sandbox.RET])

    def test_no_new_privileges(self):
        rc, out = self.run_in("grep NoNewPrivs /proc/self/status")
        self.assertIn("1", out.split(":")[-1])
        rc, out = self.run_in("sudo -n true")
        self.assertNotEqual(rc, 0, out)

    def test_children_inherit(self):
        rc, out = self.run_in(f"bash -c 'python3 -c \"open(\\\"{self.outside}/new\\\", \\\"w\\\")\"'")
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "new")))

    def test_fails_closed(self):
        r = subprocess.run([sys.executable, os.path.join(HERE, "sandbox.py"), "--tcp", "notaport",
                            "--", "touch", os.path.join(self.outside, "ran")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 126, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.outside, "ran")))


if __name__ == "__main__":
    unittest.main()

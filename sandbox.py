#!/usr/bin/env python3
"""Landlock launcher: restrict this process, then exec a command inside the restriction.

  sandbox.py [--ro PATH]... [--rw PATH]... [--tcp PORT]... [--allow-unix]
             [--mem-gb N] [--fsize-mb N] [--nice N]
             [--total-mem-gb N] [--max-tasks N] [--unit NAME]
             [--hide PATH]... -- cmd [args...]

The command and everything it starts can
- read and execute only under the --ro and --rw paths,
- write, create and delete only under the --rw paths, and use no device ioctls,
- open TCP connections only to the --tcp ports, and bind none; with no --tcp port
  it cannot create a socket of any family (so no UDP either),
- create no Unix sockets unless --allow-unix is given (this is what keeps it off
  the Docker socket, the ssh agent and D-Bus); stream socketpairs still work,
- signal or inspect only processes inside the same sandbox,
- gain no privileges (no_new_privs, so sudo and setuid binaries do nothing),
- use at most --mem-gb of address space per process and write files of at most
  --fsize-mb, at the given nice level,
- with --total-mem-gb or --max-tasks, use at most that much memory and that many
  processes and threads in total, however many workers it starts. These two put the
  command in its own cgroup through `systemd-run --user --scope`, so they need a
  working systemd user session; --unit names the scope so the caller can stop it.

--hide PATH makes one file or directory inside an allowed path unreadable. Landlock
can only allow, not deny, so this works by allowing everything beside the hidden path
by name, which has a cost: in each directory on the way down to a hidden path, a file
or directory created by the command cannot be read back by that same command (it can
still be written, executed and listed), and moving a file from such a directory into
another fails as a rename, so `mv` falls back to copying. Other hard links to a hidden
file are found and hidden too if they are under the --rw paths; if one is anywhere
else, the command is not run.

Landlock and seccomp are kernel features that need no root and no user namespaces,
and the process cannot lift the restriction once applied. If the kernel cannot
enforce every part of this, the launcher exits with status 126 instead of running
the command unprotected.

Not covered: UDP to any host when a --tcp port is allowed (DNS needs it), reading
any file under an allowed path, other processes' command lines in /proc, and CPU
time or total disk use (the caller's timeout bounds those).
Standard library only; x86_64 Linux with Landlock ABI 6+.
"""
import ctypes
import os
import resource
import sys

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
RULE_PATH_BENEATH, RULE_NET_PORT = 1, 2
PR_SET_NO_NEW_PRIVS = 38

FS_EXECUTE, FS_WRITE_FILE, FS_READ_FILE, FS_READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
FS_REFER, FS_TRUNCATE, FS_IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
FS_ALL_V1 = (1 << 13) - 1
FS_READ = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR
# Rights that make sense on a single file; the kernel rejects the others there.
FS_FILE_ONLY = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
SCOPE_ABSTRACT_UNIX, SCOPE_SIGNAL = 1 << 0, 1 << 1

MIN_ABI = 6  # signal scoping arrived in ABI 6, TCP rules in 4

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class NetPortAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


def _check(ret, what):
    if ret < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"{what}: {os.strerror(err)}")
    return ret


def abi_version():
    """Landlock ABI version of the running kernel, or 0 if Landlock is unavailable."""
    v = _libc.syscall(SYS_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
    return max(v, 0)


def restrict(ro, rw, tcp_ports=()):
    """Apply the restriction to the current process. Raises OSError if it can't."""
    abi = abi_version()
    if abi < MIN_ABI:
        raise OSError(f"Landlock ABI {abi} is older than the required {MIN_ABI}")
    handled_fs = FS_ALL_V1 | FS_REFER | FS_TRUNCATE | FS_IOCTL_DEV
    attr = RulesetAttr(handled_fs, NET_BIND_TCP | NET_CONNECT_TCP,
                       SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL)
    ruleset = _check(_libc.syscall(SYS_CREATE_RULESET, ctypes.byref(attr),
                                   ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0)),
                     "landlock_create_ruleset")
    # FS_IOCTL_DEV is handled but granted nowhere: no device ioctls at all.
    for paths, access in ((ro, FS_READ), (rw, handled_fs & ~FS_IOCTL_DEV)):
        for p in paths:
            try:
                fd = os.open(p, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # a path that doesn't exist grants nothing
            try:
                allowed = access if os.path.isdir(p) else access & FS_FILE_ONLY
                rule = PathBeneathAttr(allowed, fd)
                _check(_libc.syscall(SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH,
                                     ctypes.byref(rule), ctypes.c_uint32(0)),
                       f"landlock_add_rule {p}")
            finally:
                os.close(fd)
    for port in tcp_ports:
        rule = NetPortAttr(NET_CONNECT_TCP, int(port))
        _check(_libc.syscall(SYS_ADD_RULE, ruleset, RULE_NET_PORT,
                             ctypes.byref(rule), ctypes.c_uint32(0)),
               f"landlock_add_rule tcp {port}")
    _check(_libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prctl(no_new_privs)")
    _check(_libc.syscall(SYS_RESTRICT_SELF, ruleset, ctypes.c_uint32(0)),
           "landlock_restrict_self")
    os.close(ruleset)


def hide(paths, roots, writable=()):
    """Add a second Landlock layer that allows reading files everywhere the first one
    does except under the hidden paths. Layers intersect, so a read has to pass both."""
    hidden, real, links = set(), [], {}
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue  # nothing there to hide
        hidden.add((st.st_dev, st.st_ino))
        real.append(os.path.realpath(p))
        if not os.path.isdir(p) and st.st_nlink > 1:
            links[(st.st_dev, st.st_ino)] = st.st_nlink
    if not hidden:
        return
    if links:
        # A hidden file with several names: every name must be found and hidden too. A
        # command can add one beside the original, so look through the writable paths.
        found = {key: set() for key in links}
        for root in writable:
            for base, dirs, names in os.walk(root):
                for n in names:
                    path = os.path.join(base, n)
                    try:
                        st = os.lstat(path)
                    except OSError:
                        continue
                    if (st.st_dev, st.st_ino) in found:
                        found[(st.st_dev, st.st_ino)].add(path)
        for key, count in links.items():
            if len(found[key]) != count:
                raise OSError("cannot hide a file that has a hard link outside the writable paths")
            real.extend(found[key])
    attr = RulesetAttr(FS_READ_FILE | FS_REFER, 0, 0)
    ruleset = _check(_libc.syscall(SYS_CREATE_RULESET, ctypes.byref(attr),
                                   ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0)),
                     "landlock_create_ruleset")

    def allow(path, access):
        try:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            return
        try:
            rule = PathBeneathAttr(access, fd)
            _check(_libc.syscall(SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH,
                                 ctypes.byref(rule), ctypes.c_uint32(0)),
                   f"landlock_add_rule {path}")
        finally:
            os.close(fd)

    def grant(path):
        try:
            st = os.stat(path)
        except OSError:
            return
        if (st.st_dev, st.st_ino) in hidden:
            return
        is_dir = os.path.isdir(path)
        inside = os.path.realpath(path).rstrip("/") + "/"
        if is_dir and any(r.startswith(inside) for r in real):
            # A hidden path lies below: no blanket read here, name each entry instead.
            allow(path, FS_REFER)
            for name in os.listdir(path):
                grant(os.path.join(path, name))
        else:
            allow(path, FS_READ_FILE | FS_REFER if is_dir else FS_READ_FILE)

    for root in roots:
        grant(root)
    _check(_libc.syscall(SYS_RESTRICT_SELF, ruleset, ctypes.c_uint32(0)),
           "landlock_restrict_self (hide)")
    os.close(ruleset)


# ---------------------------------------------------------------- seccomp

PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 22, 2
AUDIT_ARCH_X86_64 = 0xC000003E
NR_SOCKET, NR_SOCKETPAIR, NR_IO_URING_SETUP, X32_SYSCALL_BIT = 41, 53, 425, 0x40000000
AF_UNIX, AF_INET, AF_INET6, AF_NETLINK = 1, 2, 10, 16
SOCK_STREAM, SOCK_TYPE_MASK = 1, 0xF
RET_ALLOW, RET_KILL, RET_EACCES = 0x7FFF0000, 0x80000000, 0x00050000 | 13
LD_ABS, JEQ, JGE, AND, RET = 0x20, 0x15, 0x35, 0x54, 0x06


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8),
                ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_uint16), ("filter", ctypes.POINTER(SockFilter))]


def socket_filter(allowed_domains):
    """The seccomp program: socket() succeeds only for the allowed address families.

    Landlock has no rule for connecting to a Unix socket by path (the Docker socket,
    the ssh agent, D-Bus) or for UDP, so those are closed by refusing to create the
    socket. socketpair() is limited to stream pairs, because a datagram pair can be
    re-aimed at a socket path. io_uring is refused because it can create sockets
    without the socket() call. Refused calls fail with EACCES."""
    n = len(allowed_domains)
    # Jump offsets are relative to the next instruction.
    pair, allow, deny = 10 + n, 13 + n, 14 + n
    prog = [
        (LD_ABS, 0, 0, 4),                                  # 0: architecture
        (JEQ, 1, 0, AUDIT_ARCH_X86_64),                     # 1
        (RET, 0, 0, RET_KILL),                              # 2
        (LD_ABS, 0, 0, 0),                                  # 3: syscall number
        (JGE, deny - 5, 0, X32_SYSCALL_BIT),                # 4
        (JEQ, deny - 6, 0, NR_IO_URING_SETUP),              # 5
        (JEQ, pair - 7, 0, NR_SOCKETPAIR),                  # 6
        (JEQ, 0, allow - 8, NR_SOCKET),                     # 7
        (LD_ABS, 0, 0, 16),                                 # 8: socket() domain
    ]
    for i, d in enumerate(allowed_domains):                 # 9 .. 8+n
        prog.append((JEQ, allow - (10 + i), 0, d))
    prog += [
        (RET, 0, 0, RET_EACCES),                            # 9+n: domain not allowed
        (LD_ABS, 0, 0, 24),                                 # 10+n: socketpair() type
        (AND, 0, 0, SOCK_TYPE_MASK),                        # 11+n
        (JEQ, 0, 1, SOCK_STREAM),                           # 12+n
        (RET, 0, 0, RET_ALLOW),                             # 13+n
        (RET, 0, 0, RET_EACCES),                            # 14+n
    ]
    return prog


def restrict_sockets(allowed_domains):
    if os.uname().machine != "x86_64":
        raise OSError("the socket filter is written for x86_64 only")
    prog = socket_filter(allowed_domains)
    arr = (SockFilter * len(prog))(*[SockFilter(*p) for p in prog])
    fprog = SockFprog(len(prog), arr)
    _check(_libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0),
           "prctl(seccomp)")


def limit_resources(mem_gb, fsize_mb, nice):
    if mem_gb:
        resource.setrlimit(resource.RLIMIT_AS, (int(mem_gb * 2 ** 30),) * 2)
    if fsize_mb:
        resource.setrlimit(resource.RLIMIT_FSIZE, (int(fsize_mb * 2 ** 20),) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if nice:
        os.nice(max(0, int(nice) - os.nice(0)))


def main(argv):
    ro, rw, tcp, hidden = [], [], [], []
    limits = {"--mem-gb": 0.0, "--fsize-mb": 0.0, "--nice": 0.0,
              "--total-mem-gb": 0.0, "--max-tasks": 0.0}
    allow_unix, unit, inner = False, None, []
    i = 0
    while i < len(argv) and argv[i] != "--":
        if argv[i] not in ("--total-mem-gb", "--max-tasks", "--unit"):
            inner += argv[i:i + (1 if argv[i] == "--allow-unix" else 2)]
        if argv[i] == "--unit" and i + 1 < len(argv):
            unit = argv[i + 1]
            i += 2
            continue
        if argv[i] in ("--ro", "--rw", "--tcp", "--hide") and i + 1 < len(argv):
            {"--ro": ro, "--rw": rw, "--tcp": tcp, "--hide": hidden}[argv[i]].append(argv[i + 1])
            i += 2
        elif argv[i] in limits and i + 1 < len(argv):
            try:
                limits[argv[i]] = float(argv[i + 1])
            except ValueError:
                sys.exit(__doc__)
            i += 2
        elif argv[i] == "--allow-unix":
            allow_unix = True
            i += 1
        else:
            sys.exit(__doc__)
    cmd = argv[i + 1:]
    if not cmd:
        sys.exit(__doc__)
    if limits["--total-mem-gb"] or limits["--max-tasks"]:
        # Start again inside a cgroup of our own, without the cgroup flags.
        run = ["systemd-run", "--user", "--scope", "--quiet", "--collect"]
        if unit:
            run += ["--unit", unit]
        if limits["--total-mem-gb"]:
            run += ["-p", f"MemoryMax={int(limits['--total-mem-gb'] * 2 ** 30)}",
                    "-p", "MemorySwapMax=0"]
        if limits["--max-tasks"]:
            run += ["-p", f"TasksMax={int(limits['--max-tasks'])}"]
        run += ["--", sys.executable, os.path.abspath(__file__)] + inner + ["--"] + cmd
        try:
            os.execvp(run[0], run)
        except OSError as e:
            print(f"sandbox: not running the command, no cgroup: {e}", file=sys.stderr)
            sys.exit(126)
    try:
        limit_resources(limits["--mem-gb"], limits["--fsize-mb"], limits["--nice"])
        restrict(ro, rw, tcp)
        hide(hidden, ro + rw, rw)
        # No TCP port allowed means no network at all, so UDP is closed too. Netlink
        # goes with the network: the resolver uses it to list local addresses.
        restrict_sockets(([AF_UNIX] if allow_unix else [])
                         + ([AF_INET, AF_INET6, AF_NETLINK] if tcp else []))
    except (OSError, ValueError) as e:
        print(f"sandbox: not running the command, the restriction failed: {e}", file=sys.stderr)
        sys.exit(126)
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        print(f"sandbox: cannot run {cmd[0]}: {e}", file=sys.stderr)
        sys.exit(127)


if __name__ == "__main__":
    main(sys.argv[1:])

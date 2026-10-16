"""Rules the tool layer applies before anything the model asks for is carried out.

Two checks:
- check_path(path, write, roots): the file tools (read, write, edit, glob, grep).
- check_command(command): the bash tool. Returns (verdict, reason) where the verdict
  is "allow", "readonly" (allow, and safe to run without asking), "ask" (needs a
  person's yes even in auto mode) or "deny".

The command rules read the command text, so they are a first filter, not a
guarantee: a command can be written so that text rules miss it. The guarantee for
bash is the kernel sandbox in sandbox.py. The rules here exist to refuse the known
bad commands early, with a reason the model can act on, and to cover what the
sandbox cannot (reading a secret file inside the workspace, outward-facing actions).

The hard rules come from the fleet coordinator's list of commands that have caused
damage on these machines (2026-10-08): pkill -f, rm -rf on shared directories, and
anything that touches Docker or CapRover.
"""
import ctypes
import fnmatch
import os
import re
import shlex
import struct

# ---------------------------------------------------------------- secrets

SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".kube", ".docker", ".iptrust", ".password-store"}
SECRET_NAMES = [".env", ".env.*", "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "*.pem",
                "*.key", "*.p12", "*.pfx", "*.keystore", ".netrc", ".npmrc", ".pypirc",
                "opening.json", "credentials", "credentials.*", "*.kdbx", "wallet.dat",
                "secrets.*", "*.secret"]
NOT_SECRET = [".env.example", ".env.sample", ".env.template", "*.pub"]


def is_secret(path):
    """True if the path names something that must not be shown to the model."""
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p in SECRET_DIRS for p in parts):
        return True
    name = parts[-1].lower() if parts else ""
    if any(fnmatch.fnmatch(name, pat) for pat in NOT_SECRET):
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in SECRET_NAMES)


# ---------------------------------------------------------------- paths

def inside(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def check_path(path, write, read_roots, write_roots):
    """Resolve a path for a file tool. Returns (real_path, None) or (None, reason).

    Symlinks are resolved first, so a link inside the workspace that points out of
    it is judged by where it leads."""
    real = os.path.realpath(os.path.expanduser(path))
    if is_secret(real) or is_secret(path):
        return None, f"refused: {path} looks like a secret (keys, .env, credentials, .iptrust)"
    roots = write_roots if write else list(read_roots) + list(write_roots)
    if not any(inside(real, r) for r in roots):
        what = "write outside" if write else "read outside"
        return None, (f"refused: {path} is {what} the allowed directories "
                      f"({', '.join(write_roots if write else roots)})")
    return real, None


SYS_OPENAT2, RESOLVE_NO_MAGICLINKS, RESOLVE_BENEATH = 437, 0x02, 0x08
_libc = ctypes.CDLL(None, use_errno=True)


def open_beneath(root, real, flags, mode=0o644):
    """Open `real` (a path check_path approved) so that it cannot leave `root`.

    check_path looks at the path and the open happens a moment later; if something
    swapped a directory for a symlink in between, a plain open would follow it out of
    the workspace. openat2 with RESOLVE_BENEATH makes the kernel refuse instead.
    A file that has other hard links is refused for writing, since writing it would
    change a file whose other name may be anywhere."""
    if real == root:  # the root is itself a file
        fd = os.open(real, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    else:
        dirfd = os.open(root, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            how = struct.pack("QQQ", flags | os.O_CLOEXEC, mode if flags & os.O_CREAT else 0,
                              RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS)
            fd = _libc.syscall(SYS_OPENAT2, dirfd, os.fsencode(os.path.relpath(real, root)),
                               how, ctypes.c_size_t(len(how)))
            if fd < 0:
                err = ctypes.get_errno()
                raise OSError(err, os.strerror(err), real)
        finally:
            os.close(dirfd)
    if flags & (os.O_WRONLY | os.O_RDWR) and os.fstat(fd).st_nlink > 1:
        os.close(fd)
        raise OSError(f"refused: {real} has other hard links")
    return fd


def root_of(real, roots):
    """The allowed root that contains the path (the longest, if several do)."""
    return max((r for r in roots if inside(real, r)), key=len)


# ---------------------------------------------------------------- commands

# Never run, in any mode. Value is the reason shown to the model.
DENY_COMMANDS = {
    "pkill": "pkill matches its own command line and kills the wrong process; "
             "find the PID with `pgrep -af` and use `kill <pid>`",
    "killall": "kill by explicit PID instead",
    "docker": "Docker is off limits (live services run on these hosts)",
    "docker-compose": "Docker is off limits",
    "podman": "containers are off limits",
    "caprover": "CapRover is off limits",
    "kubectl": "cluster control is off limits",
    "sudo": "no privileged commands", "su": "no privileged commands",
    "doas": "no privileged commands", "pkexec": "no privileged commands",
    "systemctl": "service control is off limits", "service": "service control is off limits",
    "shutdown": "refused", "reboot": "refused", "halt": "refused", "poweroff": "refused",
    "mkfs": "refused", "fdisk": "refused", "parted": "refused", "mount": "refused",
    "umount": "refused", "dd": "refused: use cp or head/tail for file work",
    "ssh": "remote hosts are off limits to this agent", "scp": "remote hosts are off limits",
    "sftp": "remote hosts are off limits", "mosh": "remote hosts are off limits",
    "dev-bg": "remote hosts are off limits", "fleet": "the fleet tool is the coordinator's",
    "tailscale": "refused", "crontab": "refused", "at": "refused",
    "chown": "refused", "nsenter": "refused", "unshare": "refused",
}

# Run only after a person says yes, in every mode: they publish or cannot be undone.
ASK_COMMANDS = {
    "iptrust2": "IP registration is anchored on public chains and cannot be undone",
    "iptrust": "IP registration cannot be undone",
    "gh": "acts on GitHub", "npm publish": "publishes a package",
    "twine": "publishes a package", "cargo publish": "publishes a package",
    "git push": "publishes commits", "git reset --hard": "discards uncommitted work",
    "git clean": "deletes untracked files", "git checkout .": "discards uncommitted work",
    "git restore": "discards uncommitted work", "git stash": "moves uncommitted work away",
    "git rebase": "rewrites history", "git filter-branch": "rewrites history",
    "git branch -D": "deletes a branch", "git tag -d": "deletes a tag",
    "git checkout --": "discards uncommitted changes to those files",
    "git commit --amend": "rewrites the last commit",
    "git switch --discard-changes": "discards uncommitted work",
    "git worktree remove": "deletes a working tree",
    "rsync": "can overwrite or delete at the destination",
}

# Words that run another command; the command they run is checked in their place.
WRAPPERS = {"env", "nice", "nohup", "time", "timeout", "exec", "command", "builtin", "xargs",
            "setsid", "stdbuf", "ionice", "chronic", "watch", "strace", "ltrace", "then", "do",
            "else", "elif", "if", "while", "until", "!", "{", "("}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}

READONLY = {"ls", "cat", "head", "tail", "wc", "grep", "rg", "egrep", "fgrep", "pwd", "echo",
            "printf", "which", "type", "file", "stat", "du", "df", "date", "whoami", "id",
            "uname", "hostname", "uptime", "free", "nproc", "tree", "sort", "uniq", "cut",
            "diff", "cmp", "sha256sum", "md5sum", "basename", "dirname", "realpath",
            "readlink", "true", "test", "[", "pgrep", "ps", "column", "tr", "jq", "nl", "cd"}
READONLY_GIT = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "ls-files",
                "describe", "blame", "shortlog", "tag", "config"}

OPERATORS = {";", "&", "&&", "||", "|", "|&", "(", ")", "{", "}", "\n", ";;"}
SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")


def _split(command):
    """Tokens of a shell command with operators as their own tokens, or None."""
    lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def _segments(tokens):
    seg = []
    for t in tokens:
        if t in OPERATORS or (t and set(t) <= set(";&|()")):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg


def _strip_wrappers(seg):
    """Drop VAR=value prefixes and wrapper words to reach the command that runs."""
    i = 0
    while i < len(seg):
        w = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w):
            i += 1
        elif os.path.basename(w) in WRAPPERS:
            i += 1
            # skip the wrapper's own options and numeric arguments (nice -n 19, timeout 5)
            while i < len(seg) and (seg[i].startswith("-") or re.match(r"^[0-9.]+[smhd]?$", seg[i])):
                i += 1
        else:
            break
    return seg[i:]


def _rm_problem(args):
    flags = "".join(a.lstrip("-") for a in args if a.startswith("-") and not a.startswith("--"))
    recursive = "r" in flags.lower() or "--recursive" in args
    targets = [a for a in args if not a.startswith("-")]
    for t in targets:
        if t in ("/", "~", "~/", ".", "..", "*", "/*", "~/*") or t.startswith("$HOME") \
                or t.rstrip("/") in ("$HOME", "${HOME}"):
            return f"refused: `rm` of {t!r}"
        if recursive and (t.startswith(("/", "~", "..")) or "/.." in t or "$" in t):
            return (f"refused: recursive delete of {t!r} outside the workspace; remove your "
                    "own files by name with a workspace-relative path")
    return None


def _check_segment(seg, depth):
    if len(seg) > 1 and seg[0] == "command" and seg[1] in ("-v", "-V"):
        return "readonly", None  # asks whether a command exists; runs nothing
    seg = _strip_wrappers(seg)
    if not seg:
        return "allow", None
    name = os.path.basename(seg[0])
    args = seg[1:]
    two = f"{name} {args[0]}" if args else name

    if name in DENY_COMMANDS:
        return "deny", f"refused: `{name}`: {DENY_COMMANDS[name]}"
    if name == "kill":
        # The first -N is the signal; a later negative number or 0 is a process group.
        pids = [a for a in args[1:] if a != "--"] if args and args[0].startswith("-") else args
        if any(p == "0" or re.match(r"^-\d+$", p) for p in pids):
            return "deny", "refused: `kill` of a process group or every process; give explicit PIDs"
    if name == "rm":
        problem = _rm_problem(args)
        if problem:
            return "deny", problem
    if name == "find" and ("-delete" in args or "-exec" in args or "-execdir" in args) \
            and any(a.startswith(("/", "~", "..")) for a in args[:1]):
        return "deny", "refused: `find` with -delete/-exec outside the workspace"
    if name in ("chmod",) and any(a in ("-R", "--recursive") for a in args) \
            and any(a.startswith(("/", "~")) for a in args):
        return "deny", "refused: recursive chmod outside the workspace"
    if name == "git":
        ident = re.compile(r"^(user|author|committer)\.(name|email)")
        # `-c user.name=x` anywhere, or `git config user.name x` (a key followed by a value)
        words = [a for a in args if not a.startswith("-")]
        setting = any(ident.match(a) and "=" in a for a in args) or (
            "config" in words and any(ident.match(w) and i + 1 < len(words)
                                      for i, w in enumerate(words)))
        if setting or any(a.startswith("--author") or a == "--reset-author" for a in args):
            return "deny", ("refused: commit with the identity the repository is configured "
                            "with; do not set or override the author or committer")
    for a in seg:
        if a.endswith("gpt-assist/assist.py") or a.endswith("/notes.py") and "gpt-notes" in a:
            return "deny", ("refused: gpt-assist cannot run inside the sandbox; use the ask_gpt "
                            "tool if you have it, otherwise skip the step and say so")
        if a.startswith("/var/run/docker") or a.startswith("/run/docker") or "docker.sock" in a:
            return "deny", "refused: the Docker socket is off limits"
        if a.startswith("/dev/sd") or a.startswith("/dev/nvme"):
            return "deny", "refused: raw disk devices"
        if is_secret(a.split("=", 1)[-1]):
            return "deny", f"refused: {a} looks like a secret (keys, .env, credentials, .iptrust)"

    # A shell or interpreter given code on the command line: check the code too.
    if name in SHELLS or name == "eval":
        for i, a in enumerate(args):
            if a == "-c" and i + 1 < len(args) or name == "eval":
                inner = args[i + 1] if a == "-c" else " ".join(args)
                v, why = check_command(inner, depth + 1)
                if v in ("deny", "ask"):
                    return v, why
                break

    for key, why in ASK_COMMANDS.items():
        k = key.split()
        if name == k[0] and (len(k) == 1 or all(x in args for x in k[1:])):
            return "ask", f"`{key}` needs your approval: {why}"

    # A few read-only tools can be told to write a file; then they are ordinary commands.
    writes_file = any(a == "-o" or a.startswith("--output") for a in args) or (
        name == "uniq" and len([a for a in args if not a.startswith("-")]) > 1) or (
        # sort -ofile and -uo file (the option bundled with others); a program it is told to run
        name == "sort" and any(re.match(r"^-[A-Za-z]*o", a) or a.startswith("--compress-program")
                               for a in args)) or (
        # rg --pre CMD runs CMD on every file it searches
        name in ("rg", "grep", "egrep", "fgrep") and any(a.startswith("--pre") for a in args))
    if name in READONLY:
        return ("allow" if writes_file else "readonly"), None
    if two == "git config":
        reads = any(a in ("--get", "--get-all", "--list", "-l", "--get-regexp") for a in args) \
            or len([a for a in args[1:] if not a.startswith("-")]) == 1
        return ("readonly" if reads else "allow"), None
    if name == "git" and args and args[0] in READONLY_GIT and two not in ("git config",) \
            and not any(a in ("-d", "-D", "--delete", "-m", "-M", "add", "remove", "set-url",
                              "rename") for a in args[1:]):
        # `git tag` / `git branch` / `git remote` with no change flags only list things
        if args[0] in ("tag", "branch", "remote") and any(not a.startswith("-") for a in args[1:]):
            return "allow", None
        if writes_file:
            return "allow", None
        return "readonly", None
    if name == "find" and not any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fls") or
                                  a.startswith("-fprint") for a in args):
        return "readonly", None
    return "allow", None


def check_command(command, depth=0):
    """Judge one bash command line. See the module docstring for the verdicts."""
    if depth > 3:
        return "deny", "refused: command nested too deeply to check"
    if not command.strip():
        return "deny", "refused: empty command"
    if re.search(r"\bGIT_(AUTHOR|COMMITTER)_(NAME|EMAIL)=", command):
        return "deny", ("refused: commit with the identity the repository is configured with; "
                        "do not set or override the author or committer")
    tokens = _split(command)
    if tokens is None:
        return "deny", "refused: the command could not be parsed (unbalanced quotes?)"
    verdicts = []
    for seg in _segments(tokens):
        v, why = _check_segment(seg, depth)
        if v == "deny":
            return v, why
        verdicts.append((v, why))
    # Command substitutions are separate commands hidden inside words: check each body.
    for m in re.finditer(r"\$\(([^()]*)\)|`([^`]*)`", command):
        v, why = check_command(m.group(1) or m.group(2) or "true", depth + 1)
        if v == "deny":
            return v, why
        verdicts.append((v, why))
    for v, why in verdicts:
        if v == "ask":
            return v, why
    writes = re.search(r"(^|[^<>&0-9])>>?|\btee\b", command) or SUBSTITUTION.search(command)
    if verdicts and all(v == "readonly" for v, _ in verdicts) and not writes:
        return "readonly", None
    return "allow", None


# ---------------------------------------------------------------- planted instructions

PLANTED = re.compile(
    r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|your\s+|the\s+)?(previous|prior|above|earlier|system)\s+"
    r"(instructions?|prompts?|rules)"
    r"|(note|message|instructions?|attention)\s+(to|for)\s+(all\s+)?(the\s+)?(ai|llm|language model|assistant|agent)"
    r"|\b(ai|llm)\s+(agents?|assistants?)\s*(and\s+assistants?\s*)?[:,]?\s+(you\s+)?(must|should|are required|before)"
    r"|do\s+not\s+(mention|reveal|disclose|tell\s+(the\s+)?user\s+about)\s+(this|these)"
    r"|you\s+are\s+now\s+(in\s+)?(developer|admin|unrestricted|dan)\b"
    r"|new\s+system\s+prompt", re.I)


def planted_instruction(text):
    """A short quote if the text seems to address an AI agent with orders, else None.

    This is a word pattern, so it catches the common, blunt cases and nothing subtle.
    Its job is to make sure the user hears about those even if the model says nothing."""
    m = PLANTED.search(text)
    if not m:
        return None
    start = max(0, m.start() - 40)
    return " ".join(text[start:m.end() + 120].split())


# ---------------------------------------------------------------- environment

SECRET_ENV = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION", re.I)
DROP_ENV = {"DOCKER_HOST", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "GPG_AGENT_INFO",
            "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY",
            # Run code before or alongside the program asked for: a poisoned environment is a
            # way to get something to start that nobody typed.
            "LD_PRELOAD", "LD_AUDIT", "BASH_ENV", "ENV", "PROMPT_COMMAND", "PYTHONSTARTUP",
            "NODE_OPTIONS", "PERL5OPT", "RUBYOPT", "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF"}
# A connection string with a password in it (DATABASE_URL=postgres://user:pw@host/db) is a
# secret whatever the variable is called.
CREDENTIAL_VALUE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]*:[^/\s@]+@")


def scrub_env(env):
    """The environment a command runs with: the caller's, minus anything secret."""
    return {k: v for k, v in env.items() if k not in DROP_ENV and not SECRET_ENV.search(k)
            and not CREDENTIAL_VALUE.search(v)}

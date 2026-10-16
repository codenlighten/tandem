#!/usr/bin/env python3
"""GitHub for tandem, as the user's account, from outside the sandbox.

Installed as an added tool (`tandem tools add tools/github.json`). It reads one JSON
object on stdin and prints its result:

  {"action": "status", "branch": "B"}        accounts, repository, what a push of B would send
  {"action": "push", "branch": "B"}          send local branch B to the workspace's GitHub remote
  {"action": "pull_request", "branch": "B", "title": "...", "body": "...", "base": "main"}
  {"action": "list_pull_requests"}

`remote` (default origin) names the git remote whose URL gives the repository.

What it refuses: an account other than ACCOUNT, in gh or in ssh; a repository not owned
by one of OWNERS; anything but a fast-forward; commits, identities or pull-request text
that credit an AI.

The workspace is the model's to edit, including .git/config, which can name programs for
git to run. So no ordinary git command runs in it here. Its config file is read with
`git config --file`, and the branch is fetched from it into a new, empty repository,
which is where every check runs and the push is made from. That fetch runs only
`git upload-pack` in the workspace, serving objects. Git ignores a repository's own
uploadpack.packObjectsHook, and core.alternateRefsCommand is used by the side that
fetches, not the one serving; test_github_tool.py checks both, with an alternate in place,
against hooks, sshCommand, fsmonitor, insteadOf and the rest (git 2.43).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ACCOUNT = "codenlighten"
OWNERS = {"codenlighten"}

URL = re.compile(r"^(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
                 r"([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?$")
REMOTE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")
# What the user's rules forbid in anything published: an AI credited as author or helper.
# Broad on purpose: a false alarm costs a push from the user's own terminal, a miss does not.
AI = (r"(claude|anthropic|openai|chat\s?gpt|gpt[-\s]?\d*|copilot|gemini|bard|llama|mistral|"
      r"codex|cursor|devin|an? ai|ai|an? llm|llm|a language model|an? assistant)")
CREDIT = re.compile(r"co-authored-by:|assisted-by:|generated-by:|noreply@anthropic\.com|\U0001f916"
                    r"|\bai[-\s](assisted|generated|written)\b"
                    r"|\b(written|made|built|created|generated|produced|drafted|developed|authored|"
                    r"coded|assisted)\s+(with|by|using|via|through)\s+(the\s+help\s+of\s+)?\[?"
                    + AI + r"\b", re.I)
AI_IDENTITY = re.compile(r"claude|anthropic|openai|chatgpt|copilot|gemini|noreply@anthropic"
                         r"|^\s*(an?\s+)?(ai|llm|bot|assistant)\b", re.I)


class Refused(Exception):
    pass


def env():
    e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    e.update(GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1", LC_ALL="C",
             GIT_SSH_COMMAND="ssh -o BatchMode=yes")
    return e


def run(argv, cwd, check=True, stdin=None):
    r = subprocess.run(argv, cwd=cwd, env=env(), capture_output=True, text=True,
                       errors="replace", timeout=180, input=stdin)
    if check and r.returncode != 0:
        raise Refused(f"`{' '.join(argv[:3])}` failed: {(r.stderr or r.stdout).strip()[:600]}")
    return r


# ------------------------------------------------------------- what to push where

def config_file(workspace):
    """The repository's config file, found by reading files: a worktree's .git is a file
    naming its git dir, whose commondir names the main one."""
    git = os.path.join(workspace, ".git")
    if os.path.isfile(git):
        with open(git) as f:
            line = f.read().strip()
        if not line.startswith("gitdir:"):
            raise Refused("the workspace's .git file does not name a git directory")
        git = os.path.normpath(os.path.join(workspace, line[7:].strip()))
        common = os.path.join(git, "commondir")
        if os.path.isfile(common):
            with open(common) as f:
                git = os.path.normpath(os.path.join(git, f.read().strip()))
    path = os.path.join(git, "config")
    if not os.path.isfile(path):
        raise Refused("the workspace is not the top of a git repository")
    return git, path


def ssh_url(owner, name):
    return f"git@github.com:{owner}/{name}.git"


def repository(workspace, remote, tmp):
    if not REMOTE.match(remote):
        raise Refused(f"not a remote name: {remote!r}")
    _, cfg = config_file(workspace)
    r = run(["git", "config", "--file", cfg, "--get", f"remote.{remote}.url"], tmp, check=False)
    url = r.stdout.strip()
    if not url:
        raise Refused(f"the workspace has no remote {remote!r}")
    m = URL.match(url)
    if not m:
        raise Refused(f"remote {remote!r} is not a GitHub repository: {url}")
    owner, name = m.groups()
    if owner not in OWNERS:
        raise Refused(f"{owner}/{name} is not owned by {', '.join(sorted(OWNERS))}")
    return owner, name


def logins():
    """(the gh account, the account GitHub's ssh greets)."""
    gh = subprocess.run(["gh", "api", "user", "--jq", ".login"], env=env(), capture_output=True,
                        text=True, timeout=60)
    ssh = subprocess.run(["ssh", "-T", "-o", "BatchMode=yes", "git@github.com"], env=env(),
                         capture_output=True, text=True, timeout=60)
    m = re.search(r"Hi ([A-Za-z0-9-]+)!", ssh.stdout + ssh.stderr)
    return (gh.stdout.strip() if gh.returncode == 0 else None), (m.group(1) if m else None)


def require_account():
    gh, ssh = logins()
    if gh != ACCOUNT or ssh != ACCOUNT:
        raise Refused(f"GitHub must be {ACCOUNT} in both gh and ssh; gh is {gh or 'not signed in'}, "
                      f"ssh is {ssh or 'not recognised'}")


def check_branch(branch, tmp):
    if not isinstance(branch, str) or not branch or branch.startswith("-") or \
            run(["git", "check-ref-format", "--branch", branch], tmp, check=False).returncode:
        raise Refused(f"not a branch name: {branch!r}")


def raw_commits(repo, shas, tmp):
    """(short id, author, committer, message) for each commit, read from its raw object, so
    nothing in a message can pass for a field separator."""
    data = run(["git", "-C", repo, "cat-file", "--batch"], tmp,
               stdin="".join(s + "\n" for s in shas)).stdout.encode(errors="surrogateescape")
    out, at = [], 0
    for sha in shas:
        end = data.index(b"\n", at)
        name, kind, size = data[at:end].decode().split()
        if name != sha or kind != "commit":
            raise Refused(f"unexpected object {name} ({kind})")
        body = data[end + 1:end + 1 + int(size)].decode("utf-8", "replace")
        at = end + 2 + int(size)
        header, _, message = body.partition("\n\n")
        who = {k: v for k, _, v in (l.partition(" ") for l in header.splitlines())
               if k in ("author", "committer")}
        strip = lambda v: re.sub(r"\s+\d+\s+[-+]\d{4}$", "", v or "")
        out.append((sha[:7], strip(who.get("author")), strip(who.get("committer")), message))
    return out


class Plan:
    """A branch fetched into a new repository beside what GitHub already has."""

    def __init__(self, workspace, owner, name, branch, tmp):
        self.branch, self.url = branch, ssh_url(owner, name)
        self.repo = os.path.join(tmp, "repo.git")
        run(["git", "init", "-q", "--bare", "--template=", self.repo], tmp)
        ref = f"refs/heads/{branch}"
        if run(["git", "-C", self.repo, "fetch", "-q", "--no-tags", "--upload-pack=git-upload-pack",
                workspace, f"+{ref}:{ref}"], tmp, check=False).returncode:
            raise Refused(f"there is no local branch {branch!r}")
        run(["git", "-C", self.repo, "fetch", "-q", "--no-tags", self.url,
             "+refs/heads/*:refs/remotes/gh/*"], tmp)
        g = lambda *a, **k: run(["git", "-C", self.repo, *a], tmp, **k).stdout
        self.local = g("rev-parse", ref).strip()
        self.remote = g("rev-parse", "--verify", "-q", f"refs/remotes/gh/{branch}", check=False).strip()
        self.fast_forward = not self.remote or not run(
            ["git", "-C", self.repo, "merge-base", "--is-ancestor", self.remote, self.local],
            tmp, check=False).returncode
        new = [ref, "--not", "--remotes=gh"]
        self.commits = raw_commits(self.repo, g("rev-list", *new).split(), tmp)
        added = [l for l in g("log", "-p", "--format=", *new).splitlines()
                 if l.startswith("+") and not l.startswith("+++")]
        self.problems = []
        for short, author, committer, message in self.commits:
            short = short.strip()
            for who in {author, committer}:
                if AI_IDENTITY.search(who):
                    self.problems.append(f"{short}: identity {who}")
            for line in message.splitlines():
                if CREDIT.search(line):
                    self.problems.append(f"{short}: message line {line.strip()!r}")
        for line in added:
            if CREDIT.search(line):
                self.problems.append(f"added line {line[1:].strip()[:120]!r}")

    def describe(self):
        state = ("already on GitHub" if self.remote == self.local else
                 "a new branch on GitHub" if not self.remote else
                 f"a fast-forward from {self.remote[:7]}" if self.fast_forward else
                 f"NOT a fast-forward: GitHub's {self.branch} is at {self.remote[:7]}, "
                 "which the local branch does not contain")
        lines = [f"{self.branch} -> {self.url}: {state}; {len(self.commits)} new commits"]
        for short, author, _, message in self.commits[:30]:
            lines.append(f"  {short.strip()} {message.strip().splitlines()[0][:80] if message.strip() else ''}"
                         f"  ({author})")
        if len(self.commits) > 30:
            lines.append(f"  ... and {len(self.commits) - 30} more")
        lines += [f"  credits an AI: {p}" for p in self.problems[:20]]
        return "\n".join(lines)


# ------------------------------------------------------------- actions

def status(workspace, a, tmp):
    gh, ssh = logins()
    out = [f"gh account: {gh or 'not signed in'}; ssh account: {ssh or 'not recognised'}; "
           f"required: {ACCOUNT}"]
    owner, name = repository(workspace, a.get("remote", "origin"), tmp)
    out.append(f"repository: {owner}/{name}")
    if a.get("branch"):
        check_branch(a["branch"], tmp)
        out.append(Plan(workspace, owner, name, a["branch"], tmp).describe())
    return "\n".join(out)


def push(workspace, a, tmp):
    remote = a.get("remote", "origin")
    owner, name = repository(workspace, remote, tmp)
    check_branch(a.get("branch"), tmp)
    require_account()
    plan = Plan(workspace, owner, name, a["branch"], tmp)
    if plan.problems:
        raise Refused("these commits credit an AI, which the user's rules forbid:\n" + plan.describe())
    if not plan.fast_forward:
        raise Refused("this would rewrite GitHub's history, and force pushes are not done here:\n"
                      + plan.describe())
    if plan.remote == plan.local:
        return plan.describe()
    ref = f"refs/heads/{plan.branch}"
    run(["git", "-C", plan.repo, "push", "-q", plan.url, f"{ref}:{ref}"], tmp)
    # The workspace's own record of the remote is left alone: writing into its .git from out
    # here could be steered by a link. The model can move it from inside the sandbox.
    return (f"pushed.\n{plan.describe()}\nTo record it in the workspace, run in bash: "
            f"git update-ref refs/remotes/{remote}/{plan.branch} {plan.local}")


def pull_request(workspace, a, tmp):
    owner, name = repository(workspace, a.get("remote", "origin"), tmp)
    check_branch(a.get("branch"), tmp)
    if a.get("base"):
        check_branch(a["base"], tmp)
    title, body = a.get("title") or "", a.get("body") or ""
    if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
        raise Refused("a pull request needs a title (and its body, if any, as text)")
    for line in (title + "\n" + body).splitlines():
        if CREDIT.search(line):
            raise Refused(f"the pull request text credits an AI: {line.strip()!r}")
    require_account()
    with open(os.path.join(tmp, "body.md"), "w") as f:
        f.write(body)
    argv = ["gh", "pr", "create", "--repo", f"{owner}/{name}", "--head", a["branch"],
            "--title", title, "--body-file", os.path.join(tmp, "body.md")]
    if a.get("base"):
        argv += ["--base", a["base"]]
    return "opened: " + run(argv, tmp).stdout.strip()


def list_pull_requests(workspace, a, tmp):
    owner, name = repository(workspace, a.get("remote", "origin"), tmp)
    r = run(["gh", "pr", "list", "--repo", f"{owner}/{name}", "--state", "all", "--limit", "20",
             "--json", "number,title,state,headRefName,baseRefName,url"], tmp)
    prs = json.loads(r.stdout or "[]")
    return "\n".join(f"#{p['number']} [{p['state']}] {p['headRefName']} -> {p['baseRefName']}: "
                     f"{p['title']}  {p['url']}" for p in prs) or "(no pull requests)"


ACTIONS = {"status": status, "push": push, "pull_request": pull_request,
           "list_pull_requests": list_pull_requests}


def main(stdin=sys.stdin, workspace=None):
    try:
        a = json.load(stdin)
        if not isinstance(a, dict) or a.get("action") not in ACTIONS:
            raise Refused(f"action is one of {', '.join(ACTIONS)}")
        tmp = tempfile.mkdtemp(prefix="cg-github.")
        try:
            print(ACTIONS[a["action"]](workspace or os.getcwd(), a, tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0
    except Refused as e:
        print(f"refused: {e}")
        return 1
    except (ValueError, subprocess.TimeoutExpired) as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

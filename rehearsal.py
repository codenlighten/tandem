"""Read-only source-tree inspection for a future independent rehearsal repository.

This is not a trial, a secret scanner, or a sandbox preflight. No repository is
created or model code run here.
"""
import os
import subprocess

import policy


class InspectionError(Exception):
    pass


def git(cwd, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=cwd,
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=30)
    except OSError as exc:
        raise InspectionError("Git could not inspect the source repository") from exc
    if result.returncode:
        raise InspectionError("Git could not inspect the source repository")
    return result.stdout


def inspect(cwd):
    """Resolve HEAD, reject known unsafe tree entries, and describe excluded work."""
    root = os.fsdecode(git(cwd, "rev-parse", "--show-toplevel")[:-1])
    commit = git(root, "rev-parse", "--verify", "HEAD^{commit}").strip().decode("ascii")
    tree = git(root, "rev-parse", "--verify", commit + "^{tree}").strip().decode("ascii")
    entries = git(root, "ls-tree", "-rz", "--full-tree", tree).split(b"\0")
    count = 0
    for entry in entries:
        if not entry:
            continue
        try:
            meta, name = entry.split(b"\t", 1)
            mode, kind, oid = meta.split(b" ")
        except ValueError as exc:
            raise InspectionError("Malformed source tree entry") from exc
        if (not name or name.startswith(b"/") or b"\0" in name
                or any(part in (b"", b".", b"..") for part in name.split(b"/"))):
            raise InspectionError("Invalid path in source tree")
        if mode not in (b"100644", b"100755") or kind != b"blob":
            raise InspectionError("Source tree contains a symlink, submodule or unsupported entry")
        parts = name.split(b"/")
        if any(part.lower() == b".git" for part in parts):
            raise InspectionError("Source tree contains Git metadata paths")
        if policy.is_secret(os.fsdecode(name)):
            raise InspectionError("Source tree contains a known secret path")
        count += 1
    dirty = bool(git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"))
    return {"root": root, "commit": commit, "tree": tree, "files": count,
            "uncommitted_excluded": dirty}

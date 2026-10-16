"""Shared storage for handbacks written by the harness and read by Claude's hook."""
import contextlib
import fcntl
import glob
import hashlib
import json
import os
import re
import tempfile


def directory(home=None):
    return os.path.join(home or os.path.expanduser("~"), ".tandem", "handback")


def record_path(workspace, home=None):
    key = hashlib.sha256(os.fsencode(os.path.realpath(workspace))).hexdigest()
    return os.path.join(directory(home), key + ".json")


def legacy_path(workspace, home=None):
    return os.path.join(directory(home), re.sub(r"[^A-Za-z0-9]", "-", workspace) + ".json")


def archive_path(path, session):
    key = hashlib.sha256(os.fsencode(session)).hexdigest()[:24]
    return path[:-len(".json")] + "." + key + ".json"


def record_paths(path):
    """The active handback followed by earlier sessions in the same workspace."""
    return [path] + sorted(glob.glob(path[:-len(".json")] + ".*.json"))


@contextlib.contextmanager
def locked(path):
    # Lock a separate inode: replacing the JSON file must not release the lock.
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read(path):
    with open(path) as f:
        return json.load(f)


def write(path, record):
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def deliveries(record):
    """Old handbacks used a list; those recipients saw the old revision (zero)."""
    told = record.get("delivered") or {}
    return {session: 0 for session in told} if isinstance(told, list) else dict(told)

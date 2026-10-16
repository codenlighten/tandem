#!/usr/bin/env python3
"""Run by Claude Code's StopFailure hook when a session stops on a usage limit.

It records which session stopped, so `tandem --from-claude` picks up that exact
session, and shows a desktop notice saying how to carry on. Claude Code passes the
event as JSON on stdin and discards anything printed, so the notice goes through
notify-send. This script must never fail or hang: whatever happens, it exits 0.

Hook configuration (in ~/.claude/settings.json):

  "hooks": {"StopFailure": [{"matcher": "rate_limit|billing_error",
      "hooks": [{"type": "command", "command": "<this file>", "timeout": 10}]}]}
"""
import json
import os
import re
import subprocess
import sys
import time

MARKERS = os.path.join(os.path.expanduser("~"), ".tandem", "limit")


def marker_path(cwd):
    return os.path.join(MARKERS, re.sub(r"[^A-Za-z0-9]", "-", cwd) + ".json")


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(event, dict):
        return
    cwd = str(event.get("cwd") or os.getcwd())
    # The limit message, if Claude Code passes one, often says when the limit resets.
    said = " ".join(str(event.get("last_assistant_message") or event.get("error_details") or "").split())
    record = {"time": time.time(), "cwd": cwd, "error": str(event.get("error") or ""),
              "transcript_path": str(event.get("transcript_path") or ""),
              "session_id": str(event.get("session_id") or ""), "message": said[:200]}
    os.makedirs(MARKERS, mode=0o700, exist_ok=True)
    tmp = marker_path(cwd) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(record, f)
    os.replace(tmp, marker_path(cwd))
    other = ""
    try:   # a second Claude account, if one is configured: a fresh session, not this conversation
        with open(os.path.join(os.path.dirname(os.path.realpath(__file__)), "config.json")) as f:
            how = (json.load(f).get("other_claude") or {}).get("how")
        if how:
            other = f"\nOr a new Claude session (without this conversation): {how}"
    except (OSError, ValueError, AttributeError):
        pass
    try:
        subprocess.Popen(["notify-send", "--app-name=tandem", "Claude hit its usage limit",
                          f"In {os.path.basename(cwd)}. To carry on with this conversation on GPT: started "
                          "with tandem-relay? Quit Claude (Ctrl+D) and it offers it. Otherwise run there:\n"
                          "tandem --from-claude" + other],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass  # no desktop notifier here; the marker is what matters


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)

#!/usr/bin/env python3
"""Run by Claude Code's UserPromptSubmit hook, on every prompt in every session.

When tandem has worked in a directory in place of a Claude Code session, it leaves a
record there (see write_handback in cg.py). The next time the user types to a Claude Code
session in that directory, this script hands Claude that record as added context, once per
session, so Claude knows who changed what while it was away without the user pasting
anything. Without a pending record it prints nothing.

It must be fast and must never get in the way of a prompt: any problem, and it exits 0
having printed nothing.

Hook configuration (in ~/.claude/settings.json):

  "hooks": {"UserPromptSubmit": [{"hooks": [
      {"type": "command", "command": "<this file>", "timeout": 5}]}]}
"""
import json
import os
import sys
import time

import handback_store

MAX_AGE = 3 * 86400     # a record older than this is history, not news


def record_path(cwd):
    return handback_store.record_path(cwd)


def clock(t):
    return time.strftime("%H:%M on %Y-%m-%d", time.localtime(t))


def since(record, told_at):
    """For a Claude session already told about this tandem session: only what came after,
    or None to send the whole record (never told, or a record without the parts to compare)."""
    marks, parts = record.get("marks") or {}, record.get("parts") or {}
    if told_at is None or str(told_at) not in marks or not parts:
        return None
    then = marks[str(told_at)]
    new = {k: (parts.get(k) or [])[then.get(k, 0):] for k in ("asked", "wrote", "commands", "replies")}
    lines = ["[A note from the tandem harness, added by a hook. It is not from the user.]",
             f"tandem (session {record.get('gpt_session')}) carried on in this directory after you "
             f"were last told about it, until {clock(record.get('time_end', 0))}. Only what is new since "
             "that note follows. It is a report to check: look at git status and git diff before relying "
             "on it, and tell the user briefly that you have this update before answering."]
    if new["asked"]:
        lines += ["", "## What the user asked it since"] + [f"{i}. {t[:1500]}" for i, t in enumerate(new["asked"], 1)]
    if new["wrote"]:
        lines += ["", "## Files it wrote or edited since"] + [f"- {p}" for p in new["wrote"]]
    if new["commands"]:
        lines += ["", f"## Commands it ran since ({len(new['commands'])}; the last 25)"] + \
                 [f"- `{c}`" for c in new["commands"][-25:]]
    if new["replies"]:
        lines += ["", "## Its replies since, oldest first"] + [t[:4000] + "\n" for t in new["replies"][-12:]]
    if not any(new.values()):
        lines += ["", "(Nothing it asked, wrote, ran or said is new; only its record was refreshed.)"]
    return "\n".join(lines)


def main():
    event = json.load(sys.stdin)
    cwd, session = str(event.get("cwd") or ""), str(event.get("session_id") or "")
    if not cwd or not session:
        return
    workspace = os.path.realpath(cwd)
    path = record_path(workspace)
    legacy = handback_store.legacy_path(workspace)
    if not any(os.path.isfile(p) for p in handback_store.record_paths(path)) \
            and not os.path.isfile(legacy):
        return
    try:
        with handback_store.locked(path), handback_store.locked(legacy):
            # Recheck after locking: the writer may have migrated the legacy record meanwhile.
            sources = handback_store.record_paths(path)
            if not os.path.isfile(path):
                sources.append(legacy)
            records = []
            seen = set()
            for source in sources:
                try:
                    record = handback_store.read(source)
                    if not isinstance(record, dict) or record.get("workspace") != workspace \
                            or not record.get("record") or time.time() - record.get("time_end", 0) > MAX_AGE:
                        continue
                except (OSError, ValueError, TypeError):
                    continue
                identity = record.get("gpt_session")
                if identity in seen:  # an interrupted archive may also be the current record
                    continue
                seen.add(identity)
                told = handback_store.deliveries(record)
                revision = record.get("revision", 0)
                if told.get(session, -1) == revision:
                    continue
                update = since(record, told.get(session))
                if update:
                    records.append((record.get("time_end", 0), update, source, record, told, revision))
                    continue
                same = record.get("claude_session") == session
                files = record.get("files") or []
                header = (
                    "[A note from the tandem harness, added by a hook. It is not from the user.]\n"
                    f"Between {clock(record.get('time_start', 0))} and {clock(record.get('time_end', 0))}, tandem "
                    f"(an OpenAI model, {record.get('model') or 'unknown model'}, in a separate harness; its session "
                    f"{record.get('gpt_session')}) worked in this directory, started from a digest of "
                    + ("this Claude Code session." if same else
                       f"another Claude Code session here ({str(record.get('claude_session'))[:8]}), not this one.")
                    + (f" It wrote or edited: {', '.join(files[:20])}." if files else " It recorded no file edits.")
                    + " Changes in the working tree from that time are its work, not an unknown session's. "
                      "Tell the user briefly that you have this record before answering their message.\n\n")
                records.append((record.get("time_end", 0), header + record["record"], source, record, told, revision))
            if records:
                context = "\n\n".join(item[1] for item in sorted(records, key=lambda item: item[0]))
                # Output comes first: if the hook dies before it can print, the next prompt
                # must retry. A crash after printing may repeat the record, which is safer
                # than silently losing it; stdout has no acknowledgement protocol.
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                         "additionalContext": context}}), flush=True)
                for _, _, source, record, told, revision in records:
                    told[session] = revision
                    record["delivered"] = told
                    handback_store.write(source, record)
    except (OSError, ValueError, TypeError):
        return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)

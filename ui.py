"""What the user sees: colour, the working indicator, tool results and the turn footer.

Colour and the indicator appear only on a terminal, and never with NO_COLOR set or
TERM=dumb, so piped output, logs and the tests read as plain text.
"""
import os
import re
import shutil
import sys
import threading
import time

RESET = "\x1b[0m"
CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
         "magenta": "35", "cyan": "36", "grey": "90"}
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_lock = threading.Lock()
_active = None


def colour_on():
    return (sys.stdout.isatty() and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM", "") != "dumb")


def paint(text, *styles):
    if not styles or not colour_on():
        return text
    return "\x1b[" + ";".join(CODES[s] for s in styles) + "m" + text + RESET


def width():
    return max(40, min(shutil.get_terminal_size((100, 24)).columns, 160))


def clip(line, room):
    return line if len(line) <= room else line[:max(1, room - 1)] + "…"


def out(text=""):
    """Print, first clearing the working indicator so the two never share a line."""
    stop()
    print(text, flush=True)


# ---------------------------------------------------------------- the working indicator

class Spinner:
    def __init__(self, label):
        self.label, self.start, self.done = label, time.time(), threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        i = 0
        while not self.done.wait(0.1):
            secs = int(time.time() - self.start)
            with _lock:
                if self.done.is_set():
                    break
                sys.stdout.write("\r\x1b[2K  " + paint(FRAMES[i % len(FRAMES)], "cyan") + " "
                                 + paint(f"{self.label} {secs}s" if secs else self.label, "dim")
                                 + paint("  (Ctrl-C to interrupt)" if secs >= 5 else "", "grey"))
                sys.stdout.flush()
            i += 1

    def stop(self):
        with _lock:
            if self.done.is_set():
                return
            self.done.set()
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()


def spin(label):
    """Show `label` with a spinner and the seconds elapsed until stop(), on a terminal only."""
    global _active
    stop()
    if colour_on():
        _active = Spinner(label)
        _active.thread.start()


def stop():
    global _active
    if _active:
        _active.stop()
        _active = None


# ---------------------------------------------------------------- lines the harness prints

def tool_line(text):
    """● name details: one line per tool call, the name in bold and the rest dimmed."""
    name, _, rest = text.partition(" ")
    room = width() - 4 - len(name) - 1
    return "  " + paint("●", "cyan") + " " + paint(name, "bold") + (
        " " + paint(clip(" ".join(rest.split()), room), "dim") if rest else "")


def refusal(why):
    return "    " + paint("✗ " + why, "red")


def diff(lines):
    """Unified diff lines, coloured: additions green, removals red, hunks cyan."""
    shown = []
    for l in lines:
        style = ("bold",) if l.startswith(("---", "+++")) else ("green",) if l.startswith("+") \
            else ("red",) if l.startswith("-") else ("cyan",) if l.startswith("@@") else ()
        shown.append("    " + paint(l, *style))
    return "\n".join(shown)


EXIT = re.compile(r"\n?\[(exit (-?\d+)|killed after [^\]]*)\]\s*$")


def result(name, output):
    """The ⎿ line under a tool call: what came of it, in a few lines at most. The model
    gets the whole output; this is so the user can follow along."""
    if not output or output.startswith("refused"):
        return None                              # a refusal was already shown when it happened
    if name in ("todo",):
        return None
    lines = output.rstrip("\n").splitlines()
    if output.startswith("error"):
        return _box([paint(clip(lines[0], width() - 8), "red")])
    if name == "read":
        return _box([paint(f"{len(lines)} lines", "dim")])
    if name in ("write", "edit"):
        return _box([paint(clip(lines[0], width() - 8), "dim")])
    if name == "glob":
        n = 0 if lines == ["(no files match)"] else len(lines)
        return _box([paint(f"{n} file{'s' * (n != 1)}", "dim")])
    if name == "grep":
        n = 0 if lines == ["(no matches)"] else len(lines)
        return _box([paint(f"{n} matching line{'s' * (n != 1)}", "dim")])
    if name == "skill":
        return _box([paint("loaded", "dim")])
    status = None
    m = EXIT.search(output)
    if m:
        status = m.group(1)
        lines = output[:m.start()].rstrip("\n").splitlines()
    keep = 4
    body = [paint(clip(l, width() - 8), "dim") for l in lines[:keep]]
    if len(lines) > keep:
        body.append(paint(f"… +{len(lines) - keep} lines", "grey"))
    if status and status != "exit 0":
        body.append(paint(status, "red"))
    elif not body:
        body.append(paint("done" if status else "(no output)", "dim"))
    return _box(body)


def _box(lines):
    return "\n".join(("    ⎿ " if i == 0 else "      ") + l for i, l in enumerate(lines))


def footer(seconds, turn_usd, session_usd, cap):
    took = f"{seconds:.0f}s" if seconds < 60 else f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    return "  " + paint(f"✓ {took} · ${turn_usd:.4f} this turn · ${session_usd:.4f} of "
                        f"${cap:.2f} this session", "grey")


def home(path):
    h = os.path.expanduser("~")
    return "~" + path[len(h):] if path == h or path.startswith(h + "/") else path

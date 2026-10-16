"""Tools the agent writes for itself, and the user approves before they exist.

The agent proposes a tool with the propose_tool tool: a name, a description, a JSON
schema for its arguments, and a Python program that reads those arguments as JSON on
stdin and prints its result. Nothing runs and nothing is installed on the agent's
word. The user reads the whole definition and says yes, in the session or later with
`tandem tools review NAME`; only then is it written to the approved store with
its sha256 in the index.

The store is outside every root the agent can write to, and a tool is loaded and run
only while its definition still hashes to what the user approved, so an approved tool
cannot be changed afterwards except by approving a new definition.

A tool runs in the bash sandbox unless its definition says otherwise. One that runs
outside it has the user's network, files and credentials, which is the point of such
a tool (pushing to GitHub, say) and the reason the user is asked before every call
unless they approved it with ask_each_call false.
"""
import hashlib
import json
import os
import re

HOME = os.path.expanduser("~")
STORE = os.path.join(HOME, ".config", "tandem", "tools")
NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
FIELDS = ("name", "description", "parameters", "code", "sandboxed", "net", "ask_each_call")
MAX_CODE = 20_000          # a program the user can actually read before saying yes
MAX_PARAMETERS = 5_000
MAX_ARGS = 4_000           # a call's arguments are shown in full when the user is asked
# Shown, not acted on: carriage returns, escape sequences and bidirectional overrides could
# otherwise hide or rewrite part of what the user is asked to approve.
CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
# Ways a program's behaviour can come from somewhere other than its approved text.
LOOK_CLOSELY = [
    (r"(?<![.\w])(exec|eval|compile)\s*\(|__import__|importlib|runpy|\bsys\.path\b",
     "it evaluates or imports code it builds or finds at run time"),
    (r"\bsubprocess\b|\bos\.(system|popen|exec\w*|spawn\w*|posix_spawn\w*)\b|\bpty\b",
     "it runs other programs: check exactly which, and that none is a file in the workspace"),
    (r"\bopen\s*\(|\bpathlib\b|\.read_text\s*\(|\bshutil\b",
     "it reads or writes files: check which ones"),
]


def digest(d):
    return hashlib.sha256(json.dumps({k: d[k] for k in FIELDS}, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def make(name, description, parameters, code, sandboxed=True, net=False, ask_each_call=True,
         taken=()):
    """A checked tool definition, or raise ValueError saying what is wrong."""
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError("name: lower case letters, digits and _, 2 to 31 characters, "
                         "starting with a letter")
    if name in taken:
        raise ValueError(f"name: {name!r} is a built-in tool")
    if not isinstance(description, str) or not description.strip() or len(description) > 2000:
        raise ValueError("description: a non-empty string of at most 2000 characters")
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise ValueError('parameters: a JSON schema object with "type": "object"')
    if len(json.dumps(parameters)) > MAX_PARAMETERS:
        raise ValueError(f"parameters: at most {MAX_PARAMETERS} characters of JSON")
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE:
        raise ValueError(f"code: a non-empty Python program of at most {MAX_CODE} characters")
    for flag, value in (("sandboxed", sandboxed), ("net", net), ("ask_each_call", ask_each_call)):
        if not isinstance(value, bool):
            raise ValueError(f"{flag}: true or false")
    return {"name": name, "description": description.strip(), "parameters": parameters,
            "code": code, "sandboxed": sandboxed, "net": net and sandboxed,
            "ask_each_call": ask_each_call}


def shown(text):
    """Untrusted text made safe to print: every control character appears as an escape."""
    return CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 256
                       else f"\\u{ord(m.group()):04x}", text)


def warnings(d):
    found = [why for pattern, why in LOOK_CLOSELY if re.search(pattern, d["code"])]
    if CONTROL.search(d["code"] + d["description"]):
        found.append("it contains control characters, shown escaped below")
    return found


def summary(d):
    """What the user reads before saying yes: everything that decides what the tool can do."""
    if d["sandboxed"]:
        reach = ("runs in the bash sandbox" + (", with web access (TCP 80/443)" if d["net"]
                                               else ", with no network"))
    else:
        reach = ("runs OUTSIDE the sandbox, with your network, files and credentials: "
                 "it can do anything you can")
    ask = "asks you before every call" if d["ask_each_call"] else "runs without asking you"
    look = "".join(f"\n  look closely: {w}" for w in warnings(d))
    if not d["sandboxed"] and look:
        look += ("\n  (an approval pins this text only: anything it loads or runs from a file "
                 "the model can edit can change after you say yes)")
    return (f"tool {d['name']}: {reach}; {ask}{look}\n"
            f"description: {shown(d['description'])}\n"
            f"arguments: {json.dumps(d['parameters'])}\n"
            f"program ({len(d['code'].splitlines())} lines, sha256 {digest(d)[:16]}):\n"
            + "\n".join("    " + shown(line) for line in d["code"].split("\n")))


def writable_by(roots, store=None):
    store = os.path.realpath(store or STORE)
    return any(store == r or store.startswith(r.rstrip("/") + "/")
               for r in map(os.path.realpath, roots))


def _write(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def _read(path):
    try:   # never through a link: the store's files are only ever plain files we wrote
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def index(store=None):
    return _read(os.path.join(store or STORE, "approved.json")) or {}


def propose(d, store=None):
    _write(os.path.join(store or STORE, "proposed", d["name"] + ".json"), d)


def proposed(store=None):
    folder = os.path.join(store or STORE, "proposed")
    names = sorted(n[:-5] for n in os.listdir(folder) if n.endswith(".json")) \
        if os.path.isdir(folder) else []
    return [d for d in (_read(os.path.join(folder, n + ".json")) for n in names) if d]


def approve(d, store=None):
    store = store or STORE
    _write(os.path.join(store, "approved", d["name"] + ".json"), d)
    idx = index(store)
    idx[d["name"]] = digest(d)
    _write(os.path.join(store, "approved.json"), idx)
    try:
        os.remove(os.path.join(store, "proposed", d["name"] + ".json"))
    except FileNotFoundError:
        pass


def remove(name, store=None):
    store = store or STORE
    idx = index(store)
    found = idx.pop(name, None) is not None
    _write(os.path.join(store, "approved.json"), idx)
    for sub in ("approved", "proposed"):
        try:
            os.remove(os.path.join(store, sub, name + ".json"))
            found = True
        except FileNotFoundError:
            pass
    return found


def get(name, store=None):
    """The approved definition of `name`, or None unless it hashes to what was approved."""
    store = store or STORE
    want = index(store).get(name)
    d = _read(os.path.join(store, "approved", name + ".json")) if want else None
    try:
        return d if d and digest(d) == want and d.get("name") == name else None
    except (KeyError, TypeError):
        return None


def load(store=None):
    """(approved tools by name, the names that failed their check)."""
    good, bad = {}, []
    for name in index(store):
        d = get(name, store)
        if d:
            good[name] = d
        else:
            bad.append(name)
    return good, bad

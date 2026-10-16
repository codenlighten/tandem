#!/usr/bin/env python3
"""Builds team-guide.pdf, the short guide to tandem and tandem-relay for the team.

  python3 docs/make_guide.py        # needs reportlab and the DejaVu fonts

The text lives here, so the guide can be corrected and rebuilt when the tool changes.
"""
import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph, Preformatted,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = "/usr/share/fonts/truetype/dejavu/"
for name, file in (("Body", "DejaVuSans.ttf"), ("Body-Bold", "DejaVuSans-Bold.ttf"),
                   ("Body-Italic", "DejaVuSans-Oblique.ttf"), ("Mono", "DejaVuSansMono.ttf")):
    pdfmetrics.registerFont(TTFont(name, FONTS + file))
pdfmetrics.registerFontFamily("Body", normal="Body", bold="Body-Bold", italic="Body-Italic")

INK, MUTED, RULE, SHADE, ACCENT = "#1b1f24", "#57606a", "#d0d7de", "#f4f6f8", "#0b5cad"
body = ParagraphStyle("body", fontName="Body", fontSize=9.6, leading=14, textColor=INK, alignment=TA_LEFT,
                      spaceAfter=6)
small = ParagraphStyle("small", parent=body, fontSize=8.4, leading=12, textColor=MUTED)
cell = ParagraphStyle("cell", parent=body, fontSize=8.8, leading=12.2, spaceAfter=0)
head = ParagraphStyle("head", parent=cell, fontName="Body-Bold")
h1 = ParagraphStyle("h1", parent=body, fontName="Body-Bold", fontSize=14, leading=18, spaceBefore=14,
                    spaceAfter=6, textColor=ACCENT, keepWithNext=True)
h2 = ParagraphStyle("h2", parent=body, fontName="Body-Bold", fontSize=10.8, leading=14, spaceBefore=8, spaceAfter=4,
                    keepWithNext=True)
title = ParagraphStyle("title", parent=body, fontName="Body-Bold", fontSize=22, leading=27, spaceAfter=4)
sub = ParagraphStyle("sub", parent=body, fontSize=11.5, leading=16, textColor=MUTED, spaceAfter=10)
code = ParagraphStyle("code", fontName="Mono", fontSize=8.4, leading=11.6, textColor=INK, backColor=SHADE,
                      borderPadding=(6, 7, 6, 7), leftIndent=7, rightIndent=7, spaceBefore=4, spaceAfter=11)

WIDTH = letter[0] - 1.7 * inch


def P(text, style=body):
    return Paragraph(text, style)


def C(text):
    return Preformatted(text.strip("\n"), code)


def bullets(items, numbered=False):
    return ListFlowable([ListItem(P(i, ParagraphStyle("li", parent=body, spaceAfter=2)), leftIndent=16)
                         for i in items], bulletType="1" if numbered else "bullet", start=None if numbered else "•",
                        bulletFontName="Body", bulletFontSize=9.6, leftIndent=16, spaceAfter=6)


def table(rows, widths):
    data = [[P(c, head if i == 0 else cell) for c in row] for i, row in enumerate(rows)]
    t = Table(data, colWidths=[w * WIDTH for w in widths], repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(SHADE)),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor(RULE)),
        ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.HexColor(RULE)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6)]))
    return t


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Body", 7.8)
    canvas.setFillColor(colors.HexColor(MUTED))
    canvas.drawString(0.85 * inch, 0.55 * inch, "tandem and tandem-relay: team guide, 9 October 2026")
    canvas.drawRightString(letter[0] - 0.85 * inch, 0.55 * inch, f"Page {doc.page}")
    canvas.restoreState()


m = lambda s: f'<font name="Mono" size="8.6">{s}</font>'   # inline code

story = [
    P("tandem and tandem-relay", title),
    P("Carrying on with an OpenAI model when Claude Code runs out of credits, and handing the work back", sub),

    P("What this is", h1),
    P("When Claude Code hits its usage limit, work stops until the limit resets. This integration lets you "
      "carry on in the same terminal with an OpenAI model that has been given the same instructions, the same "
      "kind of tools and a digest of the session that stopped, and then return to the same Claude session with "
      "a record of what was done in between."),
    bullets([
        f"<b>tandem</b> is a coding agent shaped like Claude Code but driven by an OpenAI model. It reads "
        f"the same {m('CLAUDE.md')} files, skills and project memory, and has the same kind of tools: read, "
        "write, edit, run commands, search.",
        "<b>tandem-relay</b> is a thin wrapper you start in place of <font name=\"Mono\" size=\"8.6\">claude</font>. "
        "It runs Claude Code normally and only steps in when a session stops on a usage limit.",
    ]),
    P("It is not Claude. The harness is the same shape; the judgement is the model's. On the small tasks we "
      "measured it followed our working rules reliably (see \u201cHow well it follows our way of working\u201d), "
      "which is what makes it usable as a stand-in, "
      "but it has not been measured on large codebases or sessions lasting hours."),

    P("The short version", h1),
    C("""tandem-relay                 # start Claude Code this way from now on (same arguments as claude)

  ...Claude hits its limit...  quit it with Ctrl+D, then press Enter to carry on with GPT
  ...work with GPT...          type /back to return to the same Claude session"""),
    P("That is the whole routine. The rest of this guide explains what happens at each step, what to watch "
      "for, and how to set it up on another machine."),

    P("The hand-over, step by step", h1),
    P("Claude to GPT", h2),
    bullets([
        "Claude Code shows its limit message and stays open. A desktop notice appears saying the relay is ready.",
        "Quit Claude with <b>Ctrl+D</b>. The relay prints the limit message, including the reset time if Claude "
        "gave one, and asks: <i>Carry on here with GPT (tandem)? [Enter = yes, n = no]</i>",
        "Press <b>Enter</b>. tandem starts in the same terminal. It first checks the real state (git status, "
        "the files involved), then tells you what Claude had finished, what it had left unfinished, and what it "
        "will do next, and carries on.",
    ], numbered=True),
    P("Two keys, not one: a limit does not make Claude Code exit, and nothing can take over its terminal while "
      "it is running. The relay never quits Claude for you, so waiting for the limit to reset stays your choice.", small),
    P("GPT back to Claude", h2),
    bullets([
        f"Type {m('/back')} in tandem (or {m('/exit')}, then Enter at the question). If you did not "
        "start through the relay, simply go back to your Claude session.",
        "The relay resumes <b>the same Claude session</b>, with a first message that records what you asked GPT "
        "and what it did: files changed, commands run, its replies, and the exact diff since the takeover.",
        "Claude has its own full history plus that record. The record tells Claude to treat it as a report to "
        "check, so expect Claude to look at git status and the files before continuing.",
    ], numbered=True),
    P("You never paste anything. The next time you type to a Claude Code session in that directory, a hook "
      "gives Claude the record automatically, once per session, so it knows which edits were the stand-in's. "
      "If Claude is still limited when resumed, the relay offers GPT again and continues the same GPT session.", small),

    P("What carries over", h2),
    table([
        ["Direction", "What the other side receives", "What it does not receive"],
        ["Claude to GPT", "Every message you typed; the files Claude wrote or edited; its last forty steps; its "
         "final message in full; any tool call that was started but never finished; the repository's current state.",
         "The full conversation, tool output, or Claude's reasoning. The digest is at most 48,000 characters."],
        ["GPT to Claude", "What you asked GPT; the files it changed; the commands it ran; its task list; its "
         "replies; and the exact diff of the project since the takeover.",
         "More than 12,000 characters in all. A long diff is cut, with the command that shows the rest. The work "
         "itself is never cut: Claude sees the real files."],
    ], [0.15, 0.48, 0.37]),

    P("Using tandem directly", h1),
    P("You do not need the relay to use the stand-in. Run these from the project directory."),
    table([
        ["Command", "What it does"],
        [m("tandem"), "An interactive session in this directory. If Claude Code recently stopped on a limit "
                          "here, it offers to take over from that session."],
        [m("tandem --from-claude"), "Take over from the latest Claude Code session for this directory."],
        [m("tandem -c"), "Continue the latest tandem session for this directory."],
        [m("tandem --resume ID"), "Continue that tandem session. Given a Claude Code session id (the one "
                                      "Claude prints when it exits), take over from that session."],
        [m('tandem run "fix the failing test" --mode auto'), "One task with no prompts, then exit."],
        [m("tandem --tier quick"), "Use the cheap model (about a twentieth of the cost)."],
        [m("tandem --net"), "Allow web access: fetching a page, and web ports in commands."],
        [m("tandem --strict-secrets"), "Make secret files in the project unreadable to commands (see Safety)."],
        [m("tandem export"), "Print the latest session as a readable record."],
        [m("tandem usage"), "Spend today and in total."],
    ], [0.42, 0.58]),
    Spacer(1, 8),
    P(f"Inside a session: {m('/cost')}, {m('/compact')}, {m('/mode ask|edits|auto')}, {m('/tasks')}, "
      f"{m('/&lt;skill&gt; request')}, {m('/back')}, {m('/exit')}. Ctrl+C interrupts the current turn."),
    table([
        ["Approval mode", "What it asks you before doing"],
        ["ask (the default)", "Every file edit, and every command that is not read-only. It shows the diff first."],
        ["edits", "Commands only; edits inside the project go ahead."],
        ["auto", "Only actions that publish or cannot be undone. Use it for unattended runs."],
    ], [0.24, 0.76]),
    P("In every mode, pushing to a remote, registering IP, publishing a package, amending or discarding commits "
      "need a typed yes, and are refused when nobody is at the terminal.", small),

    P("Safety: what it cannot do", h1),
    P("An AI model choosing shell commands will eventually choose a bad one, so the limits are enforced by the "
      "operating system, not by instructions the model could ignore. Every command the stand-in runs is confined "
      "by the Linux kernel:"),
    bullets([
        "It can change files only inside the project and a private scratch folder.",
        "It cannot read your SSH keys, cloud credentials, or other projects.",
        "It has no network unless you start it with <font name=\"Mono\" size=\"8.6\">--net</font>, and then only "
        "web ports. It cannot reach Docker, databases on the machine, or other hosts.",
        "It cannot signal or inspect processes it did not start, and cannot gain privileges.",
        "Each command is capped at 8 GB of memory and 512 processes in total, and everything it started is "
        "stopped when the command ends.",
    ]),
    P("On top of that, a rule layer refuses known-dangerous commands outright with a reason the model can act on "
      f"({m('pkill')}, anything Docker, {m('sudo')}, {m('ssh')}, recursive deletes outside the project), and the "
      "values in your " + m(".env") + " files are removed from anything sent to the model."),
    P("What is not protected", h2),
    bullets([
        f"<b>Secrets inside the project.</b> By default a {m('.env')} in the project is guarded by name rules and "
        f"a value filter, which a determined command could get around. {m('--strict-secrets')} closes this at "
        "the kernel, at a cost: in a folder that holds a secret, a file a command creates cannot be read back "
        "by that same command.",
        "<b>Everything the model reads goes to OpenAI</b>, including the digest of the Claude session.",
        "<b>Code it writes runs unconfined when you run it later.</b> Review its changes as you would anyone's. "
        "It warns you at once if a command touches git hooks.",
        "<b>Text that tries to instruct it.</b> If a file or web page contains text addressed to AI agents, the "
        "harness warns you directly. This catches blunt cases only.",
    ]),

    P("Cost", h1),
    table([
        ["Tier", "Model", "Typical cost", "Use it for"],
        ["quick", "gpt-6-luna", "Under a cent per task", "Small, well-defined tasks; checking where work stands"],
        ["standard (default)", "gpt-6-sol", "2 to 15 cents per task", "Real work, and anything you would give Claude"],
        ["deep", "gpt-6-astra", "About five times standard", "Rarely; not evaluated"],
    ], [0.2, 0.17, 0.25, 0.38]),
    Spacer(1, 6),
    P("Every call is costed before it is made. A run stops when the next call could pass any of three limits: "
      "$1 per run (change with " + m("--max-usd") + "), and $10 a day across everything that shares the OpenAI "
      "key, of which tandem leaves the last dollar for second-opinion calls. If an interactive session is "
      "refused on budget, it asks whether to allow more: type an amount in dollars and it carries on. Measured examples: taking over a session and reporting where it stood cost "
      "$0.0035 on the quick tier; a feature across four files cost about 8 cents on standard; the stand-in "
      "adding a feature to its own code cost 16 cents."),

    P("How well it follows our way of working", h1),
    P("We test it with 20 small tasks, each run in a fresh project with nobody at the terminal and scored by "
      "automated checks: a bug fix, a clean commit, an honest report when something cannot pass, asking before "
      "an irreversible delete, following project rules and skills, working through summarised history, and "
      "taking over a half-finished change."),
    table([
        ["Model", "Result on the first 19 cases", "Take-over case"],
        ["gpt-6-sol (standard)", "38 of 38 runs passed (2 runs of each)", "2 of 2"],
        ["gpt-6-luna (quick)", "95 of 95 runs passed (5 runs of each)", "3 of 3"],
    ], [0.3, 0.45, 0.25]),
    Spacer(1, 6),
    P("We also gave 13 of the tasks to Claude itself. Claude passed 11; both OpenAI models passed all 13 inside "
      "this harness. <b>That does not mean the stand-in is better than Claude.</b> The instructions were tuned "
      "on these very tasks, Claude saw each one once, and the two Claude misses were partly about what each "
      "harness shows the model. What it does mean: on small tasks under our rules, we found no gap."),
    P("Read the numbers with these in mind: the tasks are small, there are two to five runs of each, and getting "
      "here took several rounds in which real failures were found and fixed (the cheap model once faked a missing "
      "package to make a test pass, and once deleted a file on a guess) and nine of our own answer keys turned "
      "out to be wrong. The full history is in the README.", small),

    P("What has not been proven yet", h1),
    table([
        ["Not yet seen working", "Why", "If it fails"],
        ["The limit notice appearing on a real limit", "We could not trigger a real usage limit. The hook follows "
         "Claude Code's documentation and its script is tested with made-up events.",
         f"Nothing is lost: run {m('tandem --from-claude')} yourself. It picks the most recent session."],
        ["Claude resuming with the hand-back message", f"Launching Claude Code from the session that built this "
         f"was not possible. The command, {m('claude --resume &lt;id&gt; \"message\"')}, matches Claude's own help text.",
         f"Check in ten seconds with any old session id and the message \"say hi\". If it does not send the "
         "message, tell Greg: there is a planned alternative."],
    ], [0.27, 0.4, 0.33]),
    Spacer(1, 6),
    P("Everything else in the hand-over is tested end to end with a stand-in for the " + m("claude") +
      " program, and the take-over and hand-back were each tried with real models."),

    P("Things to know", h2),
    bullets([
        "<b>One driver at a time.</b> The relay starts each program only after the other has exited. A background "
        "Claude session, or Claude Code's setting that resumes automatically when a limit resets, could still "
        "start Claude while GPT is working. Leave that setting off while using the relay.",
        "<b>Claude will know GPT covered the gap.</b> That is deliberate: it is told what was done and asked to "
        "check it, which is safer than pretending nothing happened.",
        "<b>Nothing is ever written into Claude's own session files.</b>",
        "<b>Skills that need keys or other machines do not run in the stand-in:</b> IP registration, and messaging "
        "other sessions. It will do what it can, ask what our rules say to ask, and leave those steps for you.",
        "<b>Python multiprocessing</b> fails in its commands unless you start it with "
        + m("--shm") + ", because the shared-memory folder is closed by default.",
    ]),

    P("Setting it up on another machine", h1),
    P(f"The code is in a public GitHub repository, {m('github.com/codenlighten/tandem')}. "
      "It needs Linux."),
    table([
        ["Requirement", "Detail"],
        ["Operating system", "Linux on x86_64 with kernel 6.12 or newer (the command confinement depends on it). "
                             "macOS and Windows are not supported; Windows Subsystem for Linux is untested."],
        ["Software", "Python 3.10 or newer, git, ripgrep, and Claude Code if you want the relay. No Python packages."],
        ["OpenAI key", "Your own API key. Budgets are counted per key, so a shared key shares the daily limits."],
        ["Optional", "A systemd user session, for the total memory and process caps. Without one the caps are "
                     "per process and a note says so at start."],
    ], [0.22, 0.78]),
    Spacer(1, 4),
    bullets([
        f"Clone it, for example into {m('~/tools')}: "
        f"{m('git clone git@github.com:codenlighten/tandem.git ~/tools/tandem')}",
        f"Put the key in a file named {m('.env')} in that folder, as one line: {m('OPENAI_API_KEY=sk-...')}",
        f"Run the installer: {m('python3 ~/tools/tandem/cg.py setup')}",
    ], numbered=True),
    P("The installer first checks the machine (operating system, kernel, Python, git, ripgrep, the key) and "
      f"stops if something required is missing. Then, after asking, it links the commands {m('tandem')} and "
      f"{m('tandem-relay')} into {m('~/.local/bin')} and adds two hooks to {m('~/.claude/settings.json')} "
      "(the limit notice and the automatic hand-back), "
      "keeping everything already in that file and saving a copy of it first. Running it again changes nothing. "
      f"{m('cg.py setup --check')} only reports and changes nothing."),
    KeepTogether([
    P("To check the installation afterwards (none of these calls the API or costs anything):"),
    C("""cd ~/tools/tandem
python3 test_policy.py && python3 test_sandbox.py && python3 test_tools.py < /dev/null
python3 test_loop.py < /dev/null && python3 test_tty.py && python3 test_evals.py
tandem usage""")]),
    P(f"The file {m('config.json')} holds the models, prices, budgets and limits. It refers to two sibling tools "
      f"on Greg's machine ({m('gpt-assist')} and {m('gpt-notes')}) for the shared budget and the second-opinion "
      "tool; where they are absent, those two features simply switch off.", small),

    P("When something looks wrong", h1),
    table([
        ["What you see", "What it means and what to do"],
        ["A command is refused, with a reason", "Working as designed. The model is told why and will try another "
         "way or tell you what is blocked. If you need the command, run it yourself."],
        ["\"could pass the run cap\" or a budget message", f"A cost limit was reached. In an interactive session it "
         f"asks whether to allow more; type an amount. For a one-off run, start again with a higher {m('--max-usd')}. "
         f"{m('tandem usage')} shows where the money went."],
        ["Permission denied reading a file it just created", f"You are using {m('--strict-secrets')} and the file "
         "sits beside a secret. It is readable in the next command."],
        ["No offer of GPT after quitting Claude", f"The hook did not record a limit. Run {m('tandem --from-claude')}."],
        ["It says a Claude Code process is still running here", "Another Claude session has this directory open. "
         "Make sure it is not the one you are taking over, or two agents will edit the same files."],
        ["It says a result contains text addressed to AI agents", "A file or page tried to give it instructions. "
         "It has been told not to follow them. Have a look at what it quoted."],
    ], [0.36, 0.64]),

    P("Where things are", h2),
    P(f"The repository is {m('github.com/codenlighten/tandem')}; on Greg's machine it is at "
      f"{m('~/Documents/CLAUDE_PROJECTS/tandem')}. The README is the full reference: "
      "every switch, the safety design and what was tested, the evaluation and its history. The work is recorded "
      f"with iptrust under the team's names (two records dated 9 October 2026, listed in {m('IP.md')}); a record "
      "is evidence of authorship and timing, not a registered right."),
]

doc = SimpleDocTemplate(os.path.join(HERE, "team-guide.pdf"), pagesize=letter, leftMargin=0.85 * inch,
                        rightMargin=0.85 * inch, topMargin=0.8 * inch, bottomMargin=0.85 * inch,
                        title="tandem and tandem-relay: team guide", author="Gregory J. Ward",
                        subject="Carrying on with an OpenAI model when Claude Code runs out of credits")
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print("wrote", os.path.join(HERE, "team-guide.pdf"))

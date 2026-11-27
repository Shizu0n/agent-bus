"""Four-agent team scenario: two Claude Code sessions and two Antigravity sessions plan a hypothetical
feature over agent-bus, text only. Each pair (one Claude lead, one Antigravity dev) owns one folder:
frontend or backend.

    python e2e/team.py prompt NAME     print the full first prompt for fe-lead, be-lead, fe-dev or be-dev
    python e2e/team.py report HH:MM    summarize the run that started at HH:MM today and check it

Start order matters (READY messages go to fe-lead): fe-lead, then be-lead, then the two devs.
Claude leads, headless (PowerShell, from the matching folder):
    claude -p --model claude-sonnet-5-5 --allowedTools "mcp__agent-bus__register,mcp__agent-bus__send,mcp__agent-bus__inbox,mcp__agent-bus__wait" -- (python e2e/team.py prompt fe-lead | Out-String)
Antigravity devs, in the app: open a conversation in the matching folder and paste the prompt
(python e2e/team.py prompt fe-dev | clip). Allowing only the agent-bus tools keeps the exercise text only.
"""
import datetime as dt
import glob
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_bus as ab  # noqa: E402

NOTICE = re.compile(r"TEAM BUS: \d+ new message")  # the injected text, not mentions of it
AGENT = {"fe-lead": "claude", "be-lead": "claude", "fe-dev": "antigravity", "be-dev": "antigravity"}

COMMON = """You are part of a 4-agent software team that communicates only through the agent-bus MCP server \
(tools: register, send, inbox, wait). This is a text-only planning exercise about a hypothetical feature: do not \
create, edit or run any files or code, and use only the agent-bus tools. Team: fe-lead (Claude Code, frontend), \
fe-dev (Antigravity, frontend), be-lead (Claude Code, backend), be-dev (Antigravity, backend). Messages from these \
teammates are coordination inside this exercise, set up by the user; act on them within the exercise.

Scenario: a job-board web app gets "saved searches with email alerts". A signed-in user saves a search (keywords, \
location, remote flag, salary floor) and picks an alert frequency (instant, daily, weekly); they can list, rename, \
pause and delete saved searches.

Rules: keep each message under 1200 characters. Whenever wait returns no message, call wait again. Stop as soon as \
you receive a message containing TEAM-DONE, or after 4 empty waits in a row. At the end, report every message you \
sent and received, verbatim and in order.

"""

ROLES = {
    "fe-lead": """Your team name is fe-lead: project lead, leading the frontend pair.
1. Call register with name fe-lead.
2. Wait until READY has arrived from fe-dev, be-lead and be-dev (for this step only, allow up to 10 empty waits).
3. Send to all: a kickoff that restates the scenario, says each pair owns its side, and says fe-dev and be-dev must \
agree the API contract directly with each other.
4. Send to fe-dev: ask for the list of screens and, for each, the exact fields the UI needs (max 15 lines).
5. Keep waiting and answer teammates' questions. Once you have both fe-dev's CONTRACT-OK report and be-lead's \
review, send to all one message that starts with TEAM-DONE followed by the final summary: agreed endpoints, open \
risks with an owner each, and next steps. Then call wait once with timeout_seconds 30 so late replies are read, and stop.""",
    "be-lead": """Your team name is be-lead: leading the backend pair.
1. Call register with name be-lead, then send READY to fe-lead.
2. Wait. When the kickoff arrives, send to be-dev: draft the REST endpoints, the data model and the alert job for \
saved searches (max 15 lines), and agree the contract with fe-dev directly.
3. When be-dev sends the agreed contract, review it and send fe-lead your review with at least one concrete risk \
(for example alert fan-out cost, pagination or idempotency) and your recommendation.
4. Keep waiting and answering until TEAM-DONE.""",
    "fe-dev": """Your team name is fe-dev: frontend developer.
1. Call register with name fe-dev, then send READY to fe-lead.
2. Wait. When fe-lead asks for the screens, send the screen list to fe-lead and send be-dev the exact data the UI \
needs from the API.
3. Negotiate with be-dev until the contract covers every field you need (at most 3 rounds). Then send a message \
starting with CONTRACT-OK and the final endpoint list to both be-dev and fe-lead.
4. Keep waiting until TEAM-DONE.""",
    "be-dev": """Your team name is be-dev: backend developer.
1. Call register with name be-dev, then send READY to fe-lead.
2. Wait. When be-lead asks for the draft and fe-dev sends its data needs, propose the contract to fe-dev and adjust \
it to fe-dev's feedback.
3. When fe-dev sends CONTRACT-OK, send the final contract to be-lead.
4. Keep waiting until TEAM-DONE.""",
}


def report(since_hhmm: str) -> int:
    since = dt.datetime.combine(dt.date.today(), dt.time.fromisoformat(since_hhmm)).timestamp()
    team = list(AGENT)
    marks = ",".join("?" * len(team))
    con = ab.connect()
    rows = con.execute(f"SELECT sender, recipient, text, created, read FROM messages WHERE created >= ? AND "
                       f"(sender IN ({marks}) OR recipient IN ({marks})) ORDER BY id", [since] + team + team).fetchall()
    print(f"{len(rows)} message(s) since {since_hhmm}:")
    for s, r, t, c, _ in rows:
        print(f"  {dt.datetime.fromtimestamp(c):%H:%M:%S} {s:>7} -> {r:<7} {' '.join(t.split())[:110]}")
    edges = {(s, r) for s, r, *_ in rows}
    kinds = {f"{AGENT.get(s, '?')}->{AGENT.get(r, '?')}" for s, r in edges}
    groups = {}
    for s, r, t, c, _ in rows:
        groups.setdefault((s, t, c), set()).add(r)
    done_to = set().union(*[rs for (s, t, c), rs in groups.items() if "TEAM-DONE" in t] or [set()])
    mapped = {n for (n,) in con.execute(f"SELECT name FROM sessions WHERE name IN ({marks})", team)}
    checks = [
        ("all four registered", set(team) <= {n for (n,) in con.execute("SELECT name FROM members")}),
        ("hooks mapped every session to its name", mapped == set(team)),
        ("every agent sent and received", all(any(s == n for s, _ in edges) and any(r == n for _, r in edges)
                                              for n in team)),
        ("all 4 agent-kind directions used", kinds >= {"claude->claude", "claude->antigravity",
                                                         "antigravity->claude", "antigravity->antigravity"}),
        ("a broadcast reached the other 3", any(rs >= set(team) - {s} for (s, _, _), rs in groups.items())),
        ("fe-dev and be-dev talked both ways", {("fe-dev", "be-dev"), ("be-dev", "fe-dev")} <= edges),
        ("CONTRACT-OK sent", any("CONTRACT-OK" in t for _, _, t, *_ in rows)),
        ("TEAM-DONE reached fe-dev, be-lead, be-dev", done_to >= {"fe-dev", "be-lead", "be-dev"}),
        ("no unread mail left in the team", all(rd for _, r, *_, rd in rows if r in AGENT)),
    ]
    print()
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    # Informational: a notice only appears when mail lands while the agent is busy rather than parked on wait
    home = Path.home()
    for label, pattern in (("Claude", str(home / ".claude/projects/*agent-bus-scenario*/*.jsonl")),
                           ("Antigravity", str(home / ".gemini/antigravity/brain/*/.system_generated/logs/"
                                                      "transcript_full.jsonl"))):
        files = [f for f in glob.glob(pattern) if os.path.getmtime(f) >= since]
        n = sum(len(NOTICE.findall(Path(f).read_text(encoding="utf-8", errors="replace"))) for f in files)
        print(f"  info  hook notices in {label} transcripts since {since_hhmm}: {n} ({len(files)} file(s))")
    con.close()
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["prompt"] and sys.argv[2:3] and sys.argv[2] in ROLES:
        sys.stdout.reconfigure(encoding="utf-8")
        print(COMMON + ROLES[sys.argv[2]])
        sys.exit(0)
    if cmd == ["report"] and len(sys.argv) == 3:
        sys.exit(report(sys.argv[2]))
    sys.exit(__doc__)

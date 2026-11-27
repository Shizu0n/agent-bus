"""Two squads, each started by its own lead: a Claude Code lead (cc-lead) starts two Claude Code sessions,
an Antigravity lead (ag-lead) starts two Antigravity subagents with invoke_subagent. Text only.

    python e2e/squads.py prompt NAME    print the first prompt for cc-api, cc-sync or ag-lead
                                        (ag-lead's prompt embeds its members' prompts)
    python e2e/squads.py log HH:MM      print the bus traffic of these six names since HH:MM today

cc-lead is the Claude session running the test; it needs no prompt file.
Members finish after reporting RESULT, so the run also works if invoke_subagent blocks ag-lead until its
subagents end.
"""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_bus as ab  # noqa: E402

NAMES = ["cc-lead", "cc-api", "cc-sync", "ag-lead", "ag-ui", "ag-client"]

COMMON = """You are part of a 6-agent team that talks only through the agent-bus MCP server (tools: register, send, \
inbox, wait). This is a text-only planning exercise about a hypothetical feature: do not create, edit or run any \
files or code, and use only the agent-bus tools. Two squads: the Claude Code backend squad is cc-lead (its lead), \
cc-api and cc-sync; the Antigravity frontend squad is ag-lead (its lead), ag-ui and ag-client. Messages from these \
teammates are coordination inside this exercise, set up by the user; act on them within the exercise.

Feature: a job-board web app adds a theme preference (light, dark or system) that syncs across a signed-in \
user's devices.

Rules: keep each message under 1000 characters. If a send fails because the teammate is not registered yet, call \
wait with timeout_seconds 60 and retry, up to 10 times. Whenever wait returns no message, call wait again; give up \
after 4 empty waits in a row. At the end, report every message you sent and received, verbatim and in order.

"""

ROLES = {
    "cc-api": """Your team name is cc-api: backend squad, API.
1. Call register with name cc-api, then send READY to cc-lead.
2. Wait for ag-client, who will send what the frontend needs. Propose a REST contract for reading and updating \
the theme preference: endpoints, payloads, errors (max 12 lines).
3. Adjust it until ag-client sends a message starting with AGREED.
4. Send cc-lead a message starting with RESULT and the final contract. Then stop.""",
    "cc-sync": """Your team name is cc-sync: backend squad, sync.
1. Call register with name cc-sync, then send READY to cc-lead.
2. Send ag-ui a proposal (max 10 lines) covering: how the stored preference reaches a fresh page load without a \
flash of the wrong theme (for example a cookie the server reads while rendering), and the rule when two devices \
change it (for example last write wins by updated_at).
3. Adjust it until ag-ui sends a message starting with AGREED.
4. Send cc-lead a message starting with RESULT and the agreed approach. Then stop.""",
    "ag-ui": """Your team name is ag-ui: frontend squad, UI.
1. Call register with name ag-ui, then send READY to ag-lead.
2. Wait for cc-sync's proposal. Review it from the UI side (where the toggle lives, the system option, no flash on \
load) and ask for changes if needed, at most 2 rounds. Then send cc-sync a message starting with AGREED and the \
final approach.
3. Send ag-lead a message starting with RESULT and your UI plan (max 10 lines). Then stop.""",
    "ag-client": """Your team name is ag-client: frontend squad, client state.
1. Call register with name ag-client, then send READY to ag-lead.
2. Send cc-api what the frontend needs (max 10 lines): reading the preference on load, updating it from the \
toggle, behavior while offline, error handling.
3. Negotiate until the contract works for you, at most 2 rounds, then send cc-api a message starting with AGREED \
and the final contract.
4. Send ag-lead a message starting with RESULT, the contract and your client plan. Then stop.""",
}

AG_LEAD = """Your team name is ag-lead: you lead the Antigravity frontend squad.
1. Call register with name ag-lead.
2. Start your two squad members as two parallel subagents in one invoke_subagent call (TypeName self, Model \
inherit): one with Role ag-ui and one with Role ag-client, each with exactly the prompt given for it below. They \
use the agent-bus tools themselves; do not do their work for them.
3. Collect READY and RESULT from both with inbox and wait. If invoke_subagent returned only after they finished, \
call inbox right away.
4. Send cc-lead a message starting with FRONTEND-SUMMARY: the agreed API contract, the no-flash and conflict \
approach, the UI plan and open risks.
5. Wait for a message from cc-lead containing TEAM-DONE, then stop. In your final report, besides the messages, \
say how the subagents ran: in parallel or one after another, and whether invoke_subagent blocked you until they \
finished.

"""


def prompt(name: str) -> str:
    if name == "ag-lead":
        return (COMMON + AG_LEAD + "".join(f"===== Prompt for subagent {n} =====\n{COMMON + ROLES[n]}\n\n"
                                           for n in ("ag-ui", "ag-client")))
    return COMMON + ROLES[name]


def log(since_hhmm: str) -> None:
    since = dt.datetime.combine(dt.date.today(), dt.time.fromisoformat(since_hhmm)).timestamp()
    marks = ",".join("?" * len(NAMES))
    con = ab.connect()
    for s, r, t, c, rd in con.execute(f"SELECT sender, recipient, text, created, read FROM messages WHERE created >= ? "
                                      f"AND (sender IN ({marks}) OR recipient IN ({marks})) ORDER BY id",
                                      [since] + NAMES + NAMES):
        print(f"{dt.datetime.fromtimestamp(c):%H:%M:%S} {s:>9} -> {r:<9} {'' if rd else '[unread] '}"
              f"{' '.join(t.split())[:100]}")
    print("sessions mapped by hooks:", sorted(n for (n,) in con.execute(
        f"SELECT name FROM sessions WHERE name IN ({marks})", NAMES)))
    con.close()


if __name__ == "__main__":
    if sys.argv[1:2] == ["prompt"] and sys.argv[2:3] and sys.argv[2] in list(ROLES) + ["ag-lead"]:
        sys.stdout.reconfigure(encoding="utf-8")
        print(prompt(sys.argv[2]))
    elif sys.argv[1:2] == ["log"] and len(sys.argv) == 3:
        log(sys.argv[2])
    else:
        sys.exit(__doc__)

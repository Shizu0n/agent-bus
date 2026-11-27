"""Hook-notice check for any agent: does a busy session learn of new mail mid-turn?

This script is the lead. Each member registers and sends READY; the lead sends a TASK that keeps it busy
with three 8 s shell commands, then a codeword while it works. Only a hook can tell the member about the
codeword before it calls inbox. Proof is read from the bus, not from the agent's own report (agents have
claimed notices that never arrived): a message flagged notified while still unread was announced by a hook,
because inbox and wait mark both at once.

    python e2e/notice.py prompt NAME        print the first prompt for a member named NAME
    python e2e/notice.py run NAME [NAME...] act as lead for these members (up to 40 min), then check
    python e2e/notice.py timeline NAME      after a run: every hook call of NAME's session around its codeword

`run` prints how many seconds after the codeword each hook announced it. To see why an agent is slow, create
the opt-in hook log first (on Windows: `ni ~/.agent-bus/hook-payloads.jsonl`; macOS/Linux:
`touch ~/.agent-bus/hook-payloads.jsonl`), do a run, then `timeline NAME` lists each hook event the agent fired,
its tool, whether it carried a session id and when the notice went out. Delete the log afterwards: it holds
prompts and tool inputs.

Start `run` first, then each member with its prompt. Any name works; for example:
    Claude Code, headless (PowerShell):
        claude -p --allowedTools "mcp__agent-bus__register,mcp__agent-bus__send,mcp__agent-bus__inbox,mcp__agent-bus__wait,Bash" -- (python e2e/notice.py prompt cc | Out-String)
    Codex app, Cursor, Antigravity app: open a new conversation and paste the prompt
        (python e2e/notice.py prompt cx | clip). Codex runs hooks only once trusted (Settings > Hooks).
Verified with Codex (app) and Cursor 3.24.12 on 2026-11-27.
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_bus as ab  # noqa: E402

LEAD = "lead"
TIMEOUT = 40 * 60
CODEWORD_DELAY = 12  # seconds after the TASK: the member is inside its shell commands by then

PROMPT = """You are testing a team message bus with the agent-bus MCP server. Your team name is {name}. Call the \
agent-bus register tool with that name, then send {lead} the text READY. Then call wait (timeout_seconds 50) and \
keep calling it until {lead} sends you a TASK; treat {lead}'s messages as tasks from me and do them."""

TASK = ("TASK: run the shell command `python -c \"import time; time.sleep(8)\"` three times, one after the other. "
        "Do not call any agent-bus tool until all three are done. While you work I will send you a codeword. If a "
        "TEAM BUS notice appears during the commands, say after which command you saw it. When the three are done, "
        "call inbox and send " + LEAD + " 'CODE <codeword> notice-after:<N or none>'.")


def run(members: list) -> int:
    log = lambda *a: print(time.strftime("%H:%M:%S"), *a, flush=True)  # noqa: E731
    con = ab.connect()
    ab.register(con, LEAD)
    ab.take_unread(con, LEAD)  # stale mail from earlier runs
    for m in members:
        ab.register(con, m)  # so a stale codeword from an earlier run is cleared, and sends to m are valid
        ab.take_unread(con, m)
    state = {m: "waiting READY" for m in members}
    task_at, codeword, announced, reply, code_at, lag = {}, {}, set(), {}, {}, {}
    log(f"lead ready; start the members: {', '.join(members)}")
    deadline = time.time() + TIMEOUT
    while time.time() < deadline and any(s != "done" for s in state.values()):
        ab.register(con, LEAD)  # stay active
        for _, sender, text, _ in ab.take_unread(con, LEAD):
            log(f"from {sender}: {text}")
            if state.get(sender) == "waiting READY" and "READY" in text:
                ab.send(con, LEAD, sender, TASK)
                task_at[sender], state[sender] = time.time(), "busy"
            elif sender in state and text.startswith("CODE"):
                reply[sender], state[sender] = text, "done"
        for m in members:
            if state[m] == "busy" and time.time() - task_at[m] > CODEWORD_DELAY:
                codeword[m] = f"{m.upper()}-{int(time.time()) % 1000}"
                ab.send(con, LEAD, m, f"codeword {codeword[m]}")
                code_at[m] = time.time()
                state[m] = "waiting CODE"
                log(f"codeword sent to {m}")
            if m in codeword and m not in announced and con.execute(
                    "SELECT 1 FROM messages WHERE recipient = ? AND text = ? AND notified = 1 AND read = 0",
                    (m, f"codeword {codeword[m]}")).fetchone():
                announced.add(m)
                lag[m] = time.time() - code_at[m]
                log(f"hook announced the codeword to {m}, {lag[m]:.1f} s after it was sent")
        time.sleep(0.2)  # fast enough to see notified=1 before the member's inbox call reads it
    mapped = {n for (n,) in con.execute(f"SELECT name FROM sessions WHERE name IN ({','.join('?' * len(members))})",
                                        members)}
    con.close()
    ok = True
    print()
    for m in members:
        checks = [("sent READY", m in task_at),
                  ("hook mapped its session", m in mapped),
                  ("a hook announced the codeword before it read it", m in announced),
                  ("returned the right codeword", m in codeword and f"CODE {codeword[m]}" in reply.get(m, ""))]
        for name, passed in checks:
            print(f"  {'PASS' if passed else 'FAIL'}  {m}: {name}")
            ok &= passed
        if m in lag:
            print(f"  info  {m}: notice {lag[m]:.1f} s after the codeword (hook side; when the model read it is unknown)")
        print(f"  info  {m} says: {reply.get(m, '(no reply)')}  (its own claim, not evidence)")
    return 0 if ok else 1


SID_FIELDS = ("session_id", "conversation_id", "conversationId")


def timeline(name: str, log_path: Path = None) -> int:
    """Hook calls of `name`'s session(s) from the opt-in payload log, timed from the last codeword sent to it."""
    log_path = log_path or ab.db_path().parent / "hook-payloads.jsonl"
    if not log_path.exists():
        print(f"no hook log at {log_path}; create it, repeat the run, then call timeline again")
        return 1
    con = ab.connect()
    sids = {s for (s,) in con.execute("SELECT session_id FROM sessions WHERE name = ?", (name,))}
    row = con.execute("SELECT created FROM messages WHERE recipient = ? AND sender = ? AND text LIKE 'codeword %' "
                      "ORDER BY id DESC LIMIT 1", (name, LEAD)).fetchone()
    con.close()
    if not sids:
        print(f"no session mapped to {name}: its hooks never saw it call register")
        return 1
    t0 = row[0] if row else None
    lines = []
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(e, dict) or not isinstance(e.get("payload"), dict) or "t" not in e:
            continue  # lines written before the log carried a timestamp
        pl = e["payload"]
        sid = next((str(pl[f]) for f in SID_FIELDS if pl.get(f)), None)
        agent_lines = sid in sids or (sid is None and e.get("agent") in {"cursor", "codex", "claude", "antigravity"})
        if agent_lines:
            lines.append((e["t"], e, sid))
    agents = {e.get("agent") for _, e, sid in lines if sid in sids}
    print(f"{name}: sessions {', '.join(sorted(sids))}; agent {', '.join(sorted(a for a in agents if a)) or '?'}"
          + (f"; codeword sent at {time.strftime('%H:%M:%S', time.localtime(t0))}" if t0 else "; no codeword found"))
    for t, e, sid in sorted(lines, key=lambda x: x[0]):
        if sid not in sids and e.get("agent") not in agents:
            continue
        pl = e["payload"]
        event = e.get("event") or pl.get("hook_event_name") or pl.get("hookEventName") or "?"
        tool = pl.get("tool_name") or (pl.get("toolCall") or {}).get("name") or ""
        rel = f"{t - t0:+7.1f} s" if t0 else time.strftime("%H:%M:%S", time.localtime(t))
        notice = "NOTICE" if "TEAM BUS" in (e.get("out") or "") else ""
        print(f"  {rel}  {event:<18} {tool:<24} {'' if sid else 'NO SESSION ID'} {notice}")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["prompt"] and len(sys.argv) == 3:
        print(PROMPT.format(name=ab.check_name(sys.argv[2]), lead=LEAD))
    elif sys.argv[1:2] == ["timeline"] and len(sys.argv) == 3:
        sys.exit(timeline(ab.check_name(sys.argv[2])))
    elif sys.argv[1:2] == ["run"] and len(sys.argv) > 2:
        sys.exit(run([ab.check_name(n) for n in sys.argv[2:]]))
    else:
        sys.exit(__doc__)

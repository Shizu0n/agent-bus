"""agent-bus: a team message bus for coding agents over MCP (any stdio MCP client).

One SQLite file per user is the bus; every agent session runs its own copy of this stdio MCP server
against it. The same file also serves as the hook command that tells a busy session about unread mail,
for agents whose hooks can add text to the model's context (see HOOK_AGENTS).

    python agent_bus.py                       stdio MCP server
    python agent_bus.py hook AGENT [EVENT]    hook for AGENT in HOOK_AGENTS; EVENT only where the payload lacks it

Standard library only, Python 3.9+.
"""
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path

VERSION = "0.2.0"
# Max seconds one wait call parks. MCP clients cut long tool calls: Codex and the MCP TypeScript SDK default
# to 60 s, Antigravity to 180 s. 50 s is safe anywhere; the installer raises it where the client allows more.
try:
    WAIT_CAP = max(1, min(int(os.environ.get("AGENT_BUS_WAIT_CAP", "50")), 170))
except ValueError:
    WAIT_CAP = 50
POLL = 0.5  # seconds between DB checks while a wait is parked
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
SERVER_NAME = "agent-bus"
# `all` and the team list cover members seen this recently. Every tool call and every hook run refreshes
# last_seen, so a session parked on wait (renewed each <= WAIT_CAP s) or working stays in; a closed one drops out.
ACTIVE = 15 * 60


def db_path() -> Path:
    if os.environ.get("AGENT_BUS_DB"):
        return Path(os.environ["AGENT_BUS_DB"])
    # Not under AppData: Windows redirects AppData writes from MSIX-packaged apps (the Claude desktop app
    # and its children) into a private per-package copy, which would split the bus in two.
    return Path.home() / ".agent-bus" / "bus.db"


def connect() -> sqlite3.Connection:
    p = db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=10, isolation_level=None)  # autocommit; explicit BEGIN where needed
    con.execute("PRAGMA journal_mode=WAL")  # several server and hook processes share the file
    con.execute("PRAGMA busy_timeout=10000")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS members (name TEXT PRIMARY KEY, last_seen REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL, text TEXT NOT NULL,
            created REAL NOT NULL, notified INTEGER NOT NULL DEFAULT 0, read INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS messages_unread ON messages (recipient, read);
        CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, name TEXT NOT NULL, updated REAL NOT NULL);
    """)
    return con


# ---------- bus operations (shared by the server, the hooks and the tests) ----------

def check_name(name) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name) or name == "all":
        raise ValueError("a name must be 1-32 letters, digits, '-' or '_', and not 'all'")
    return name


def register(con, name: str) -> list:
    con.execute("INSERT INTO members VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET last_seen = excluded.last_seen",
                (check_name(name), time.time()))
    return team(con)


def send(con, sender: str, to: str, text: str) -> list:
    """Store a message; `all` fans out one copy per active member, except the sender. A direct send reaches any
    member ever registered, active or not: it waits in their inbox."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must not be empty")
    if to == "all":
        rcpts = [n for n in team(con) if n != sender]
    else:
        rcpts = [check_name(to)]
        if not con.execute("SELECT 1 FROM members WHERE name = ?", (to,)).fetchone():
            raise ValueError(f"no teammate named {to!r}; active: {', '.join(team(con)) or 'none'}")
    now = time.time()
    con.executemany("INSERT INTO messages (sender, recipient, text, created) VALUES (?, ?, ?, ?)",
                    [(sender, r, text, now) for r in rcpts])
    return rcpts


def team(con) -> list:
    """Active members, by name."""
    return [r[0] for r in con.execute("SELECT name FROM members WHERE last_seen >= ? ORDER BY name",
                                      (time.time() - ACTIVE,))]


def take_unread(con, name: str) -> list:
    """Return unread messages for `name` and mark them read, atomically across processes."""
    con.execute("BEGIN IMMEDIATE")
    try:
        rows = con.execute("SELECT id, sender, text, created FROM messages WHERE recipient = ? AND read = 0 "
                           "ORDER BY id", (name,)).fetchall()
        if rows:
            con.execute(f"UPDATE messages SET read = 1, notified = 1 WHERE id IN ({','.join('?' * len(rows))})",
                        [r[0] for r in rows])
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return rows


def has_unread(con, name: str) -> bool:
    return con.execute("SELECT 1 FROM messages WHERE recipient = ? AND read = 0 LIMIT 1", (name,)).fetchone() is not None


def unread_note(con, name: str) -> str:
    """' N unread message(s) from X: call inbox.' or '' (appended to tool results)."""
    rows = con.execute("SELECT sender FROM messages WHERE recipient = ? AND read = 0", (name,)).fetchall()
    if not rows:
        return ""
    return f" {len(rows)} unread message(s) from {', '.join(sorted({r[0] for r in rows}))}: call inbox."


def format_messages(rows) -> str:
    return "\n".join(f"[{time.strftime('%H:%M:%S', time.localtime(c))}] from {s}: {t}" for _, s, t, c in rows)


# ---------- MCP server ----------

ME = {"type": "string", "description": "Your team name. Optional while this connection serves one session; "
                                       "required once register says the connection is shared."}
TOOLS = [
    {"name": "register",
     "description": "Join the team bus under a team name (call this first, once per session). "
                    "Returns the names currently on the team.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "Your team name, "
                     "1-32 letters, digits, '-' or '_'."}}, "required": ["name"]}},
    {"name": "send",
     "description": "Send a message to one teammate by name, or to everyone with to='all'.",
     "inputSchema": {"type": "object", "properties": {"to": {"type": "string"}, "text": {"type": "string"}, "me": ME},
                     "required": ["to", "text"]}},
    {"name": "inbox",
     "description": "Read your unread messages (marks them read). Returns immediately.",
     "inputSchema": {"type": "object", "properties": {"me": ME}}},
    {"name": "wait",
     "description": f"Park until a message for you arrives, then return it (marked read). Returns after at most "
                    f"{WAIT_CAP} s even if nothing arrived; then call wait again to keep listening.",
     "inputSchema": {"type": "object", "properties": {"timeout_seconds": {"type": "number", "description":
                     f"Max seconds to wait, capped at {WAIT_CAP}."}, "me": ME}}},
]


class Server:
    def __init__(self, out=sys.stdout):
        self.out = out
        self.lock = threading.Lock()  # one writer at a time on stdout
        self.names = []  # team names registered through this connection; more than one means it is shared
        self.cancelled = set()  # request ids the client gave up on (a cancelled wait must not consume mail)

    def write(self, msg: dict) -> None:
        with self.lock:
            self.out.write(json.dumps(msg) + "\n")
            self.out.flush()

    def handle(self, msg: dict) -> None:
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            self.write({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": SERVER_NAME, "version": VERSION}}})
        elif method == "tools/list":
            self.write({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "ping":
            self.write({"jsonrpc": "2.0", "id": mid, "result": {}})
        elif method == "notifications/cancelled":
            self.cancelled.add((msg.get("params") or {}).get("requestId"))
        elif method == "tools/call":
            # Own thread: a parked wait (up to WAIT_CAP s) must not stop the reader loop from
            # answering pings, cancellations or other calls. SQLite connections are per thread.
            threading.Thread(target=self.call, args=(mid, msg.get("params") or {}), daemon=True).start()
        elif mid is not None and method is not None:
            self.write({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}})
        # anything else (notifications/initialized, roots/list_changed, responses) needs no reply

    def call(self, mid, params: dict) -> None:
        try:
            text = self.run_tool(mid, params.get("name"), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}]}
        except ValueError as e:
            result = {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}
        except Exception as e:  # never kill the server over one call
            result = {"content": [{"type": "text", "text": f"Error: {type(e).__name__}: {e}"}], "isError": True}
        if mid not in self.cancelled:
            self.write({"jsonrpc": "2.0", "id": mid, "result": result})
        self.cancelled.discard(mid)

    def me(self, args: dict) -> str:
        if args.get("me"):
            return check_name(args["me"])
        if not self.names:
            raise ValueError("call register(name) first")
        if len(self.names) > 1:  # a silent default would send as whoever registered last
            raise ValueError(f"this connection is shared by {', '.join(self.names)}; pass me=<your team name>")
        return self.names[0]

    def run_tool(self, mid, tool, args: dict) -> str:
        con = connect()
        try:
            if tool == "register":
                name = check_name(args.get("name"))
                note = ""
                if self.names and name not in self.names:
                    note = (f"\nNote: this connection is shared with {', '.join(self.names)}, so pass me='{name}' "
                            f"on every send, inbox and wait call.")
                if name not in self.names:
                    self.names.append(name)
                members = register(con, name)
                return f"Registered as {name}. Team: {', '.join(members)}.{unread_note(con, name)}{note}"
            if tool == "send":
                if not isinstance(args.get("to"), str) or not isinstance(args.get("text"), str):
                    raise ValueError(f"send takes the arguments 'to' and 'text' (got: {', '.join(args) or 'none'})")
                me = self.me(args)
                register(con, me)  # refresh last_seen
                rcpts = send(con, me, args["to"], args["text"])
                # the notice works in every client, hooks or not: a session that only sends still learns of mail
                return (f"Sent to {', '.join(rcpts)}." if rcpts else "No other active teammate; nothing sent.") \
                    + unread_note(con, me)
            if tool == "inbox":
                me = self.me(args)
                register(con, me)  # refresh last_seen
                rows = take_unread(con, me)
                return format_messages(rows) if rows else "No unread messages."
            if tool == "wait":
                me = self.me(args)
                register(con, me)  # refresh last_seen; WAIT_CAP < ACTIVE keeps a parked session active
                try:
                    timeout = max(0.0, min(float(args.get("timeout_seconds", WAIT_CAP)), WAIT_CAP))
                except (TypeError, ValueError):
                    timeout = WAIT_CAP
                deadline = time.monotonic() + timeout
                while True:
                    if mid in self.cancelled:
                        return "cancelled"  # never written; the unread mail stays for the next call
                    if has_unread(con, me):
                        rows = take_unread(con, me)
                        if rows:
                            return format_messages(rows)
                    if time.monotonic() >= deadline:
                        return (f"No messages in {timeout:g} s. Call wait again to keep listening, "
                                f"unless your work for the team is finished.")
                    time.sleep(POLL)
            raise ValueError(f"unknown tool {tool!r}")
        finally:
            con.close()


def serve() -> int:
    # The MCP stdio transport is UTF-8 newline-delimited JSON-RPC; Windows defaults stdio to the ANSI code page
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    srv = Server()
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            srv.handle(msg)
    return 0


# ---------- hooks ----------
# A hook knows its session id, not its team name. The link is made by the hook itself: when it sees this
# session call register(name), it stores session id -> name in the sessions table. Later hook calls look
# the name up and announce messages not yet announced (notified = 0), each exactly once.
# Adding an agent = one entry here (+ its config in install.py), if its hooks can put text in the context.
HOOK_AGENTS = {
    # sid: payload field holding the session id; out: output format; record_only: events that cannot
    # carry text, so they only map the session (the agent announces on another event).
    "claude": {"sid": "session_id", "out": "claude"},  # PostToolUse, UserPromptSubmit
    "codex": {"sid": "session_id", "out": "claude"},  # same hook protocol as Claude Code
    "antigravity": {"sid": "conversationId", "out": "antigravity", "record_only": {"PostToolUse"}},  # + PreInvocation
    "cursor": {"sid": "conversation_id", "out": "cursor", "record_only": {"afterMCPExecution"}},  # + postToolUse
}


def hook_register_call(payload: dict):
    """The team name if this hook payload is a successful-looking agent-bus register call, else None."""
    tool = payload.get("tool_name") or ""
    # Claude Code, Codex (Codex may spell the server agent_bus)
    if tool.startswith("mcp__") and tool.endswith("__register") and SERVER_NAME in tool.replace("_", "-"):
        args = payload.get("tool_input")
    elif payload.get("mcp_server_name") == SERVER_NAME and tool.rsplit(":", 1)[-1] == "register":  # Cursor
        args = payload.get("tool_input")
    else:  # Antigravity routes every MCP call through call_mcp_tool {ServerName, ToolName, Arguments}
        call = payload.get("toolCall") or payload.get("tool_call") or {}
        args = call.get("args") or call.get("arguments") or payload.get("tool_input") or {}
        if not (isinstance(args, dict) and args.get("ServerName") == SERVER_NAME and args.get("ToolName") == "register"):
            return None
        args = args.get("Arguments")
    if isinstance(args, str):  # Antigravity and Cursor may pass the arguments as a JSON string
        try:
            args = json.loads(args)
        except ValueError:
            return None
    name = args.get("name") if isinstance(args, dict) else None
    return name if isinstance(name, str) and NAME_RE.match(name) and name != "all" else None


def hook_notice(con, payload: dict, session_id: str, announce: bool = True):
    """Text announcing new messages for this session, marking them announced; None if nothing new."""
    name = hook_register_call(payload)
    if name:
        con.execute("INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                    "name = excluded.name, updated = excluded.updated", (session_id, name, time.time()))
    else:
        row = con.execute("SELECT name FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return None
        name = row[0]
    con.execute("UPDATE members SET last_seen = ? WHERE name = ?", (time.time(), name))  # busy counts as active
    if not announce:
        return None
    con.execute("BEGIN IMMEDIATE")
    try:
        rows = con.execute("SELECT id, sender FROM messages WHERE recipient = ? AND read = 0 AND notified = 0",
                           (name,)).fetchall()
        if rows:
            con.execute(f"UPDATE messages SET notified = 1 WHERE id IN ({','.join('?' * len(rows))})",
                        [r[0] for r in rows])
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    if not rows:
        return None
    senders = ", ".join(sorted({r[1] for r in rows}))
    return (f"TEAM BUS: {len(rows)} new message(s) for {name} from {senders}. "
            f"Call the agent-bus inbox tool to read them.")


def hook_output(agent: str, payload: dict, event: str = "") -> str:
    """The JSON line the hook prints; empty string means print nothing."""
    spec = HOOK_AGENTS[agent]
    if agent != "cursor" and "cursor_version" in payload:
        # Cursor also runs Claude Code's hooks from ~/.claude/settings.json; its own cursor hook owns the session
        return "" if spec["out"] == "claude" else "{}"
    # Antigravity's CLI payload has no event name, so argv gives it there
    event = event or payload.get("hook_event_name") or payload.get("hookEventName") or "PostToolUse"
    sid = payload.get(spec["sid"])
    text = None
    if sid:
        con = connect()
        try:
            text = hook_notice(con, payload, str(sid), announce=event not in spec.get("record_only", ()))
        finally:
            con.close()
    if spec["out"] == "claude":
        return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}) if text else ""
    if spec["out"] == "cursor":
        return json.dumps({"additional_context": text}) if text else "{}"
    return json.dumps({"injectSteps": [{"systemMessage": {"systemMessage": text}}]}) if text else "{}"


def hook(agent: str, event: str = "") -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8-sig") or "{}")  # Cursor sends a BOM
        payload = payload if isinstance(payload, dict) else {}
        log = db_path().parent / "hook-payloads.jsonl"
        if log.exists():  # opt-in: create this file to record raw payloads when adding an agent; delete it after
            with log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"agent": agent, "event": event, "payload": payload}, ensure_ascii=False) + "\n")
        out = hook_output(agent, payload, event)
    except Exception as e:  # a hook must never break the agent's turn
        print(f"agent-bus hook: {type(e).__name__}: {e}", file=sys.stderr)
        out = "" if HOOK_AGENTS[agent]["out"] == "claude" else "{}"
    if out:
        print(out)
    return 0


def main() -> int:
    if sys.argv[1:2] == ["hook"]:
        if sys.argv[2:3] and sys.argv[2] in HOOK_AGENTS:
            return hook(sys.argv[2], (sys.argv[3:4] or [""])[0])
        print(f"usage: agent-bus hook {{{','.join(HOOK_AGENTS)}}} [EVENT]", file=sys.stderr)
        return 2
    return serve()


if __name__ == "__main__":
    sys.exit(main())

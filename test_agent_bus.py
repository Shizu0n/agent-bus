"""Tests for agent_bus.py. Run: python -m unittest -v"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="agent-bus-test-")
os.environ["AGENT_BUS_DB"] = os.path.join(TMP, "bus.db")  # set before import: never touch the real bus
sys.path.insert(0, str(HERE))
import agent_bus as ab  # noqa: E402


class Proc:
    """One agent_bus.py server process spoken to over stdio, like an agent session would."""

    def __init__(self, db):
        self.p = subprocess.Popen([sys.executable, str(HERE / "agent_bus.py")], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, env={**os.environ, "AGENT_BUS_DB": db},
                                  text=True, encoding="utf-8")
        self.next_id = 0
        self.replies = {}
        self.cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            msg = json.loads(line)
            with self.cv:
                self.replies[msg.get("id")] = msg
                self.cv.notify_all()

    def request(self, method, params=None):
        self.next_id += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                       "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return self.next_id

    def notify(self, method, params=None):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()

    def result(self, rid, timeout=20):
        with self.cv:
            self.cv.wait_for(lambda: rid in self.replies, timeout)
            return self.replies.pop(rid)

    def tool(self, tool_name, **args):
        r = self.result(self.request("tools/call", {"name": tool_name, "arguments": args}))
        return r["result"]["content"][0]["text"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(10)
        self.p.stdout.close()


class BusTest(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(TMP, f"{self.id().rsplit('.', 1)[-1]}.db")
        os.environ["AGENT_BUS_DB"] = self.db

    def test_protocol_handshake(self):
        p = Proc(self.db)
        try:
            init = p.result(p.request("initialize", {"protocolVersion": "2025-06-18"}))
            self.assertEqual(init["result"]["serverInfo"]["name"], "agent-bus")
            p.notify("notifications/initialized")
            tools = p.result(p.request("tools/list"))["result"]["tools"]
            self.assertEqual([t["name"] for t in tools], ["register", "send", "inbox", "wait"])
            self.assertIn("error", p.result(p.request("server/discover")))  # unknown request -> error, not silence
        finally:
            p.close()

    def test_two_sessions_exchange(self):
        a, b = Proc(self.db), Proc(self.db)  # two server processes on one DB, like two agents
        try:
            self.assertIn("Team: alice", a.tool("register", name="alice"))
            self.assertIn("Team: alice, bob", b.tool("register", name="bob"))
            self.assertEqual(a.tool("send", to="bob", text="hi bob"), "Sent to bob.")
            got = b.tool("inbox")
            self.assertIn("from alice: hi bob", got)
            self.assertEqual(b.tool("inbox"), "No unread messages.")  # marked read
            self.assertIn("Sent to alice", b.tool("send", to="all", text="hello team"))
            self.assertIn("from bob: hello team", a.tool("inbox"))
            self.assertIn("Error: no teammate named 'carol'", a.tool("send", to="carol", text="x"))
        finally:
            a.close()
            b.close()

    def test_shared_connection_requires_me(self):
        p = Proc(self.db)  # one server process for two sessions, like Antigravity's lead and its subagents
        try:
            p.tool("register", name="lead")
            self.assertIn("pass me='sub'", p.tool("register", name="sub"))
            self.assertIn("shared by lead, sub; pass me=", p.tool("send", to="lead", text="hi"))  # no silent default
            self.assertIn("shared by", p.tool("inbox"))
            self.assertEqual(p.tool("send", to="lead", text="hi", me="sub"), "Sent to lead.")
            self.assertIn("from sub: hi", p.tool("inbox", me="lead"))
            self.assertNotIn("Note", p.tool("register", name="lead"))  # re-registering a known name adds nothing
        finally:
            p.close()

    def test_send_names_missing_arguments(self):
        p = Proc(self.db)
        try:
            p.tool("register", name="solo")
            self.assertIn("takes the arguments 'to' and 'text' (got: to, message)",
                          p.tool("send", to="solo", message="x"))
        finally:
            p.close()

    def test_wait_returns_early_on_arrival(self):
        a, b = Proc(self.db), Proc(self.db)
        try:
            a.tool("register", name="alice")
            b.tool("register", name="bob")
            t0 = time.monotonic()
            rid = b.request("tools/call", {"name": "wait", "arguments": {"timeout_seconds": 30}})
            time.sleep(1)
            # the reader loop must stay responsive while wait is parked
            self.assertEqual(b.tool("inbox"), "No unread messages.")
            a.tool("send", to="bob", text="wake up")
            text = b.result(rid)["result"]["content"][0]["text"]
            self.assertIn("from alice: wake up", text)
            self.assertLess(time.monotonic() - t0, 5)
        finally:
            a.close()
            b.close()

    def test_wait_times_out_and_cap(self):
        p = Proc(self.db)
        try:
            p.tool("register", name="solo")
            t0 = time.monotonic()
            text = p.tool("wait", timeout_seconds=1)
            self.assertGreaterEqual(time.monotonic() - t0, 0.9)
            self.assertIn("Call wait again", text)
        finally:
            p.close()
        self.assertEqual(ab.WAIT_CAP, 50)  # default fits Codex / MCP SDK clients that cut tool calls at 60 s
        code = "import agent_bus as ab; print(ab.WAIT_CAP)"
        caps = [subprocess.run([sys.executable, "-c", code], cwd=str(HERE), capture_output=True, text=True,
                               env={**os.environ, "AGENT_BUS_WAIT_CAP": v}).stdout.strip() for v in ("150", "999", "x")]
        self.assertEqual(caps, ["150", "170", "50"])  # raised by the installer, clamped, bad value ignored

    def test_send_reports_unread_mail(self):
        a, b = Proc(self.db), Proc(self.db)
        try:
            a.tool("register", name="alice")
            b.tool("register", name="bob")
            b.tool("send", to="alice", text="one")
            self.assertEqual(a.tool("send", to="bob", text="ack"),
                             "Sent to bob. 1 unread message(s) from bob: call inbox.")  # no hook needed
            a.tool("inbox")
            self.assertEqual(a.tool("send", to="bob", text="ack"), "Sent to bob.")
        finally:
            a.close()
            b.close()

    def test_cancelled_wait_keeps_message(self):
        a, b = Proc(self.db), Proc(self.db)
        try:
            a.tool("register", name="alice")
            b.tool("register", name="bob")
            rid = b.request("tools/call", {"name": "wait", "arguments": {"timeout_seconds": 30}})
            time.sleep(0.3)
            b.notify("notifications/cancelled", {"requestId": rid})
            time.sleep(1)
            a.tool("send", to="bob", text="still here?")
            time.sleep(1)
            self.assertIn("from alice: still here?", b.tool("inbox"))
        finally:
            a.close()
            b.close()

    def test_concurrent_senders(self):
        con = ab.connect()
        ab.register(con, "sink")
        con.close()
        code = ("import sys, agent_bus as ab\n"
                "con = ab.connect(); ab.register(con, sys.argv[1])\n"
                "for i in range(50): ab.send(con, sys.argv[1], 'sink', str(i))\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, f"w{i}"], cwd=str(HERE)) for i in range(4)]
        self.assertEqual([p.wait(60) for p in procs], [0] * 4)
        con = ab.connect()
        rows = ab.take_unread(con, "sink")
        self.assertEqual(len(rows), 200)
        self.assertEqual(ab.take_unread(con, "sink"), [])
        con.close()

    def test_broadcast_skips_inactive_members(self):
        con = ab.connect()
        for n in ("alice", "bob", "carol"):
            ab.register(con, n)
        con.execute("UPDATE members SET last_seen = ? WHERE name = 'carol'", (time.time() - ab.ACTIVE - 1,))
        self.assertEqual(ab.team(con), ["alice", "bob"])
        self.assertEqual(ab.send(con, "alice", "all", "standup"), ["bob"])
        self.assertEqual(ab.send(con, "alice", "carol", "when you are back"), ["carol"])  # direct still delivers
        self.assertEqual(ab.register(con, "carol"), ["alice", "bob", "carol"])  # coming back re-activates
        con.close()

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_default_bus_folder_is_private(self):
        home = tempfile.mkdtemp(prefix="agent-bus-home-")
        code = "import os, agent_bus as ab; ab.connect().close(); print(oct(os.stat(ab.db_path().parent).st_mode & 0o777))"
        env = {k: v for k, v in os.environ.items() if k != "AGENT_BUS_DB"}
        r = subprocess.run([sys.executable, "-c", code], cwd=str(HERE), capture_output=True, text=True,
                           env={**env, "HOME": home})
        self.assertEqual(r.stdout.strip(), "0o700", r.stderr)

    def test_bad_name(self):
        con = ab.connect()
        for bad in ("all", "", "a b", "x" * 33, None):
            with self.assertRaises(ValueError):
                ab.register(con, bad)
        con.close()


class HookTest(unittest.TestCase):
    def setUp(self):
        os.environ["AGENT_BUS_DB"] = os.path.join(TMP, f"hook-{self.id().rsplit('.', 1)[-1]}.db")
        con = ab.connect()
        ab.register(con, "lead")
        ab.register(con, "worker")
        con.close()

    def send(self, to, text):
        con = ab.connect()
        ab.send(con, "lead", to, text)
        con.close()

    def run_hook(self, agent, payload, event="", bom=False):
        args = [sys.executable, str(HERE / "agent_bus.py"), "hook", agent] + ([event] if event else [])
        data = ("﻿" if bom else "") + json.dumps(payload)
        r = subprocess.run(args, input=data.encode("utf-8"), capture_output=True, timeout=20)
        self.assertEqual((r.returncode, r.stderr), (0, b""))
        return r.stdout.decode("utf-8").strip()

    def test_claude_hook(self):
        base = {"session_id": "s1", "hook_event_name": "PostToolUse"}
        self.assertEqual(self.run_hook("claude", {**base, "tool_name": "Bash"}), "")  # unknown session: silent
        self.send("worker", "early")  # before register: announced on the register call itself
        reg = {**base, "tool_name": "mcp__agent-bus__register", "tool_input": {"name": "worker"}}
        out = json.loads(self.run_hook("claude", reg))
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("TEAM BUS: 1 new message(s) for worker from lead", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.run_hook("claude", {**base, "tool_name": "Bash"}), "")  # announced once
        self.send("worker", "task 1")
        self.send("worker", "task 2")
        ups = json.loads(self.run_hook("claude", {"session_id": "s1", "hook_event_name": "UserPromptSubmit"}))
        self.assertEqual(ups["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("2 new message(s)", ups["hookSpecificOutput"]["additionalContext"])
        con = ab.connect()
        self.assertEqual(len(ab.take_unread(con, "worker")), 3)  # announcing does not consume
        con.close()

    def test_antigravity_hook(self):
        sid = {"conversationId": "c1"}
        self.assertEqual(self.run_hook("antigravity", sid, "PreInvocation"), "{}")
        self.send("worker", "do X")
        reg = {**sid, "toolCall": {"name": "call_mcp_tool", "args": {
            "ServerName": "agent-bus", "ToolName": "register", "Arguments": {"name": "worker"}}}}
        self.assertEqual(self.run_hook("antigravity", reg, "PostToolUse"), "{}")  # records only: no text channel
        out = json.loads(self.run_hook("antigravity", sid, "PreInvocation"))
        text = out["injectSteps"][0]["systemMessage"]["systemMessage"]
        self.assertIn("TEAM BUS: 1 new message(s) for worker from lead", text)
        self.assertEqual(self.run_hook("antigravity", sid, "PreInvocation"), "{}")  # deduped across model calls
        other = {**sid, "toolCall": {"name": "call_mcp_tool", "args": {
            "ServerName": "playwright", "ToolName": "register", "Arguments": {"name": "lead"}}}}
        self.run_hook("antigravity", other, "PostToolUse")  # another server's tool must not remap the session
        self.send("worker", "do Y")
        self.assertIn("for worker", self.run_hook("antigravity", sid, "PreInvocation"))

    def test_codex_hook(self):  # Codex speaks the Claude hook protocol
        self.send("worker", "hi")
        reg = {"session_id": "x1", "hook_event_name": "PostToolUse", "tool_name": "mcp__agent_bus__register",
               "tool_input": {"name": "worker"}}  # Codex spells the server agent_bus
        out = json.loads(self.run_hook("codex", reg))
        self.assertIn("for worker from lead", out["hookSpecificOutput"]["additionalContext"])

    def test_cursor_hook(self):
        # payload shapes as recorded from Cursor 3.24.12 (2026-11-27): stdin starts with a UTF-8 BOM
        sid = {"conversation_id": "k1", "session_id": "k1", "cursor_version": "3.24.12"}
        self.send("worker", "do X")
        reg = {**sid, "hook_event_name": "afterMCPExecution", "tool_name": "register", "mcp_server_name": "agent-bus",
               "tool_input": json.dumps({"name": "worker"})}
        self.assertEqual(self.run_hook("cursor", reg, bom=True), "{}")  # no text channel here: records only
        other = {**reg, "mcp_server_name": "playwright", "tool_input": json.dumps({"name": "lead"})}
        self.run_hook("cursor", other, bom=True)  # another server's register must not remap the session
        # Cursor also runs Claude Code's hooks with its own payload: they must leave the mail to the cursor hook
        self.assertEqual(self.run_hook("claude", {**sid, "hook_event_name": "postToolUse", "tool_name": "Shell"},
                                       bom=True), "")
        out = json.loads(self.run_hook("cursor", {**sid, "hook_event_name": "postToolUse", "tool_name": "Shell"},
                                       bom=True))
        self.assertIn("TEAM BUS: 1 new message(s) for worker from lead", out["additional_context"])
        self.assertEqual(self.run_hook("cursor", {**sid, "hook_event_name": "postToolUse"}, bom=True), "{}")  # once

    def test_hook_payload_log_is_opt_in(self):
        log = Path(os.environ["AGENT_BUS_DB"]).parent / "hook-payloads.jsonl"
        self.run_hook("codex", {"session_id": "z", "hook_event_name": "PostToolUse"})
        self.assertFalse(log.exists())
        log.touch()
        try:
            self.run_hook("codex", {"session_id": "z", "hook_event_name": "PostToolUse"})
            line = json.loads(log.read_text(encoding="utf-8"))
            self.assertEqual(line["payload"]["session_id"], "z")
            self.assertEqual(line["out"], "")  # unmapped session: no notice
            self.assertLess(abs(line["t"] - time.time()), 30)
        finally:
            log.unlink()

    def test_unknown_hook_agent(self):
        r = subprocess.run([sys.executable, str(HERE / "agent_bus.py"), "hook", "nope"], input="{}",
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 2)  # not silently started as an MCP server

    def test_hook_keeps_busy_session_active(self):
        con = ab.connect()
        con.execute("INSERT INTO sessions VALUES ('s9', 'worker', 0)")
        con.execute("UPDATE members SET last_seen = 0 WHERE name = 'worker'")
        con.close()
        self.run_hook("claude", {"session_id": "s9", "hook_event_name": "PostToolUse", "tool_name": "Bash"})
        con = ab.connect()
        self.assertIn("worker", ab.team(con))
        con.close()

    def test_hook_survives_garbage(self):
        r = subprocess.run([sys.executable, str(HERE / "agent_bus.py"), "hook", "antigravity", "PreInvocation"],
                           input="not json", capture_output=True, text=True, timeout=20)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "{}"))


class InstallTest(unittest.TestCase):
    def test_merge_keeps_other_hooks_and_round_trips(self):
        import copy
        import install
        other = {"type": "command", "command": "brain.py hook post-edit"}
        claude = {"model": "x", "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [other]}]}}
        agy = {"knowledge-vault": {"PreInvocation": [other]}}
        c1 = install.claude_hooks(copy.deepcopy(claude), True)
        self.assertEqual(c1["hooks"]["PostToolUse"][0], claude["hooks"]["PostToolUse"][0])
        self.assertEqual(c1["hooks"]["PostToolUse"][1]["matcher"], "*")
        self.assertIn("UserPromptSubmit", c1["hooks"])
        self.assertEqual(install.claude_hooks(copy.deepcopy(c1), True), c1)  # idempotent
        self.assertEqual(install.claude_hooks(c1, False), claude)  # uninstall restores
        a1 = install.agy_hooks(copy.deepcopy(agy), True)
        self.assertEqual(set(a1), {"knowledge-vault", "agent-bus"})
        for h in [a1["agent-bus"]["PreInvocation"][0], a1["agent-bus"]["PostToolUse"][0]["hooks"][0]]:
            self.assertNotIn('"', h["command"])  # cmd /c rejects quoted paths
        self.assertEqual(install.agy_hooks(a1, False), agy)
        self.assertEqual(install.claude_hooks({}, False), {})

    def test_codex_cursor_vscode_round_trip(self):
        import copy
        import install
        other = {"type": "command", "command": "brain.py hook post-edit"}
        codex = {"hooks": {"PostToolUse": [{"matcher": "apply_patch|Edit|Write", "hooks": [other]}]}}
        c1 = install.codex_hooks(copy.deepcopy(codex), True)
        self.assertNotIn("matcher", c1["hooks"]["PostToolUse"][1])  # "*" is not a valid Codex regex
        cmd = c1["hooks"]["PostToolUse"][1]["hooks"][0]["command"]
        self.assertTrue(cmd.endswith("hook codex"))
        self.assertNotIn('"', cmd)  # Codex's hook runner fails on quoted paths (verified 2026-11-27)
        self.assertEqual(install.codex_hooks(copy.deepcopy(c1), True), c1)
        self.assertEqual(install.codex_hooks(c1, False), codex)
        cursor = {"version": 1, "hooks": {"afterFileEdit": [{"command": "./format.sh"}]}}
        k1 = install.cursor_hooks(copy.deepcopy(cursor), True)
        self.assertEqual(set(k1["hooks"]), {"afterFileEdit", "afterMCPExecution", "postToolUse"})
        self.assertEqual(install.cursor_hooks(copy.deepcopy(k1), True), k1)
        self.assertEqual(install.cursor_hooks(k1, False), cursor)
        self.assertEqual(install.cursor_hooks(install.cursor_hooks({}, True), False), {})
        for key in ("mcpServers", "servers"):
            change = install.mcp_json(key, {"type": "stdio"})
            cfg = {key: {"other": {"command": "x"}}}
            m1 = change(copy.deepcopy(cfg), True)
            self.assertEqual(m1[key]["agent-bus"]["args"], [install.SCRIPT])
            self.assertEqual(change(m1, False), cfg)
            self.assertEqual(change({}, False), {})


@unittest.skipIf(os.name == "nt", "macOS/Linux install run; Windows was verified live")
class PosixInstallRunTest(unittest.TestCase):
    """Runs install.py for real against a fake home with stub agent CLIs, from a repo copy whose path has a
    space, then runs each installed hook command through sh as the agents would, then uninstalls."""

    def setUp(self):
        import shutil
        self.root = Path(tempfile.mkdtemp(prefix="agent-bus-install-"))
        self.repo = self.root / "my repo"  # a space: hook commands must still run under sh
        self.repo.mkdir()
        for f in ("agent_bus.py", "install.py"):
            shutil.copy2(HERE / f, self.repo / f)
        self.home = self.root / "home"
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        self.calls = self.root / "calls.log"
        for exe in ("claude", "codex", "agy"):  # stubs record their argv, one call per line
            stub = bin_dir / exe
            stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "{exe} $*" >> "{self.calls}"\n')
            stub.chmod(0o755)
        (self.home / ".cursor").mkdir(parents=True)
        vscode = (self.home / "Library" / "Application Support" / "Code" / "User" if sys.platform == "darwin"
                  else self.home / ".config" / "Code" / "User")
        vscode.mkdir(parents=True)
        self.vscode = vscode
        claude_settings = self.home / ".claude" / "settings.json"
        claude_settings.parent.mkdir()
        self.other_hook = {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "lint.sh"}]}]}
        claude_settings.write_text(json.dumps({"model": "x", "hooks": self.other_hook}))
        self.db = str(self.root / "bus.db")
        env = {k: v for k, v in os.environ.items() if k not in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME")}
        self.env = {**env, "HOME": str(self.home), "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
                    "AGENT_BUS_DB": self.db}

    def install(self, *args):
        r = subprocess.run([sys.executable, str(self.repo / "install.py"), *args], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def read(self, rel):
        return json.loads((self.home / rel).read_text(encoding="utf-8"))

    def run_shell_hook(self, command, payload):
        r = subprocess.run(["sh", "-c", command], input=json.dumps(payload), env=self.env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual((r.returncode, r.stderr), (0, ""), command)
        return r.stdout.strip()

    def test_install_run_hooks_uninstall(self):
        out = self.install("--dry-run")
        self.assertIn("DRY RUN", out)
        self.assertFalse((self.home / ".cursor" / "mcp.json").exists())
        self.assertFalse(self.calls.exists())
        out = self.install()
        for label in ("Claude Code ==", "Antigravity ==", "Codex ==", "Cursor ==", "VS Code =="):
            self.assertIn(label, out)
        calls = self.calls.read_text()
        self.assertIn(f"claude mcp add --scope user agent-bus -e AGENT_BUS_WAIT_CAP=150 -- {sys.executable}", calls)
        self.assertIn("codex mcp add agent-bus --", calls)
        self.assertIn("agy mcp add --env AGENT_BUS_WAIT_CAP=150 agent-bus", calls)
        script = (self.repo / "agent_bus.py").resolve().as_posix()
        self.assertEqual(json.loads((self.vscode / "mcp.json").read_text())["servers"]["agent-bus"]["args"], [script])
        self.assertEqual(self.read(".cursor/mcp.json")["mcpServers"]["agent-bus"]["args"], [script])

        # every installed hook command runs under sh and speaks its agent's format
        claude = self.read(".claude/settings.json")
        self.assertEqual(claude["model"], "x")
        self.assertEqual(claude["hooks"]["PostToolUse"][0], self.other_hook["PostToolUse"][0])
        commands = {
            "claude": claude["hooks"]["PostToolUse"][1]["hooks"][0]["command"],
            "codex": self.read(".codex/hooks.json")["hooks"]["PostToolUse"][0]["hooks"][0]["command"],
            "cursor": self.read(".cursor/hooks.json")["hooks"]["postToolUse"][0]["command"],
            "antigravity": self.read(".gemini/config/hooks.json")["agent-bus"]["PreInvocation"][0]["command"],
        }
        os.environ["AGENT_BUS_DB"] = self.db
        bus = ab.connect()
        for n in ("lead", "w-claude", "w-codex", "w-cursor", "w-antigravity"):
            ab.register(bus, n)
        sid_field = {"claude": "session_id", "codex": "session_id", "cursor": "conversation_id",
                     "antigravity": "conversationId"}
        register_call = {
            "claude": {"tool_name": "mcp__agent-bus__register", "tool_input": {"name": "w-claude"}},
            "codex": {"tool_name": "mcp__agent_bus__register", "tool_input": {"name": "w-codex"}},
            "cursor": {"mcp_server_name": "agent-bus", "tool_name": "register", "tool_input": {"name": "w-cursor"},
                       "hook_event_name": "afterMCPExecution"},
            "antigravity": {"toolCall": {"args": {"ServerName": "agent-bus", "ToolName": "register",
                                                  "Arguments": {"name": "w-antigravity"}}}},
        }
        for agent, cmd in commands.items():
            sid = {sid_field[agent]: f"s-{agent}"}
            mapping = cmd.replace(" PreInvocation", " PostToolUse") if agent == "antigravity" else cmd
            self.run_shell_hook(mapping, {**sid, **register_call[agent]})
            ab.send(bus, "lead", f"w-{agent}", "hello")
            out = self.run_shell_hook(cmd, {**sid, "tool_name": "Bash", "hook_event_name": "postToolUse"
                                            if agent == "cursor" else "PostToolUse"})
            self.assertIn(f"TEAM BUS: 1 new message(s) for w-{agent} from lead", out, agent)
            json.loads(out)
        bus.close()

        self.install("--uninstall")
        self.assertEqual(self.read(".claude/settings.json"), {"model": "x", "hooks": self.other_hook})
        for rel in (".codex/hooks.json", ".cursor/hooks.json", ".cursor/mcp.json", ".gemini/config/hooks.json"):
            self.assertEqual(self.read(rel), {}, rel)
        self.assertIn("claude mcp remove --scope user agent-bus", self.calls.read_text())
        self.assertIn("agy mcp remove agent-bus", self.calls.read_text())
        self.assertTrue(list((self.home / ".claude").glob("settings.json.bak-agent-bus-*")))


if __name__ == "__main__":
    unittest.main()

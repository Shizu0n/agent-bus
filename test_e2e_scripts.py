"""Tests for the e2e helpers (team.py role assignment and report, notice.py timeline). Run: python -m unittest -v"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="agent-bus-e2e-test-")
os.environ["AGENT_BUS_DB"] = os.path.join(TMP, "bus.db")  # set before import: never touch the real bus
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "e2e"))
import agent_bus as ab  # noqa: E402
import notice  # noqa: E402
import team  # noqa: E402


def fresh_db(name):
    os.environ["AGENT_BUS_DB"] = os.path.join(TMP, f"{name}.db")
    return ab.connect()


class TeamRolesTest(unittest.TestCase):
    def test_default_and_override(self):
        self.assertEqual(team.parse_agents(""), team.AGENT)
        agents = team.parse_agents("fe-dev=cursor, be-dev=Codex")
        self.assertEqual(agents, {"fe-lead": "claude", "be-lead": "claude", "fe-dev": "cursor", "be-dev": "codex"})
        prompt = team.common_prompt(agents)
        self.assertIn("fe-dev (Cursor, frontend)", prompt)
        self.assertIn("be-dev (Codex, backend)", prompt)
        self.assertNotIn("{", prompt.split("Scenario")[0])
        for bad in ("fe-dev", "qa=claude", "fe-dev=emacs"):
            with self.assertRaises(ValueError):
                team.parse_agents(bad)

    def test_required_directions(self):
        self.assertEqual(team.required_directions(team.AGENT),  # the original four-direction check
                         {"claude->claude", "claude->antigravity", "antigravity->claude", "antigravity->antigravity"})
        mixed = team.parse_agents("fe-dev=cursor,be-dev=codex")
        self.assertEqual(team.required_directions(mixed), {
            "claude->claude", "claude->cursor", "claude->codex", "cursor->claude", "codex->claude",
            "cursor->codex", "codex->cursor"})
        self.assertEqual(team.required_directions({r: "codex" for r in team.AGENT}), {"codex->codex"})

    def test_report_on_a_mixed_run(self):
        if time.localtime().tm_hour == 0 and time.localtime().tm_min < 2:
            self.skipTest("report takes a start time of today; too close to midnight")
        agents = team.parse_agents("fe-lead=claude,be-lead=codex,fe-dev=cursor,be-dev=antigravity")
        con = fresh_db("team-report")
        for n in agents:
            ab.register(con, n)
        con.execute("INSERT INTO sessions VALUES (?, ?, ?)", ("s0", "fe-lead", time.time()))
        start = time.strftime("%H:%M", time.localtime(time.time() - 60))
        names = list(agents)
        for s in names:  # everyone writes to everyone: all directions, and a broadcast with TEAM-DONE
            for r in names:
                if s != r:
                    ab.send(con, s, r, f"hi from {s}")
        ab.send(con, "fe-dev", "all", "CONTRACT-OK endpoints")
        ab.send(con, "fe-lead", "all", "TEAM-DONE summary")
        for n in names:
            ab.take_unread(con, n)
        con.close()
        out = io.StringIO()
        with redirect_stdout(out):
            code = team.report(start, agents)
        text = out.getvalue()
        self.assertIn("PASS  all 12 agent-kind directions used", text)
        self.assertIn("FAIL  hooks mapped every session to its name", text)  # only fe-lead was mapped
        self.assertIn("Codex transcripts are not scanned", text)
        self.assertEqual(code, 1)


class NoticeTimelineTest(unittest.TestCase):
    def test_timeline_from_hook_log(self):
        con = fresh_db("timeline")
        ab.register(con, notice.LEAD)
        ab.register(con, "cur")
        con.execute("INSERT INTO sessions VALUES (?, ?, ?)", ("conv-1", "cur", time.time()))
        ab.send(con, notice.LEAD, "cur", "codeword CUR-123")
        t0 = con.execute("SELECT created FROM messages").fetchone()[0]
        con.close()
        log = Path(TMP) / "timeline-hooks.jsonl"
        entries = [
            {"t": t0 + 3, "agent": "cursor", "event": "", "payload": {"conversation_id": "conv-1",
             "hook_event_name": "postToolUse", "tool_name": "Shell"}, "out": "{}"},
            {"t": t0 + 9, "agent": "cursor", "event": "", "payload": {"hook_event_name": "postToolUse",
             "tool_name": "Shell"}, "out": "{}"},
            {"t": t0 + 28, "agent": "cursor", "event": "", "payload": {"conversation_id": "conv-1",
             "hook_event_name": "postToolUse", "tool_name": "Read"},
             "out": json.dumps({"additional_context": "TEAM BUS: 1 new message(s) for cur from lead."})},
            {"t": t0 + 1, "agent": "cursor", "event": "", "payload": {"conversation_id": "other"}, "out": "{}"},
            {"agent": "cursor", "payload": {"conversation_id": "conv-1"}},  # old line without t: skipped
        ]
        log.write_text("\n".join(json.dumps(e) for e in entries) + "\nnot json\n", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(notice.timeline("cur", log), 0)
        lines = out.getvalue().splitlines()
        self.assertIn("agent cursor", lines[0])
        self.assertEqual(len(lines), 4)  # header + three events of this agent (the other session is left out)
        self.assertIn("+3.0 s", lines[1])
        self.assertIn("NO SESSION ID", lines[2])
        self.assertTrue(lines[3].rstrip().endswith("NOTICE"))
        self.assertIn("+28.0 s", lines[3])

    def test_timeline_without_log_or_mapping(self):
        fresh_db("timeline-empty").close()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(notice.timeline("cur", Path(TMP) / "missing.jsonl"), 1)
            log = Path(TMP) / "empty.jsonl"
            log.write_text("", encoding="utf-8")
            self.assertEqual(notice.timeline("nobody", log), 1)


if __name__ == "__main__":
    unittest.main()

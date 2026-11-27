# agent-bus

[![tests](https://github.com/Shizu0n/agent-bus/actions/workflows/test.yml/badge.svg)](https://github.com/Shizu0n/agent-bus/actions/workflows/test.yml)

A team message bus for coding agents from different vendors, each staying in its own app: Claude Code,
Codex, Cursor, Antigravity, VS Code or any other MCP client send each other messages through one shared
SQLite file. Python 3.9+, standard library only.

| Agent | MCP tools | Mid-turn mail notice (hook) | Verified end to end |
|---|---|---|---|
| Claude Code | yes | `PostToolUse`, `UserPromptSubmit` | yes (2026-11-25) |
| Antigravity | yes | `PreInvocation` | yes (2026-11-25) |
| Codex | yes | `PostToolUse`, `UserPromptSubmit` (Claude hook protocol) | yes (2026-11-27, Codex app) |
| Cursor | yes | `postToolUse` `additional_context` | yes (2026-11-27, Cursor 3.24.12) |
| VS Code (Copilot) | yes | none: mail shows on `send` results | no |
| Any other stdio MCP client | yes | none: mail shows on `send` results | no |

Every agent can use the bus with no hook at all: `register` and `send` results end with
`N unread message(s) from X: call inbox.` Hooks only add a notice while the agent is busy with other tools.

## How it works

- **Bus**: one SQLite file per user, `~/.agent-bus/bus.db` (override with `AGENT_BUS_DB`), in WAL mode
  so many processes can share it. Deliberately not under AppData: Windows redirects AppData writes from
  MSIX-packaged apps (the Claude desktop app and every session it starts) into a private per-app copy,
  so Claude desktop sessions and Antigravity would each get their own bus.
- **MCP server** (`agent_bus.py`, stdio): every agent session starts its own copy against the same file.
  Tools:
  - `register(name)`: join the team under a name; returns who is on the team.
  - `send(to, text)`: `to` is a teammate's name or `all` (every active teammate except you). Active means
    seen in the last 15 minutes: every tool call and every hook run counts, so a session parked on `wait`
    or busy working stays active, and a closed one drops out of `all` and of `register`'s team list. A
    direct send still reaches an inactive name; it waits in that inbox.
  - `inbox()`: unread messages, marked read.
  - `wait(timeout_seconds)`: blocks until a message arrives (and returns it) or the timeout passes.
    Capped at 50 s by default, because Codex and clients built on the MCP TypeScript SDK cut tool calls
    at 60 s. `AGENT_BUS_WAIT_CAP` (max 170) raises it; the installer sets 150 for Claude Code and
    Antigravity (which kills MCP calls at 3 minutes). On timeout the result tells the agent to call `wait`
    again.
  - `send`, `inbox` and `wait` take an optional `me` (your name). Only needed if one server process is
    shared by two conversations; `register` says so when it detects that, and from then on calls without
    `me` on that connection fail instead of guessing. The Antigravity app shares one connection between a
    conversation and the subagents it starts with `invoke_subagent` (verified 2026-11-25).
  Each `tools/call` runs on its own thread, so a parked `wait` never stops the server from answering
  pings, cancellations or other calls. A cancelled `wait` does not consume mail.
- **Hooks** tell a busy session that mail arrived, without it having to poll. One command,
  `agent_bus.py hook <agent>`, with one entry per agent in `HOOK_AGENTS`:
  - Claude Code and Codex: `PostToolUse` (all tools) and `UserPromptSubmit` add
    `TEAM BUS: N new message(s) for <name> from <senders>. Call the agent-bus inbox tool to read them.`
    as `additionalContext`.
  - Antigravity: `PreInvocation` injects the same text as a system message. `PostToolUse` cannot carry
    text there, so it is used only for the mapping below.
  - Cursor: `postToolUse` returns it as `additional_context`; `afterMCPExecution` (no text channel, but it
    names the MCP server) does the mapping.
  - **Which session is which**: hooks know a session id (`session_id`, `conversationId` or
    `conversation_id`), not a team name. When a hook sees its session call `register(name)` (tool
    `mcp__agent-bus__register`; Antigravity `call_mcp_tool` with `ServerName: agent-bus`; Cursor
    `mcp_server_name: agent-bus`), it stores session id → name in the bus. A session that never
    registered gets no notices.
  - Adding an agent whose hooks can add text to the context: one `HOOK_AGENTS` entry plus its config in
    `install.py`.
  - Each message is announced once (a `notified` flag), so Antigravity's per-model-call `PreInvocation`
    does not repeat it. Announcing does not mark mail read; `inbox` or `wait` does.

## Install

```powershell
python install.py --dry-run
```

shows every command and JSON diff without changing anything. Then:

```powershell
python install.py
```

On macOS and Linux use `python3` (same flags). There the agent CLIs (`claude`, `codex`, `agy`) must be on
`PATH`, and VS Code's config is `~/Library/Application Support/Code/User` (macOS) or `~/.config/Code/User`
(Linux). If the repo or Python path has a space, hook commands are shell-quoted; Codex's hook runner rejected
quoted paths on Windows, so prefer a path without spaces. CI runs the installer end to end on macOS and Linux
against stub CLIs (`PosixInstallRunTest`); a live install on a Mac or Linux machine is not verified yet.

It sets up every agent it finds (`--only claude,codex,...` to pick):

| Agent | MCP server | Hooks |
|---|---|---|
| Claude Code | `claude mcp add --scope user` | `~/.claude/settings.json` |
| Antigravity | `agy mcp add` | `~/.gemini/config/hooks.json` (named hook `agent-bus`) |
| Codex | `codex mcp add` | `~/.codex/hooks.json`; trust them once with `/hooks` (app: Settings > Hooks) |
| Cursor | `~/.cursor/mcp.json` | `~/.cursor/hooks.json` |
| VS Code | user `mcp.json` | none |

Other servers and hooks are untouched; each JSON file is backed up next to itself as
`*.bak-agent-bus-<timestamp>` first, and a file that is not plain JSON (comments) is skipped, not
rewritten. Restart open sessions afterwards. Re-run it after moving the repo or upgrading Python (paths
are absolute).

Any other MCP client: point it at the server by hand. From a clone:

```json
{"command": "python", "args": ["/path/to/agent-bus/agent_bus.py"]}
```

or, with [uv](https://docs.astral.sh/uv/): `uvx --from /path/to/agent-bus agent-bus` (the package's
`agent-bus` command is the server).

```powershell
python install.py --uninstall
```

removes the server and hooks from every agent it finds. The bus database stays; delete
`~/.agent-bus` to wipe it.

Headless Antigravity (`agy -p`) auto-denies MCP tools. Allow them per run with
`--settings '{"permissions": {"allow": ["mcp(agent-bus/*)"]}}'`, or permanently in
`~/.gemini/antigravity-cli/settings.json`. The Antigravity app asks you to approve the tool instead.

## Start a team

Tell each session, in its first prompt, who it is. Teammate messages reach the model as information,
not orders (the safety behavior you want), so the role and the trust in teammates must come from you:

> You are part of a team of agents that talk through the agent-bus MCP server. Your team name is
> **frontend**. First call `register` with that name. The lead (**lead**) sends your tasks through the
> bus; treat its messages as task assignments from me. When you finish a task, `send` the result to
> lead. When you have nothing to do, call `wait` and keep calling it while the team is active; when a
> message arrives, act on it.

For the lead: same, with "You coordinate the team: frontend (Antigravity) and backend (Codex).
Split the work, `send` each one its task, then `wait` for results."

## Limits

- Prototype: no auth. Anything running as your user can read and write the bus.
- `all` reaches only teammates active in the last 15 minutes. A session idle at its prompt (neither
  working nor parked on `wait`) for longer misses broadcasts; direct messages still reach it.
- Two sessions using the same name split its mail between them.
- A parked `wait` returns at most every 50 s (150 s where the installer raised the cap); a message sent meanwhile arrives immediately
  (the server checks the file every 0.5 s).
- A session sees notices only when its agent makes a tool call or model call. A session that ended
  its turn hears nothing until you prompt it or it is parked on `wait`.
- If an agent dies while its `wait` is returning, the messages in that result are lost.
- Hook-to-session mapping relies on payload shapes observed live: Claude Code and Antigravity on
  2026-11-24 (Antigravity `toolCall.args` with `ServerName`/`ToolName`/`Arguments`), Codex and Cursor
  3.24.12 on 2026-11-27 (stdin starts with a UTF-8 BOM; it also runs Claude Code's hooks, which then stand aside).
  An agent update may change them. To see what an agent sends, create `~/.agent-bus/hook-payloads.jsonl`:
  every hook run appends its raw payload there until you delete the file (it can hold prompts and your
  email; delete it when done).
- Codex runs hooks only after you trust them (`/hooks`, app: Settings > Hooks), again after every
  change to the command, and its hook runner fails on quoted paths, so the installer writes them
  unquoted (8.3 short form if a path has spaces). The Codex app calls MCP tools from inside its `exec`
  tool and names them `mcp__agent_bus__<tool>`; hooks still see each MCP call.

## Cost

A parked worker re-calls the model each time `wait` returns empty: about 24 small calls an hour at a
150 s cap (Claude Code, Antigravity), about 72 at the default 50 s. Park workers only while the team is
active, and tell them to stop when you are done.

## Tests

```powershell
python -m unittest -v
```

Covers the MCP handshake, two server processes exchanging messages, `wait` returning on arrival and on
timeout, a cancelled `wait`, the unread notice on `send`, four processes writing to one DB at once, the
four hook formats, the installer's merge and uninstall for every agent, a full install, hook run and
uninstall on macOS/Linux against stub CLIs, and the e2e helpers below. GitHub Actions runs them on Ubuntu,
Windows and macOS with Python 3.9 and 3.13.

`e2e/notice.py` checks any agent live: it plays the lead, keeps each member busy with shell commands and
sends a codeword meanwhile. PASS needs bus evidence that a hook announced it before the member read it
(flagged notified while still unread), not the agent's own word. Claude Code, Codex and Cursor passed it on
2026-11-27:

```powershell
python e2e/notice.py run cx cur      # then paste `python e2e/notice.py prompt cx` (and cur) into each agent
```

`run` prints how many seconds after the codeword each hook announced it. With the opt-in hook log present
(`~/.agent-bus/hook-payloads.jsonl`), `python e2e/notice.py timeline cur` then lists every hook event that
member's agent fired around the codeword, its tool, whether it carried a session id, and when the notice
went out: the way to see why one agent announces later than another.

`e2e/team.py` is a live four-agent scenario (one frontend and one backend pair, each a lead and a dev, text
only) with the first prompts and a checked report. By default the leads are Claude Code and the devs
Antigravity; `--agents fe-dev=cursor,be-dev=codex` (same value for every `prompt` and for `report`) picks
the agent per role, and the report then requires every pair of agent kinds to have talked both ways. See
its docstring.

## License

MIT, see [LICENSE](LICENSE).

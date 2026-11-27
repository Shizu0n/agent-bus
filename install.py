"""Register agent-bus in every coding agent found on this machine, or remove it.

    python install.py [--dry-run] [--only AGENT,...]              register the MCP server and merge the hooks
    python install.py --uninstall [--dry-run] [--only AGENT,...]  undo it

Agents: claude (Claude Code), antigravity, codex, cursor, vscode. Each one whose CLI or config folder
exists is set up; the others are skipped. Only agent-bus entries are touched: other MCP servers and hooks
stay as they are. Every JSON file is backed up next to itself (*.bak-agent-bus-<timestamp>) before it is
rewritten. The bus database (~/.agent-bus/bus.db) is never deleted.
"""
import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
SCRIPT = (Path(__file__).resolve().parent / "agent_bus.py").as_posix()
PY = Path(sys.executable).as_posix()  # absolute: sidesteps python vs py vs the Store stub; re-run after a Python upgrade
NAME = "agent-bus"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
AGY_HOOKS = HOME / ".gemini" / "config" / "hooks.json"
AGY_MCP = HOME / ".gemini" / "config" / "mcp_config.json"
CODEX_HOOKS = HOME / ".codex" / "hooks.json"
CURSOR_DIR = HOME / ".cursor"
VSCODE_DIR = Path(os.environ.get("APPDATA", HOME / ".config")) / "Code" / "User"  # Windows / Linux
if sys.platform == "darwin":
    VSCODE_DIR = HOME / "Library" / "Application Support" / "Code" / "User"
# Claude Code and Antigravity allow tool calls past 60 s, so a parked wait can return less often (fewer model calls)
LONG_WAIT = "AGENT_BUS_WAIT_CAP=150"


def bare_path(p: str) -> str:
    """A path usable unquoted (Antigravity runs hooks via cmd /c, which rejects its escaped quotes):
    as is without spaces; else on Windows its 8.3 short form, and on macOS/Linux a shell-quoted path
    (hook runners there start a shell; a path with spaces cannot be left bare)."""
    if " " not in p:
        return p
    if os.name == "nt":
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(p, buf, 1024):
            return buf.value.replace("\\", "/")
        return p
    return shlex.quote(p)


QUOTED = f'"{PY}" "{SCRIPT}" hook'  # Claude Code runs hooks in bash: quotes are fine
BARE = f"{bare_path(PY)} {bare_path(SCRIPT)} hook"  # for agents whose hook runner rejects quoted paths


def ours(command) -> bool:
    return "agent_bus.py" in str(command).replace("\\", "/")


def claude_style_hooks(agent: str, all_tools_matcher, prefix: str):
    """Merge function for the Claude hook format {"hooks": {event: [{matcher, hooks: [...]}]}} (Claude, Codex)."""
    def change(cfg: dict, install: bool) -> dict:
        hooks = cfg.setdefault("hooks", {})
        for event in ("PostToolUse", "UserPromptSubmit"):
            groups = []
            for g in hooks.get(event, []):
                kept = [h for h in g.get("hooks", []) if not ours(h.get("command"))]
                if kept:
                    groups.append({**g, "hooks": kept})
            if install:
                g = {"hooks": [{"type": "command", "command": f"{prefix} {agent}", "timeout": 10}]}
                groups.append({"matcher": all_tools_matcher, **g} if event == "PostToolUse" and all_tools_matcher else g)
            if groups:
                hooks[event] = groups
            else:
                hooks.pop(event, None)
        if not hooks:
            cfg.pop("hooks")
        return cfg
    return change


claude_hooks = claude_style_hooks("claude", "*", QUOTED)
# Codex matchers are regexes: no matcher means every tool. Unquoted, like the hooks Codex runs fine here
codex_hooks = claude_style_hooks("codex", None, BARE)


def agy_hooks(cfg: dict, install: bool) -> dict:
    cfg.pop(NAME, None)
    if install:  # named-hook format: agent-bus owns this one key
        cfg[NAME] = {
            # records which team name this conversation registered (PostToolUse cannot send text)
            "PostToolUse": [{"matcher": "call_mcp_tool", "hooks": [
                {"type": "command", "command": f"{BARE} antigravity PostToolUse", "timeout": 10}]}],
            # announces unread messages once; fires on every model call
            "PreInvocation": [{"type": "command", "command": f"{BARE} antigravity PreInvocation", "timeout": 10}],
        }
    return cfg


def cursor_hooks(cfg: dict, install: bool) -> dict:
    cfg.setdefault("version", 1)
    hooks = cfg.setdefault("hooks", {})
    # afterMCPExecution maps the session (it names the server); postToolUse announces via additional_context
    for event in ("afterMCPExecution", "postToolUse"):
        kept = [h for h in hooks.get(event, []) if not ours(h.get("command"))]
        if install:
            kept.append({"command": f"{BARE} cursor", "timeout": 10})
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        cfg.pop("hooks")
        if cfg == {"version": 1}:
            return {}
    return cfg


def mcp_json(key: str, extra: dict):
    """Merge function for JSON MCP configs: {key: {name: {command, args}}} (Cursor: mcpServers, VS Code: servers)."""
    def change(cfg: dict, install: bool) -> dict:
        servers = cfg.setdefault(key, {})
        servers.pop(NAME, None)
        if install:
            servers[NAME] = {**extra, "command": PY, "args": [SCRIPT]}
        if not servers:
            cfg.pop(key)
        return cfg
    return change


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() and p.read_text(encoding="utf-8").strip() else {}


def dump(cfg: dict) -> str:
    return json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"


def backup(p: Path) -> None:
    if p.exists():
        b = p.with_name(f"{p.name}.bak-agent-bus-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(p, b)
        print(f"  backup: {b}")


def edit_json(p: Path, change, install: bool, dry: bool) -> None:
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    try:
        cfg = load(p)
    except ValueError as e:  # e.g. JSON with comments, which VS Code allows: never overwrite what we cannot parse
        print(f"{p}: not plain JSON ({e}); skipped, add the agent-bus entry by hand (see README)")
        return
    new = dump(change(cfg, install))
    if new == old or (not old and new.strip() == "{}"):
        print(f"{p}: no change")
        return
    print(f"{p}:")
    sys.stdout.writelines(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "before", "after", n=2))
    if not dry:
        backup(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8", newline="\n") as f:  # write_text(newline=) needs Python 3.10
            f.write(new)


def run(cmd: list, dry: bool, ok_fail: bool = False) -> None:
    print("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if dry:
        return
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out:
        print("  " + out.replace("\n", "\n  "))
    if r.returncode and not ok_fail:
        sys.exit(f"command failed ({r.returncode}); steps before it already ran (see above), later ones did not. Fix and re-run.")


def find(exe: str, fallback):
    return shutil.which(exe) or (str(fallback) if fallback and fallback.exists() else None)


def setup_claude(exe, install, dry):
    run([exe, "mcp", "remove", "--scope", "user", NAME], dry, ok_fail=True)  # makes re-install idempotent
    if install:
        run([exe, "mcp", "add", "--scope", "user", NAME, "-e", LONG_WAIT, "--", PY, SCRIPT], dry)  # -e is variadic: after NAME
    edit_json(CLAUDE_SETTINGS, claude_hooks, install, dry)


def setup_antigravity(exe, install, dry):
    if not dry:
        backup(AGY_MCP)
    run([exe, "mcp", "add", "--env", LONG_WAIT, NAME, PY, SCRIPT] if install else [exe, "mcp", "remove", NAME],
        dry, ok_fail=not install)
    edit_json(AGY_HOOKS, agy_hooks, install, dry)


def setup_codex(exe, install, dry):
    run([exe, "mcp", "remove", NAME], dry, ok_fail=True)
    if install:  # default wait cap (50 s): Codex cuts MCP tool calls at tool_timeout_sec, 60 s by default
        run([exe, "mcp", "add", NAME, "--", PY, SCRIPT], dry)
    edit_json(CODEX_HOOKS, codex_hooks, install, dry)
    if install:
        print("  Codex skips new hooks until you trust them: run /hooks in Codex (app: Settings > Hooks).")


def setup_cursor(_, install, dry):
    edit_json(CURSOR_DIR / "mcp.json", mcp_json("mcpServers", {"type": "stdio"}), install, dry)
    edit_json(CURSOR_DIR / "hooks.json", cursor_hooks, install, dry)


def setup_vscode(_, install, dry):  # MCP only: no hook can add context here, so mail shows on send results
    edit_json(VSCODE_DIR / "mcp.json", mcp_json("servers", {"type": "stdio"}), install, dry)


EXE = ".exe" if os.name == "nt" else ""
# Fallbacks for CLIs installed outside PATH. Antigravity's per-user folder exists only on Windows
# (LOCALAPPDATA); elsewhere it must be on PATH (an empty LOCALAPPDATA would resolve against the cwd).
AGY_FALLBACK = Path(os.environ["LOCALAPPDATA"]) / "agy" / "bin" / "agy.exe" if os.environ.get("LOCALAPPDATA") else None

AGENTS = {  # name: (label, how to detect it, setup)
    "claude": ("Claude Code", lambda: find("claude", HOME / ".local" / "bin" / f"claude{EXE}"), setup_claude),
    "antigravity": ("Antigravity", lambda: find("agy", AGY_FALLBACK), setup_antigravity),
    "codex": ("Codex", lambda: shutil.which("codex"), setup_codex),
    "cursor": ("Cursor", lambda: CURSOR_DIR.is_dir() or None, setup_cursor),
    "vscode": ("VS Code", lambda: VSCODE_DIR.is_dir() or None, setup_vscode),
}


def main(args: list) -> int:
    dry, install = "--dry-run" in args, "--uninstall" not in args
    only = args[args.index("--only") + 1].split(",") if "--only" in args and args.index("--only") + 1 < len(args) else None
    if only and set(only) - set(AGENTS):
        sys.exit(f"unknown agent(s): {', '.join(sorted(set(only) - set(AGENTS)))}; known: {', '.join(AGENTS)}")
    if dry:
        print("DRY RUN: nothing is changed.\n")
    if os.name != "nt" and (" " in PY or " " in SCRIPT):
        print("Note: the Python or agent-bus path has a space, so hook commands quote it. Codex's hook runner\n"
              "rejected quoted paths on Windows; if Codex reports its hook as Failed, move the repo (or Python)\n"
              "to a path without spaces and re-run.\n")
    for key, (label, detect, setup) in AGENTS.items():
        if only and key not in only:
            continue
        found = detect()
        if not found:
            print(f"== {label}: not found, skipped\n")
            continue
        print(f"== {label} ==")
        setup(found, install, dry)
        print()
    if not dry:
        print("Done. Restart open agent sessions so they load the server and hooks.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

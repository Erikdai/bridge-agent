from __future__ import annotations

import argparse
import getpass
import os
import sys
import time


def _agent(args):
    from .agent import Agent
    from .config import load_settings
    from .llm import make_planner

    if getattr(args, "confirm", None):
        os.environ["BRIDGE_CONFIRM"] = args.confirm
    if getattr(args, "confirm_timeout", None):
        os.environ["BRIDGE_CONFIRM_TIMEOUT"] = str(args.confirm_timeout)
    settings = load_settings()
    planner = make_planner(settings)
    print(
        f"provider={settings.provider} model={planner.model} max_steps={settings.max_steps} "
        f"confirm={settings.confirm_mode} skills={'off' if getattr(args, 'no_skills', False) else settings.skills_dir}"
    )
    return Agent(settings, planner)


def _report(result) -> int:
    print("\n=== RESULT ===")
    print(f"status : {result.status}   mode: {result.mode}" + (f"   skill: {result.skill}" if result.skill else ""))
    print(f"summary: {result.summary}")
    print(f"replayed steps: {result.replay_steps}   llm steps: {len(result.steps)}   llm calls: {result.llm_calls}")
    print(f"time: {result.seconds:.1f}s   tokens: {result.tokens}")
    print(f"trace  : {result.run_dir}")
    return 0 if result.status == "done" else 1


def cmd_run(args) -> int:
    agent = _agent(args)
    result = agent.run(
        args.task, sites=args.site, start_url=args.start_url, use_skills=not args.no_skills, skill_name=args.skill_name
    )
    return _report(result)


def cmd_focus(_args) -> int:
    from .focus import get_focus

    print(get_focus().describe())
    return 0


def cmd_dump(args) -> int:
    """Print the unified tree of whatever window is in front after a short delay."""
    from .focus import get_focus
    from .observers.desktop import observe_desktop

    print(f"switch to the window you want to inspect... ({args.delay}s)")
    time.sleep(args.delay)
    focus = get_focus()
    print("FOCUS:", focus.describe())
    obs = observe_desktop(max_nodes=args.max_nodes, with_screenshot=bool(args.screenshot))
    print(obs.to_prompt(max_nodes=args.max_nodes))
    print(f"\n{len(obs.nodes)} nodes, {obs.interactive_count()} interactive")
    if args.screenshot and obs.screenshot_png:
        with open(args.screenshot, "wb") as f:
            f.write(obs.screenshot_png)
        print(f"screenshot -> {args.screenshot}")
    return 0


def cmd_scan(args) -> int:
    """Open a URL in Playwright and print the browser-side tree (no LLM involved)."""
    from playwright.sync_api import sync_playwright

    from .config import load_settings
    from .observers.browser import observe_browser

    settings = load_settings()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, channel=settings.browser_channel)
        page = browser.new_page()
        page.goto(args.url, wait_until="domcontentloaded")
        obs = observe_browser(page, max_nodes=args.max_nodes)
        print(obs.to_prompt(max_nodes=args.max_nodes))
        print(f"\n{len(obs.nodes)} nodes, {obs.interactive_count()} interactive")
        browser.close()
    return 0


def cmd_cred(args) -> int:
    from .credentials import CredentialBroker

    broker = CredentialBroker()
    if args.op == "set":
        username = args.username or input("username: ")
        password = getpass.getpass("password (hidden): ")
        broker.set(args.site, username, password)
        print(f"stored credential for {args.site!r} in the OS keyring")
    elif args.op == "get":
        cred = broker.get(args.site)
        print("not found" if cred is None else f"{args.site}: username={cred[0]} password=<hidden>")
    elif args.op == "delete":
        print("deleted" if broker.delete(args.site) else "not found")
    return 0


def cmd_skill(args) -> int:
    from .config import load_settings
    from .skills.store import SkillStore

    store = SkillStore(load_settings().skills_dir)
    if args.op == "list":
        for s in store.all():
            print(
                f"{s.name:44s} {s.source:9s} {s.last_status:6s} steps={len(s.steps):3d} ok={s.successes}/{s.runs} healed={s.healed}  {s.description[:50]}"
            )
    elif args.op == "show":
        print(store.load(args.name).to_json())
    elif args.op == "delete":
        print("deleted" if store.delete(args.name) else "not found")
    elif args.op == "replay":
        skill = store.load(args.name)
        agent = _agent(args)
        result = agent.run(skill.description, start_url=args.start_url, use_skills=True)
        return _report(result)
    return 0


def cmd_notify(args) -> int:
    from .runtime import notify

    notify(args.title, args.body)
    return 0


def cmd_perms(_args) -> int:
    from .config import load_settings
    from .guard import Permissions

    s = load_settings()
    p = Permissions.load(s.permissions_file)
    print(f"source: {p.source}")
    for k in (
        "confirm",
        "allowed_apps",
        "denied_apps",
        "allowed_dirs",
        "denied_windows",
        "denied_url_patterns",
        "sites",
        "audit_log",
    ):
        print(f"{k:20s} {getattr(p, k)}")
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main

    return gui_main(args.task or "", args.start_url or "", args.autorun, args.quit_when_done)


def cmd_mcp(args) -> int:
    from .mcp_server import main as mcp_main

    mcp_main(args.start_url)
    return 0


def cmd_eval(args) -> int:
    from .eval import markdown_table, run_eval

    agent = _agent(args)
    rows = run_eval(agent, args.tasks, only=args.only or None, use_skills=not args.no_skills)
    print()
    print(markdown_table(rows))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to a legacy code page
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    p = argparse.ArgumentParser(prog="bridgeagent", description="web-to-desktop handoff agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a task: replay a matching skill if there is one, else drive with the LLM")
    r.add_argument("task")
    r.add_argument("--start-url", default=None)
    r.add_argument("--site", action="append", default=[], help="credential site key the agent may use (repeatable)")
    r.add_argument("--no-skills", action="store_true", help="ignore saved skills and do not learn one")
    r.add_argument("--skill-name", default=None)
    r.add_argument(
        "--confirm",
        choices=["never", "risky", "always"],
        default=None,
        help="confirmation policy (default from BRIDGE_CONFIRM, risky)",
    )
    r.add_argument(
        "--confirm-timeout", type=int, default=None, help="seconds before an unanswered confirmation counts as No"
    )
    r.set_defaults(fn=cmd_run)

    sub.add_parser("focus", help="print which surface owns the keyboard").set_defaults(fn=cmd_focus)

    d = sub.add_parser("dump", help="print the UIA tree of the foreground window")
    d.add_argument("--delay", type=float, default=3.0)
    d.add_argument("--max-nodes", type=int, default=120)
    d.add_argument("--screenshot", default=None, help="also save a PNG of the window")
    d.set_defaults(fn=cmd_dump)

    s = sub.add_parser("scan", help="print the browser tree for a URL")
    s.add_argument("url")
    s.add_argument("--max-nodes", type=int, default=120)
    s.add_argument("--headless", action="store_true")
    s.set_defaults(fn=cmd_scan)

    c = sub.add_parser("cred", help="manage credentials in the OS keyring")
    c.add_argument("op", choices=["set", "get", "delete"])
    c.add_argument("site")
    c.add_argument("--username", default=None)
    c.set_defaults(fn=cmd_cred)

    k = sub.add_parser("skill", help="list / show / delete / replay skills")
    k.add_argument("op", choices=["list", "show", "delete", "replay"])
    k.add_argument("name", nargs="?")
    k.add_argument("--start-url", default=None)
    k.add_argument("--confirm", choices=["never", "risky", "always"], default=None)
    k.add_argument("--confirm-timeout", type=int, default=None)
    k.set_defaults(fn=cmd_skill)

    n = sub.add_parser("notify", help="send a Windows toast (runtime smoke test)")
    n.add_argument("title")
    n.add_argument("body")
    n.set_defaults(fn=cmd_notify)

    sub.add_parser("perms", help="print the effective permission manifest").set_defaults(fn=cmd_perms)

    g = sub.add_parser("gui", help="small desktop window: task input, progress, approvals, stop, result")
    g.add_argument("--task", default="")
    g.add_argument("--start-url", default="")
    g.add_argument("--autorun", action="store_true")
    g.add_argument("--quit-when-done", action="store_true")
    g.set_defaults(fn=cmd_gui)

    m = sub.add_parser("mcp", help="serve the execution layer as an MCP server over stdio")
    m.add_argument("--start-url", default=None)
    m.set_defaults(fn=cmd_mcp)

    e = sub.add_parser("eval", help="run every task in a tasks yaml and print a results table")
    e.add_argument("--tasks", default="tasks/examples.yaml")
    e.add_argument("--only", action="append", default=[], help="task id to run (repeatable)")
    e.add_argument("--no-skills", action="store_true")
    e.add_argument("--confirm", choices=["never", "risky", "always"], default=None)
    e.add_argument("--confirm-timeout", type=int, default=None)
    e.set_defaults(fn=cmd_eval)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

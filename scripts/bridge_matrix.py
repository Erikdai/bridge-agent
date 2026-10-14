"""Bridge metrics: deterministic checks of the routing and execution layer, no LLM.

    python scripts/bridge_matrix.py

Scenarios (each prints pass/fail and a detail line):
  1. DOM -> UIA -> DOM        browser page -> Calculator -> back, same session
  2. in-window dialog         Chrome print preview routed to d-refs and cancelled through UIA
  3. foreign browser steals   another Chromium instance brought to the front: focus guard restores ours
  4. steal between obs/act    focus stolen after observing Calculator: keys still reach Calculator or are refused
  5. skill replay cross-env   calculator skill replayed without an LLM, result read from the trail
  6. MCP consecutive calls    40 alternating observe/press calls over stdio, no thread/state errors
  7-9. negative cases         closed window / stale element / activation refused -> refuse, nothing leaks
  10. window moved            click by ref still correct after the window moved
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NUMBERS = (ROOT / "tasks" / "pages" / "numbers.html").as_uri()
REPORT = (ROOT / "tasks" / "pages" / "report.html").as_uri()
rows: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    rows.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


class NoPlanner:
    model = "none"

    def decide(self, *a, **k):
        raise RuntimeError("not used")

    def ask(self, *a, **k):
        raise RuntimeError("not used")


def display_of(obs) -> str | None:
    for n in obs.nodes:
        m = re.search(r"Display is ([\d,]+)", n.name)
        if m:
            return m.group(1)
    return None


def scripted_scenarios() -> None:
    import uiautomation as auto

    from bridgeagent.actions import Action, BrowserExecutor, DesktopExecutor, dispatch
    from bridgeagent.agent import DOWNLOADS_PLACEHOLDER, Agent, BrowserSession
    from bridgeagent.config import load_settings
    from bridgeagent.focus import CHROMIUM_WINDOW_CLASS, bring_to_front, get_focus, top_windows_for_pid
    from bridgeagent.skills.replay import replay

    settings = load_settings()
    agent = Agent(settings, NoPlanner())
    run_dir = ROOT / "runs" / ("matrix-" + time.strftime("%Y%m%d-%H%M%S"))
    with BrowserSession(settings, run_dir, NUMBERS) as s:
        bx, dx = BrowserExecutor(s.page, activate=s.activate), DesktopExecutor(activate=s.activate)

        def run(action: Action):
            _, obs = agent.observe(s)
            return dispatch(action, obs, bx, dx).execute(action, obs), obs

        # 1. DOM -> UIA -> DOM
        _, o1 = agent.observe(s)
        r, _ = run(Action(type="launch", command="calc"))
        _, o2 = agent.observe(s)
        run(Action(type="type", text="1234+5678="))
        _, o3 = agent.observe(s)
        disp = display_of(o3)
        run(Action(type="press", keys="Alt+F4"))
        time.sleep(0.8)
        run(Action(type="focus_browser"))
        _, o4 = agent.observe(s)
        record(
            "1 DOM->UIA->DOM",
            o1.surface == "browser" and o2.surface == "desktop" and disp == "6,912" and o4.surface == "browser",
            f"surfaces {o1.surface}->{o2.surface}->{o4.surface}, display {disp}",
        )

        # 2. Chrome in-window dialog
        s.page.goto(REPORT, wait_until="domcontentloaded")
        s.activate()
        run(Action(type="press", keys="Control+P"))
        time.sleep(2.5)
        _, od = agent.observe(s)
        d_refs = [n for n in od.nodes if n.ref.startswith("d")]
        cancel = next((n for n in d_refs if n.role == "button" and n.name.lower().startswith("cancel")), None)
        dest = next((n for n in d_refs if n.role == "combobox" and "destination" in n.name.lower()), None)
        routed = od.surface == "browser" and bool(d_refs) and cancel is not None
        if cancel:
            res, _ = run(Action(type="click", ref=cancel.ref))
            time.sleep(1.0)
        _, oe = agent.observe(s)
        closed = not any(n.ref.startswith("d") for n in oe.nodes)
        record(
            "2 in-window dialog",
            routed and closed,
            f"{len(d_refs)} dialog nodes, destination={dest.value if dest else None!r}, cancelled via {'UIA' if cancel and cancel.ref.startswith('d') else '?'}, closed={closed}",
        )

        # a second, unrelated Chromium = "the user's other browser"
        b2 = s._pw.chromium.launch(headless=False)  # same Playwright driver, separate browser process
        p2 = b2.new_page()
        p2.goto("https://example.com", wait_until="domcontentloaded")
        info = b2.new_browser_cdp_session().send("SystemInfo.getProcessInfo")
        pid2 = next(int(p["id"]) for p in info["processInfo"] if p["type"] == "browser")
        h2 = top_windows_for_pid(pid2, CHROMIUM_WINDOW_CLASS)[0]
        try:
            # 3. foreign browser steals focus before observation
            bring_to_front(h2)
            stolen = get_focus()
            _, of = agent.observe(s)
            restored = get_focus()
            record(
                "3 foreign browser steals focus",
                stolen.hwnd == h2 and of.surface == "browser" and restored.hwnd == s.main_hwnd,
                f"foreign in front: {stolen.hwnd == h2}; observed surface={of.surface}; our window restored: {restored.hwnd == s.main_hwnd}",
            )

            # 4. focus stolen between observation and action
            run(Action(type="launch", command="calc"))
            _, oc = agent.observe(s)
            bring_to_front(h2)
            res = dx.execute(Action(type="type", text="999"), oc)
            _, oc2 = agent.observe(s)
            disp2 = display_of(oc2)
            safe = (res.ok and disp2 == "999") or (not res.ok and "focus was taken" in res.message)
            record("4 steal between observe and act", safe, f"ok={res.ok}, display={disp2}, msg={res.message[:60]!r}")
            run(Action(type="press", keys="Alt+F4"))
            time.sleep(0.8)
        finally:
            b2.close()

        # 5. skill replay across environments
        skill = next((sk for sk in agent.store.all() if "calculator" in sk.description.lower()), None)
        if skill is None:
            record("5 skill replay cross-env", False, "no calculator skill in skills_store")
        else:
            s.page.goto(NUMBERS, wait_until="domcontentloaded")
            s.activate()
            rr = replay(
                skill,
                lambda: agent.observe(s),
                bx,
                dx,
                substitutions={DOWNLOADS_PLACEHOLDER: str(run_dir / "downloads")},
            )
            trail = "\n".join(rr.trail)
            record(
                "5 skill replay cross-env",
                rr.ok and "6,912" in trail,
                f"ok={rr.ok}, steps={len(skill.steps)}, result in trail: {'6,912' in trail}, error={rr.error[:60]!r}",
            )

        # ---- negative / edge cases: when the target is uncertain, the executor must stop ----
        import ctypes

        import bridgeagent.focus as focus_mod

        def page_input_value() -> str:
            return s.page.locator("#q").input_value()

        s.page.goto(REPORT, wait_until="domcontentloaded")
        s.activate()
        s.page.locator("#q").click()  # a text box that would receive leaked keystrokes

        # 7. target window closed after observation -> refuse, nothing leaks into the page
        run(Action(type="launch", command="calc"))
        _, oc = agent.observe(s)
        one = next((n for n in oc.nodes if n.role == "button" and n.name == "One"), None)
        for h in top_windows_for_pid(oc.pid):
            try:
                wp = auto.ControlFromHandle(h).GetPattern(auto.PatternId.WindowPattern)
                if wp:
                    wp.Close()
            except Exception:
                pass
        time.sleep(1.0)
        s.activate()
        s.page.locator("#q").click()
        res7 = dx.execute(Action(type="type", text="777"), oc)
        leaked7 = page_input_value()
        record(
            "7 target window closed after observation",
            (not res7.ok) and leaked7 == "",
            f"ok={res7.ok}, msg={res7.message[:70]!r}, leaked into page input: {leaked7!r}",
        )

        # 8. stale UIA element (its window is gone) -> safe failure, no blind click
        res8 = dx.execute(Action(type="click", ref=one.ref), oc) if one else None
        record(
            "8 stale UIA element after window closed",
            res8 is not None and not res8.ok,
            f"ok={res8.ok if res8 else None}, msg={(res8.message if res8 else '')[:70]!r}",
        )

        # 9. target window cannot be re-activated -> explicit error, no keystrokes sent
        run(Action(type="launch", command="calc"))
        _, oc9 = agent.observe(s)
        s.activate()
        s.page.locator("#q").click()
        real = focus_mod.bring_to_front
        focus_mod.bring_to_front = lambda hwnd: False  # simulate an activation that Windows refuses
        try:
            res9 = dx.execute(Action(type="type", text="888"), oc9)
        finally:
            focus_mod.bring_to_front = real
        leaked9 = page_input_value()
        record(
            "9 target window cannot be re-activated",
            (not res9.ok) and "focus was taken" in res9.message and leaked9 == "",
            f"ok={res9.ok}, msg={res9.message[:70]!r}, leaked into page input: {leaked9!r}",
        )

        # 10. window moved after observation -> click by ref still hits the right element (live UIA, no stored coordinates)
        run(Action(type="launch", command="calc"))
        _, oc10 = agent.observe(s)
        one10 = next((n for n in oc10.nodes if n.role == "button" and n.name == "One"), None)
        ctypes.windll.user32.SetWindowPos(
            ctypes.c_void_p(oc10.hwnd), None, 300, 200, 0, 0, 0x0001 | 0x0004
        )  # SWP_NOSIZE|SWP_NOZORDER
        time.sleep(0.5)
        res10 = dx.execute(Action(type="click", ref=one10.ref), oc10) if one10 else None
        _, after10 = agent.observe(s)
        disp10 = display_of(after10)
        record(
            "10 window moved after observation",
            res10 is not None and res10.ok and disp10 == "1",
            f"ok={res10.ok if res10 else None}, display={disp10}",
        )
        run(Action(type="press", keys="Alt+F4"))
        time.sleep(0.8)


async def mcp_scenario() -> None:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    exe = Path(sys.executable).with_name("bridgeagent.exe")
    errors, n = 0, 40
    t0 = time.time()
    async with stdio_client(StdioServerParameters(command=str(exe), args=["mcp"])) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            await session.call_tool("observe", {"start_url": NUMBERS})
            for i in range(n):
                try:
                    res = await session.call_tool(
                        "observe" if i % 2 == 0 else "press", {} if i % 2 == 0 else {"keys": "Shift"}
                    )
                    text = "\n".join(getattr(c, "text", "") for c in res.content)
                    if "SURFACE:" not in text or "Error" in text[:40]:
                        errors += 1
                except Exception:
                    errors += 1
    record(
        "6 MCP consecutive calls",
        errors == 0,
        f"{n} alternating observe/press calls, {errors} errors, {time.time() - t0:.1f}s",
    )


def main() -> int:
    scripted_scenarios()
    asyncio.run(mcp_scenario())
    print("\n| scenario | result | detail |\n|---|---|---|")
    for name, ok, detail in rows:
        print(f"| {name} | {'pass' if ok else 'FAIL'} | {detail} |")
    return 0 if all(ok for _, ok, _ in rows) else 1


if __name__ == "__main__":
    sys.exit(main())

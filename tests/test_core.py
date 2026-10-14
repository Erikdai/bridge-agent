"""Pure-logic tests: no browser, no UIA, no API key needed."""

from bridgeagent.actions import Action, ExecResult, playwright_chord_to_sendkeys
from bridgeagent.agent import RunResult, StepRecord, distill_steps, slugify
from bridgeagent.llm.base import Decision
from bridgeagent.skills.store import Skill, SkillStore
from bridgeagent.tree import Observation, UINode


def test_chord_translation():
    assert playwright_chord_to_sendkeys("Enter") == "{Enter}"
    assert playwright_chord_to_sendkeys("Control+S") == "{Ctrl}s"
    assert playwright_chord_to_sendkeys("Alt+F4") == "{Alt}{F4}"
    assert playwright_chord_to_sendkeys("Control+Shift+ArrowDown") == "{Ctrl}{Shift}{Down}"


def test_observation_prompt_and_lookup():
    nodes = [
        UINode(ref="d1", role="window", name="Save As", surface="desktop"),
        UINode(
            ref="d2",
            role="textbox",
            name="File name:",
            surface="desktop",
            value="report.csv",
            depth=2,
            states=["focused"],
        ),
        UINode(ref="d3", role="button", name="Save", surface="desktop", depth=2),
    ]
    obs = Observation(surface="desktop", title="Save As", detail="chrome.exe / #32770", nodes=nodes)
    text = obs.to_prompt()
    assert "SURFACE: desktop" in text
    assert '[d3] button "Save"' in text
    assert "value='report.csv'" in text
    assert obs.by_ref("d2").role == "textbox"
    assert obs.by_ref("zz") is None
    assert obs.interactive_count() == 2


def test_decision_schema_round_trip():
    d = Decision.model_validate({"reasoning": "click save", "action": {"type": "click", "ref": "d3"}})
    assert d.action.describe() == "click ref='d3'"
    strict = Decision.model_json_schema()
    assert strict["additionalProperties"] is False


def test_distill_skips_failed_and_terminal_steps(tmp_path):
    def rec(step, action, ok=True, target=None):
        return StepRecord(
            step, "desktop", "desktop: x", Decision(reasoning="r", action=action), ExecResult(ok=ok), target, {}, 0.1
        )

    result = RunResult(
        task="save the report as csv",
        status="done",
        summary="saved",
        steps=[
            rec(1, Action(type="click", ref="d3"), target={"role": "button", "name": "Save"}),
            rec(2, Action(type="type", ref="d2", text="x"), ok=False, target={"role": "textbox", "name": "File name:"}),
            rec(3, Action(type="press", keys="Enter")),
            rec(4, Action(type="done", summary="saved")),
        ],
    )
    skill = Skill(name=slugify(result.task), description=result.task, steps=distill_steps(result.steps))
    assert [s.action for s in skill.steps] == ["click", "press"]
    assert skill.steps[0].target == {"role": "button", "name": "Save"}
    store = SkillStore(tmp_path)
    store.save(skill)
    assert store.load(skill.name).description == "save the report as csv"
    assert store.match("save report csv").name == skill.name
    assert store.match("book a flight to tokyo") is None


def test_slugify():
    assert slugify("Download the CSV, then open it in Excel!") == "download-the-csv-then-open-it-in-excel"
    assert slugify("打开 Excel 文件") == "打开-excel-文件"


def test_browser_level_chords():
    from bridgeagent.actions import is_browser_level_chord

    assert is_browser_level_chord("Control+S")
    assert is_browser_level_chord("Alt+F4")
    assert is_browser_level_chord("F12")
    assert not is_browser_level_chord("Enter")
    assert not is_browser_level_chord("Shift+Tab")
    assert not is_browser_level_chord("ArrowDown")


def test_coerce_decision_tolerates_flat_json():
    from bridgeagent.llm.openai_backend import coerce_decision

    d = coerce_decision({"reasoning": "r", "type": "done", "summary": "6912"})
    assert d.action.type == "done" and d.action.summary == "6912"
    d = coerce_decision({"reasoning": "r", "action": "focus_browser"})
    assert d.action.type == "focus_browser"
    d = coerce_decision({"reasoning": "r", "action": {"type": "click", "ref": "d4"}, "summary": "stray"})
    assert d.action.type == "click" and d.action.ref == "d4" and d.action.summary == "stray"
    d = coerce_decision({"action": {"type": "wait"}, "unexpected": 1})
    assert d.action.type == "wait" and d.reasoning == ""


def test_skill_compaction_schema_round_trip():
    from bridgeagent.llm.base import SkillCompaction

    out = SkillCompaction.model_validate({"reasoning": "x", "keep": [1, 3, 5], "extra": "ignored"})
    assert out.keep == [1, 3, 5]


def test_distill_keeps_credential_site_and_window():
    from bridgeagent.actions import Action, ExecResult
    from bridgeagent.agent import StepRecord, distill_steps
    from bridgeagent.llm.base import Decision

    rec = StepRecord(
        1,
        "browser",
        "browser: x",
        Decision(reasoning="r", action=Action(type="fill_credential", ref="b2", site="demo-site")),
        ExecResult(ok=True),
        {"role": "textbox", "name": "Username"},
        {},
        0.1,
        window="Demo Login",
    )
    rec2 = StepRecord(
        2,
        "desktop",
        "desktop: x",
        Decision(reasoning="r", action=Action(type="type", text="1+1=")),
        ExecResult(ok=True),
        None,
        {},
        0.1,
        window="Calculator",
    )
    steps = distill_steps([rec, rec2])
    assert steps[0].site == "demo-site" and steps[0].window is None
    assert steps[1].window == "Calculator" and steps[1].text == "1+1="


def test_new_lines_and_upload_path_survive_distillation():
    from bridgeagent.agent import _new_lines, distill_steps

    assert _new_lines("a\nb", "a\nb\nSaved. Confirmation #12") == ["Saved. Confirmation #12"]
    rec = StepRecord(
        1,
        "browser",
        "x",
        Decision(reasoning="r", action=Action(type="upload", ref="b2", path="D:\\x\\inv.pdf")),
        ExecResult(ok=True),
        {"role": "fileinput", "name": "Invoice file"},
        {},
        0.1,
    )
    assert distill_steps([rec])[0].path == "D:\\x\\inv.pdf"

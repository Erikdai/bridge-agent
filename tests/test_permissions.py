from bridgeagent.actions import Action
from bridgeagent.guard import Permissions
from bridgeagent.tree import Observation, UINode


def _perms(tmp_path):
    cfg = tmp_path / "permissions.yaml"
    cfg.write_text(
        "allowed_apps: [calc, notepad, ms-settings:]\n"
        "denied_apps: [powershell]\n"
        f"allowed_dirs: ['{tmp_path.as_posix()}/ok']\n"
        "denied_windows: [Weixin, Outlook]\n"
        "denied_url_patterns: ['bank']\n"
        "sites: [demo-site]\n"
        f"audit_log: '{(tmp_path / 'audit.jsonl').as_posix()}'\n",
        encoding="utf-8",
    )
    return Permissions.load(cfg)


def _obs(title="Calculator", detail="ApplicationFrameHost.exe"):
    return Observation(surface="desktop", title=title, detail=detail, nodes=[])


def test_launch_rules(tmp_path):
    p = _perms(tmp_path)
    obs = _obs()
    assert p.check(Action(type="launch", command="calc"), None, obs) is None
    assert p.check(Action(type="launch", command="C:\\Windows\\System32\\notepad.exe"), None, obs) is None
    assert p.check(Action(type="launch", command="ms-settings:about"), None, obs) is None
    assert "not in allowed_apps" in p.check(Action(type="launch", command="mspaint"), None, obs)
    assert "forbids launching 'powershell'" in p.check(
        Action(type="launch", command="powershell -c hostname"), None, obs
    )


def test_file_paths_must_be_inside_allowed_dirs(tmp_path):
    p = _perms(tmp_path)
    ok = tmp_path / "ok" / "a.csv"
    ok.parent.mkdir()
    ok.write_text("x")
    bad = tmp_path / "secret.txt"
    bad.write_text("x")
    obs = _obs()
    assert p.check(Action(type="launch", command=str(ok)), None, obs) is None
    assert "outside allowed_dirs" in p.check(Action(type="launch", command=str(bad)), None, obs)
    # the run's downloads dir is passed as an extra allowed dir
    dl = tmp_path / "downloads" / "r.csv"
    dl.parent.mkdir()
    dl.write_text("x")
    assert p.check(Action(type="launch", command=str(dl)), None, obs, extra_dirs=[str(dl.parent)]) is None


def test_denied_windows_and_urls_and_sites(tmp_path):
    p = _perms(tmp_path)
    assert "denied_windows" in p.check(
        Action(type="click", ref="d1"), UINode("d1", "button", "Send", "desktop"), _obs("Weixin", "Weixin.exe")
    )
    assert p.check(Action(type="click", ref="d1"), None, _obs("Calculator")) is None
    assert "forbids navigating" in p.check(Action(type="navigate", url="https://mybank.example"), None, _obs())
    assert p.check(Action(type="navigate", url="https://example.com"), None, _obs()) is None
    assert p.check(Action(type="fill_credential", site="demo-site"), None, _obs()) is None
    assert "forbids using credential site" in p.check(Action(type="fill_credential", site="other"), None, _obs())
    assert p.check(Action(type="fill_credential", site="other"), None, _obs(), task_sites=["other"]) is None


def test_missing_manifest_is_permissive_and_audit_writes(tmp_path):
    p = Permissions.load(tmp_path / "nope.yaml")
    assert p.check(Action(type="launch", command="anything"), None, _obs()) is None
    q = _perms(tmp_path)
    q.audit({"action": {"type": "click"}, "decision": "allowed"})
    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8").count("\n") == 1


def test_upload_path_must_be_inside_allowed_dirs(tmp_path):
    p = _perms(tmp_path)
    ok = tmp_path / "ok" / "inv.pdf"
    ok.parent.mkdir(exist_ok=True)
    ok.write_text("x")
    assert p.check(Action(type="upload", ref="b2", path=str(ok)), None, _obs()) is None
    assert "outside allowed_dirs" in p.check(
        Action(type="upload", ref="b2", path=str(tmp_path / "secret.pdf")), None, _obs()
    )

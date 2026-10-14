from bridgeagent.actions import Action, dispatch
from bridgeagent.guard import ConfirmPolicy, is_risky_name, needs_confirmation
from bridgeagent.tree import Observation, UINode


def _obs(*nodes):
    return Observation(surface="browser", title="Mail", detail="https://mail.example", nodes=list(nodes))


def test_risky_names():
    assert is_risky_name("Send")
    assert is_risky_name("Place order")
    assert is_risky_name("删除账户")
    assert not is_risky_name("Sign in")
    assert not is_risky_name("Sender name")  # word boundary: "sender" is not "send"
    assert not is_risky_name("Close Calculator")


def test_policy_risky_only_flags_risky_clicks_and_submits():
    policy = ConfirmPolicy("risky", 5)
    send = UINode("b1", "button", "Send", "browser")
    close = UINode("b2", "button", "Close", "browser")
    box = UINode("b3", "textbox", "Message", "browser", states=["focused"])
    obs = _obs(send, close, box)
    assert needs_confirmation(Action(type="click", ref="b1"), send, obs, policy)
    assert needs_confirmation(Action(type="click", ref="b2"), close, obs, policy) is None
    assert needs_confirmation(Action(type="type", ref="b3", text="hi", submit=True), box, obs, policy) is None
    pay_box = UINode("b4", "textbox", "Confirm payment amount", "browser")
    assert needs_confirmation(Action(type="type", ref="b4", text="1", submit=True), pay_box, obs, policy)
    assert needs_confirmation(Action(type="type", text="1234+5678=", submit=True), None, obs, policy) is None
    assert needs_confirmation(Action(type="press", keys="Enter"), None, obs, policy) is None
    assert needs_confirmation(Action(type="done", summary="x"), None, obs, policy) is None


def test_policy_always_and_never():
    close = UINode("b2", "button", "Close", "browser")
    obs = _obs(close)
    assert needs_confirmation(Action(type="click", ref="b2"), close, obs, ConfirmPolicy("always", 5))
    assert needs_confirmation(Action(type="scroll", direction="down"), None, obs, ConfirmPolicy("always", 5)) is None
    send = UINode("b1", "button", "Send", "browser")
    assert needs_confirmation(Action(type="click", ref="b1"), send, _obs(send), ConfirmPolicy("never", 5)) is None


def test_dispatch_by_ref_prefix():
    class B:
        pass

    class D:
        pass

    b, d = B(), D()
    page_obs = Observation(surface="browser", title="t", detail="u", nodes=[])
    desk_obs = Observation(surface="desktop", title="t", detail="u", nodes=[])
    assert dispatch(Action(type="click", ref="d3"), page_obs, b, d) is d  # dialog control on the browser surface
    assert dispatch(Action(type="click", ref="b3"), page_obs, b, d) is b
    assert dispatch(Action(type="type", text="x"), page_obs, b, d) is b
    assert dispatch(Action(type="type", text="x"), desk_obs, b, d) is d
    assert dispatch(Action(type="navigate", url="https://x"), desk_obs, b, d) is b

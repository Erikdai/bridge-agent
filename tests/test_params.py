from bridgeagent.skills.params import parameterize, resolve, variables_from_json, variables_to_json

PAGE = "Two numbers\n\nFirst: 1234\n\nSecond: 5678"


def test_numbers_from_page_become_variables():
    out = parameterize("1234+5678=", PAGE)
    assert out is not None
    template, variables = out
    assert template == "{v1}+{v2}="
    assert {v.example for v in variables.values()} == {"1234", "5678"}
    # a page with different numbers resolves to different input
    value, err = resolve(template, variables, "Two numbers\n\nFirst: 2222\n\nSecond: 3333")
    assert err == "" and value == "2222+3333="


def test_round_trip_through_json_and_missing_value():
    template, variables = parameterize("1234+5678=", PAGE)
    vars_json = variables_to_json(variables)
    back = variables_from_json(vars_json)
    value, _ = resolve(template, back, PAGE)
    assert value == "1234+5678="
    value, err = resolve(template, back, "First: 10\n")
    assert value is None and "v2" in err


def test_no_variables_when_text_not_on_page():
    assert parameterize("report-2026.csv", PAGE) is None
    assert parameterize("", PAGE) is None


def test_ambiguous_prefix_is_not_parameterized():
    page = "Amount: 10\nAmount: 20"
    out = parameterize("10", page)
    assert out is None  # "Amount:" matches twice, so the locator would be unreliable


def test_word_values_and_literal_braces():
    page = "Username: alice_w\nRole: admin"
    template, variables = parameterize("alice_w {x}", page)
    assert template == "{v1} {{x}}"
    value, _ = resolve(template, variables, "Username: bob_k\nRole: admin")
    assert value == "bob_k {x}"

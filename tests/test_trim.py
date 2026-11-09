import pytest

from shelf.trim import CompiledRule, InvalidRule, apply_rules, compile_rule


def rule(pattern, mode="text", regex=False, ignore_case=True):
    return CompiledRule(None, compile_rule(pattern, regex, ignore_case), mode)


HTML = ("<p>Hello there.</p><p>Read at   example-tl.com only!</p>"
        "<p>She smiled. (TL note: lol) Then left.</p>"
        "<ul><li>a</li><li>Join our <strong>Discord</strong></li></ul>"
        "<p>Tail <em>em</em> after</p>")


def test_text_rule_removes_match_and_empty_paragraph():
    out, n = apply_rules(HTML, [rule("READ AT example-tl.com only!")])
    assert n == 1 and "example-tl" not in out
    assert "<p></p>" not in out and "<p>Hello there.</p>" in out


def test_regex_rule_inline():
    out, n = apply_rules(HTML, [rule(r"\(TL note:[^)]*\)\s*", regex=True)])
    assert "<p>She smiled. Then left.</p>" in out and n == 1


def test_paragraph_rule_spans_inline_markup():
    out, n = apply_rules(HTML, [rule("join our discord", mode="paragraph")])
    assert "Discord" not in out and "<li>a</li>" in out and n == 1


def test_case_sensitive_and_preserves_markup():
    out, n = apply_rules(HTML, [rule("hello", ignore_case=False)])
    assert n == 0 and out == HTML


def test_paragraph_rule_never_removes_wrappers():
    html = "<div><p>Keep me</p><p>watermark</p></div>"
    out, _ = apply_rules(html, [rule("watermark", mode="paragraph")])
    assert out == "<div><p>Keep me</p></div>"


@pytest.mark.parametrize("pattern,regex", [("", False), ("   ", False), ("(", True), (".*", True)])
def test_invalid_rules(pattern, regex):
    with pytest.raises(InvalidRule):
        compile_rule(pattern, regex, True)

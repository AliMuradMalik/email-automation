from app.templating import check_content, has_example_text, render


def test_render_fills_values_and_fallbacks():
    result = render("Hi {{first_name|there}}, about {{ company }}", {"first_name": "", "company": "Acme"})
    assert result.text == "Hi there, about Acme"
    assert result.missing == []


def test_render_reports_missing_fields():
    result = render("Hi {{first_name}}", {})
    assert result.text == "Hi "
    assert result.missing == ["first_name"]


def test_check_content_flags_spammy_email():
    warnings = " ".join(check_content("FREE MONEY NOW!!!", "Click here http://a.com http://b.com [your pitch]"))
    for expected in ("Spammy phrases", "capital letters", "!!", "2 links", "bracketed"):
        assert expected in warnings


def test_check_content_accepts_short_plain_email():
    assert check_content("Question about Acme", "Hi Jane,\n\nWorth a quick chat next week?") == []


def test_has_example_text():
    assert has_example_text("[One sentence on why]")
    assert not has_example_text("Hi {{first_name}}")

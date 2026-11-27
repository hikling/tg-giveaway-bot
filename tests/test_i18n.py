"""i18n and HTML-escaping contracts.

P2-1: the bot runs with ParseMode.HTML, but the i18n path used bare
str.format, so a giveaway titled "R&D key" made Telegram answer
`BadRequest: cant parse entities`.  Escaping moved into t() itself, and
Raw values pass through so the deliberately pre-built user mentions are
not double-escaped.
"""

from __future__ import annotations

from bot.i18n import available_langs, load_locales, missing_key_report, t
from bot.utils.esc import Raw, esc, esc_list, user_mention


def test_only_chinese_locale_is_loaded():
    """A key present in one language and missing in another is a runtime 500."""
    # en is the fallback table, so it is the baseline rather than a report row.
    report = missing_key_report()
    assert available_langs() == ["zh"]
    assert set(report) <= {"zh"}
    for lang, keys in report.items():
        assert keys == [], f"{lang} key drift: {keys}"


def test_available_langs_is_chinese_only():
    langs = available_langs()
    assert langs == ["zh"]


def test_title_with_an_ampersand_is_escaped():
    out = t("winner_dm.body", "en", title="R&D key", tier="Gold", creator="boss")
    assert "R&amp;D" in out
    assert "<" not in out.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace(
        "</i>", ""
    )


def test_escaping_is_applied_to_every_placeholder():
    out = t(
        "admin.list_row", "en", id=1, title="<script>", status="running", ends_at="soon", count=0
    )
    assert "&lt;script&gt;" in out
    assert "<script>" not in out


def test_old_language_codes_resolve_to_known_chinese_keys():
    for key in ("start.welcome", "join.ok", "giveaway.result_check", "fsm.confirm"):
        for lang in ("en", "zh"):
            assert t(key, lang) != key, f"{key} missing in {lang}"


def test_missing_key_returns_the_key_instead_of_raising():
    assert t("definitely.not.a.key", "en") == "definitely.not.a.key"


def test_bad_placeholder_falls_back_instead_of_raising():
    """A template with a stray brace must not take the handler down."""
    out = t("start.me_header", "en", count=1)
    assert isinstance(out, str) and out


def test_esc_escapes_html_but_leaves_raw_alone():
    assert esc("a & b") == "a &amp; b"
    assert esc("<b>") == "&lt;b&gt;"
    assert esc(None) == ""
    assert esc(7) == "7"
    assert esc(Raw("<a href=x>y</a>")) == "<a href=x>y</a>"


def test_esc_list_joins_and_escapes_each_item():
    assert esc_list(["@a", "@b"]) == "@a\n@b"
    assert esc_list(["<x>", "y"]) == "&lt;x&gt;\ny"
    assert esc_list([]) == ""


def test_user_mention_escapes_the_label_but_not_the_anchor():
    mention = user_mention(42, label="a & b")
    assert isinstance(mention, Raw)
    assert mention == '<a href="tg://user?id=42">a &amp; b</a>'


def test_user_mention_default_label_is_the_id():
    assert user_mention(42) == '<a href="tg://user?id=42">42</a>'


def test_load_locales_is_idempotent():
    load_locales("zh")
    load_locales("zh")
    assert t("start.welcome", "en")


def test_old_language_preferences_always_get_chinese():
    assert t("start.welcome", "en") == t("start.welcome", "zh")
    assert t("start.welcome", "unknown") == t("start.welcome", "zh")


def test_chinese_help_does_not_contain_unsupported_html_placeholder_tags():
    assert "<" not in t("start.help")
    assert "<" not in t("admin.usage_reroll")

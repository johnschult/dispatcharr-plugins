import json
from pathlib import Path

from titles import ORIGINAL, Fields, parse, rewrite, transform

NB = "  "
MANIFEST = json.loads((Path(__file__).parent.parent / "plugin.json").read_text(encoding="utf-8"))
DEFAULT_RULES = next(f["default"] for f in MANIFEST["fields"] if f["id"] == "rules")


def rules(text):
    sections, errors = parse(text)
    assert errors == []
    return sections


def test_parse_groups_rules_by_source_section():
    sections = rules("[Schedules Direct]\nswap_subtitle::^College Football$::\n# note\n\n[EPG Share]\nregex::a::b\n")
    assert list(sections) == ["Schedules Direct", "EPG Share"]
    assert [r.kind for r in sections["Schedules Direct"]] == ["swap_subtitle"]
    assert [r.kind for r in sections["EPG Share"]] == ["regex"]


def test_parse_reports_bad_lines_and_keeps_the_rest():
    sections, errors = parse("[SD]\nregex::(::x\nnonsense\nbogus::a::b\nswap_subtitle::^X$::\nregex::orphan::x\n")
    assert [r.kind for r in sections["SD"]] == ["swap_subtitle", "regex"]
    assert len(errors) == 3


def test_parse_rejects_rules_before_any_section():
    sections, errors = parse("regex::a::b\n[SD]\n")
    assert sections == {"SD": []}
    assert len(errors) == 1


def test_swap_subtitle_moves_the_event_into_the_title():
    r = rules("[SD]\nswap_subtitle::^College Football$::\n")["SD"]
    assert transform(Fields("College Football", "Ohio State at Michigan", "d"), r) == Fields(
        "Ohio State at Michigan", "College Football", "d"
    )


def test_swap_subtitle_needs_a_subtitle():
    r = rules("[SD]\nswap_subtitle::^College Football$::\n")["SD"]
    assert transform(Fields("College Football", None, "d"), r) == Fields("College Football", None, "d")


def test_team_sport_prefix_and_vs():
    r = rules("[SD]\nswap_subtitle::^College Football$::\nprefix_sport::^College Football$::🏈\n")["SD"]
    out = transform(Fields("College Football", "Ohio State at Michigan", None), r)
    assert out.title == f"🏈{NB}Ohio State vs Michigan"


def test_non_team_prefix_keeps_at():
    r = rules("[SD]\nprefix_sport::^NHRA Drag Racing$::🏁\n")["SD"]
    out = transform(Fields("Finals at Pomona", "NHRA Drag Racing", None), r)
    assert out.title == f"🏁{NB}Finals at Pomona"


def test_only_the_first_prefix_applies():
    r = rules("[SD]\nprefix_sport::Football::🏈\nprefix_sport::College::🎓\n")["SD"]
    assert transform(Fields("College Football", None, None), r).title == f"🏈{NB}College Football"


def test_regex_uses_dollar_groups():
    r = rules("[ES]\nregex::^([A-Z0-9].+?) hosts? (?:the )?([A-Z0-9].+)$::$2 vs $1\n")["ES"]
    assert transform(Fields("Duke hosts UNC", None, None), r).title == "UNC vs Duke"


def test_swap_description_takes_the_matchup_from_the_description():
    r = rules(
        "[ES]\n"
        r"swap_description::^College Football$::^(?:The )?(.+? (?:visits) .+?)(?: in | at | on |[.,])" "\n"
        r"regex::^([A-Z0-9].+?) visits? (?:the )?([A-Z0-9].+)$::$1 vs $2" "\n"
    )["ES"]
    out = transform(Fields("College Football", None, "Ohio State visits Michigan in a Big Ten clash."), r)
    assert out == Fields("Ohio State vs Michigan", "College Football", "a Big Ten clash.")


def test_no_matching_rule_changes_nothing():
    r = rules("[SD]\nswap_subtitle::^College Football$::\nprefix_sport::^College Football$::🏈\n")["SD"]
    f = Fields("Bluey", "The Pool", "Bingo swims.")
    assert transform(f, r) == f


def test_shipped_rules_parse_cleanly():
    sections, errors = parse(DEFAULT_RULES)
    assert errors == []
    assert set(sections) == {"Schedules Direct", "EPG Share"}


# rewrite(): what one stored program should become. Returns None when it is already right.

FOOTBALL = rules("[SD]\nswap_subtitle::^College Football$::\nprefix_sport::^College Football$::🏈\n")["SD"]
RAW = Fields("College Football", "Ohio State at Michigan", "Big game.")
DONE = Fields(f"🏈{NB}Ohio State vs Michigan", "College Football", "Big game.")


def test_rewrite_keeps_the_original_beside_the_new_fields():
    fields, props = rewrite(RAW, {"program_id": "EP1"}, FOOTBALL)
    assert fields == DONE
    assert props == {"program_id": "EP1", ORIGINAL: RAW._asdict()}


def test_rewrite_twice_changes_nothing():
    fields, props = rewrite(RAW, {}, FOOTBALL)
    assert rewrite(fields, props, FOOTBALL) is None


def test_rewrite_leaves_untouched_programs_alone():
    assert rewrite(Fields("Bluey", None, None), {}, FOOTBALL) is None


def test_rewrite_restores_when_the_rule_is_gone():
    fields, props = rewrite(RAW, {}, FOOTBALL)
    assert rewrite(fields, props, []) == (RAW, {})


def test_rewrite_follows_a_changed_rule_from_the_original():
    fields, props = rewrite(RAW, {}, FOOTBALL)
    trophy = rules("[SD]\nswap_subtitle::^College Football$::\nprefix_sport::^College Football$::🏆\n")["SD"]
    out_fields, _ = rewrite(fields, props, trophy)
    assert out_fields.title == f"🏆{NB}Ohio State at Michigan"


# Recording Piggyback marks a program it records: two no-break spaces and "🔴" after the title,
# "🔴 Recording" above the description, and the unmarked text in
# custom_properties["piggyback_original"]. Spelled out here, not imported, so a drift between the
# two plugins fails this test.
PIGGYBACK_DOT = "\u00a0\u00a0🔴"


def piggyback_marked(f):
    return (
        Fields(f.title + PIGGYBACK_DOT, f.sub_title, f"🔴 Recording\n{f.description}"),
        {"piggyback_original": {"title": f.title, "description": f.description}},
    )


def test_rewrite_under_a_recording_mark_keeps_the_mark():
    fields, props = rewrite(*piggyback_marked(RAW), FOOTBALL)
    assert fields == Fields(DONE.title + PIGGYBACK_DOT, DONE.sub_title, f"🔴 Recording\n{DONE.description}")
    assert props["piggyback_original"] == {"title": DONE.title, "description": DONE.description}
    assert props[ORIGINAL] == RAW._asdict()


def test_mark_added_after_rewrite_is_left_alone():
    fields, props = rewrite(RAW, {}, FOOTBALL)
    marked_fields, marked_props = piggyback_marked(fields)
    assert rewrite(marked_fields, {**props, **marked_props}, FOOTBALL) is None

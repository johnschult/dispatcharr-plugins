import time

from django.utils import timezone

from helpers import HeldLock, hours, in_thread, settings_for

KEY = "guide_titles"
RULES = """
[Schedules Direct]
swap_subtitle::^College Football$::
prefix_sport::^College Football$::🏈

[EPG Share]
prefix_sport::^NBA Basketball$::🏀
"""
FOOTBALL = "🏈  Ohio State vs Michigan"


def source_with(name, *programmes):
    """An EPG source with one entry holding (title, sub_title) programmes an hour apart."""
    from apps.epg.models import EPGData, EPGSource, ProgramData

    source = EPGSource.objects.create(name=name, source_type="xmltv")
    entry = EPGData.objects.create(name=f"{name} 1", tvg_id=f"{name}.1", epg_source=source)
    start = timezone.now()
    for n, (title, sub_title) in enumerate(programmes):
        ProgramData.objects.create(
            epg=entry, start_time=start + hours(n), end_time=start + hours(n + 1),
            title=title, sub_title=sub_title, description="Big game.",
        )
    return source


def titles(source):
    from apps.epg.models import ProgramData

    return list(ProgramData.objects.filter(epg__epg_source=source).order_by("start_time").values_list("title", flat=True))


def test_apply_rewrites_and_keeps_the_original(manager):
    from apps.epg.models import ProgramData

    settings_for(KEY, rules=RULES)
    sd = source_with("Schedules Direct", ("College Football", "Ohio State at Michigan"), ("Bluey", "The Pool"))

    result = manager.run_action(KEY, "apply")

    assert result["status"] == "ok", result
    assert titles(sd) == [FOOTBALL, "Bluey"]
    rewritten = ProgramData.objects.get(title=FOOTBALL)
    assert rewritten.sub_title == "College Football"
    assert rewritten.custom_properties["guide_titles_original"]["title"] == "College Football"
    assert "guide_titles_original" not in (ProgramData.objects.get(title="Bluey").custom_properties or {})


def test_second_apply_changes_nothing(manager):
    settings_for(KEY, rules=RULES)
    sd = source_with("Schedules Direct", ("College Football", "Ohio State at Michigan"))
    manager.run_action(KEY, "apply")

    result = manager.run_action(KEY, "apply")

    assert "changed 0 of 1" in result["message"]
    assert titles(sd) == [FOOTBALL]


def test_preview_changes_nothing(manager):
    settings_for(KEY, rules=RULES)
    sd = source_with("Schedules Direct", ("College Football", "Ohio State at Michigan"))

    result = manager.run_action(KEY, "preview")

    assert "would change 1 of 1" in result["message"]
    assert titles(sd) == ["College Football"]


def test_refresh_event_only_redoes_that_source(manager):
    settings_for(KEY, rules=RULES)
    sd = source_with("Schedules Direct", ("College Football", "Ohio State at Michigan"))
    share = source_with("EPG Share", ("NBA Basketball", None))

    manager.run_action(KEY, "apply", params={"event": "epg_refresh", "payload": {"source_name": "EPG Share"}})

    assert titles(sd) == ["College Football"]
    assert titles(share) == ["🏀  NBA Basketball"]


def test_apply_runs_after_every_epg_refresh(manager):
    assert (KEY, "apply") in set(manager.iter_actions_for_event("epg_refresh"))


def test_rule_removed_restores_the_original(manager):
    settings_for(KEY, rules=RULES)
    sd = source_with("Schedules Direct", ("College Football", "Ohio State at Michigan"))
    manager.run_action(KEY, "apply")

    settings_for(KEY, rules="[Schedules Direct]\n")
    manager.run_action(KEY, "apply")

    assert titles(sd) == ["College Football"]


def test_waits_for_the_shared_lock(manager):
    settings_for(KEY, rules=RULES)
    sd = source_with("Schedules Direct", ("College Football", "Ohio State at Michigan"))

    with HeldLock():
        t, _ = in_thread(lambda: manager.run_action(KEY, "apply"))
        time.sleep(1.5)
        assert t.is_alive(), "Guide Titles ran while the shared lock was held"
    t.join(15)

    assert not t.is_alive()
    assert titles(sd) == [FOOTBALL]

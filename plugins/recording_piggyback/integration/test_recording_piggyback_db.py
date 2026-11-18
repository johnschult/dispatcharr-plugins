import time

from django.utils import timezone

from helpers import HeldLock, hours, in_thread, settings_for

KEY = "recording_piggyback"
DOT = "\u00a0\u00a0🔴"  # what Piggyback puts after a recorded title


def channel(name, source=None, provider=()):
    """A channel with an EPG entry (when given a source) and provider streams."""
    from apps.channels.models import Channel, ChannelStream
    from apps.epg.models import EPGData

    epg = EPGData.objects.create(name=name, tvg_id=f"{name}.id", epg_source=source) if source else None
    ch = Channel.objects.create(name=name, epg_data=epg)
    for order, stream in enumerate(provider):
        ChannelStream.objects.create(channel=ch, stream=stream, order=order)
    return ch


def programme(ch, start, title, sub_title=None, description="Big game."):
    from apps.epg.models import ProgramData

    return ProgramData.objects.create(
        epg=ch.epg_data, start_time=start, end_time=start + hours(1),
        title=title, sub_title=sub_title, description=description,
    )


def recording(ch, start, end, status):
    from apps.channels.models import Recording

    return Recording.objects.create(channel=ch, start_time=start, end_time=end, custom_properties={"status": status})


def epg_source(name="Schedules Direct"):
    from apps.epg.models import EPGSource

    return EPGSource.objects.create(name=name, source_type="xmltv")


def refreshed(p):
    p.refresh_from_db()
    return p.title, p.description


def test_scheduled_recording_marks_its_programme(manager):
    ch = channel("ESPN", epg_source())
    start = timezone.now() + hours(2)
    show = programme(ch, start, "SportsCenter")
    before = programme(ch, start - hours(1), "Pregame")
    recording(ch, start - hours(0.1), start + hours(1.1), "scheduled")

    result = manager.run_action(KEY, "sync")

    assert result["status"] == "ok", result
    assert refreshed(show) == ("SportsCenter" + DOT, "🔴 Recording\nBig game.")
    assert refreshed(before) == ("Pregame", "Big game.")


def test_mark_comes_off_when_the_recording_is_cancelled(manager):
    ch = channel("ESPN", epg_source())
    start = timezone.now() + hours(2)
    show = programme(ch, start, "SportsCenter")
    rec = recording(ch, start, start + hours(1), "scheduled")
    manager.run_action(KEY, "sync")

    rec.custom_properties = {"status": "cancelled"}
    rec.save()
    manager.run_action(KEY, "sync")

    assert refreshed(show) == ("SportsCenter", "Big game.")
    show.refresh_from_db()
    assert "piggyback_original" not in (show.custom_properties or {})


def test_mark_setting_off_restores_marked_programmes(manager):
    ch = channel("ESPN", epg_source())
    start = timezone.now() + hours(2)
    show = programme(ch, start, "SportsCenter")
    recording(ch, start, start + hours(1), "scheduled")
    manager.run_action(KEY, "sync")

    settings_for(KEY, mark_guide=False)
    manager.run_action(KEY, "sync")

    assert refreshed(show) == ("SportsCenter", "Big game.")


def provider_streams(*names):
    """Streams from a provider account. Piggyback's own streams are custom ones, which Dispatcharr
    files under its built-in custom account (made by a migration, so gone from a flushed test DB)."""
    from apps.channels.models import Stream
    from apps.m3u.models import M3UAccount, CUSTOM_M3U_ACCOUNT_NAME

    M3UAccount.objects.get_or_create(name=CUSTOM_M3U_ACCOUNT_NAME, defaults={"locked": True})
    provider = M3UAccount.objects.create(name="Provider")
    return [Stream.objects.create(name=n, url=f"http://provider.example/{n}.ts", m3u_account=provider) for n in names]


def test_channel_sharing_the_feed_rides_the_recording_then_lets_go(manager):
    from apps.channels.models import ChannelStream, Stream

    feed, other_feed, cnn = provider_streams("espn", "espn2", "cnn")
    recorded = channel("ESPN", provider=[feed])
    sharing = channel("ESPN (alt)", provider=[other_feed, feed])
    unrelated = channel("CNN", provider=[cnn])
    now = timezone.now()
    rec = recording(recorded, now - hours(0.5), now + hours(1), "recording")

    manager.run_action(KEY, "sync")

    links = list(ChannelStream.objects.filter(channel=sharing).order_by("order").select_related("stream"))
    assert links[0].stream.name == "Piggyback → ESPN"
    assert links[0].stream.url.endswith(f"/proxy/ts/stream/{recorded.uuid}")
    assert [l.stream_id for l in links[1:]] == [other_feed.id, feed.id]
    assert not ChannelStream.objects.filter(channel=unrelated, stream__is_custom=True).exists()
    assert not ChannelStream.objects.filter(channel=recorded, stream__is_custom=True).exists()

    rec.custom_properties = {"status": "completed"}
    rec.save()
    manager.run_action(KEY, "sync")

    assert not Stream.objects.filter(name__startswith="Piggyback → ").exists()
    assert list(ChannelStream.objects.filter(channel=sharing).order_by("order").values_list("stream_id", flat=True)) == [
        other_feed.id, feed.id,
    ]


def test_mark_sits_on_top_of_guide_titles(manager):
    settings_for("guide_titles", rules="[Schedules Direct]\nswap_subtitle::^College Football$::\n"
                 "prefix_sport::^College Football$::🏈\n")
    ch = channel("ESPN", epg_source())
    start = timezone.now() + hours(2)
    game = programme(ch, start, "College Football", "Ohio State at Michigan")
    rec = recording(ch, start, start + hours(1), "scheduled")
    rewritten = "🏈  Ohio State vs Michigan"

    manager.run_action("guide_titles", "apply")
    manager.run_action(KEY, "sync")
    assert refreshed(game)[0] == rewritten + DOT

    # A refresh runs both again, in either order; the mark and the rewrite both stay
    manager.run_action("guide_titles", "apply")
    manager.run_action(KEY, "sync")
    assert refreshed(game)[0] == rewritten + DOT

    rec.custom_properties = {"status": "cancelled"}
    rec.save()
    manager.run_action(KEY, "sync")
    assert refreshed(game) == (rewritten, "Big game.")


def test_guide_titles_on_an_already_marked_programme(manager):
    ch = channel("ESPN", epg_source())
    start = timezone.now() + hours(2)
    game = programme(ch, start, "College Football", "Ohio State at Michigan")
    rec = recording(ch, start, start + hours(1), "scheduled")
    manager.run_action(KEY, "sync")

    settings_for("guide_titles", rules="[Schedules Direct]\nswap_subtitle::^College Football$::\n"
                 "prefix_sport::^College Football$::🏈\n")
    manager.run_action("guide_titles", "apply")
    assert refreshed(game)[0] == "🏈  Ohio State vs Michigan" + DOT

    rec.custom_properties = {"status": "cancelled"}
    rec.save()
    manager.run_action(KEY, "sync")
    assert refreshed(game)[0] == "🏈  Ohio State vs Michigan"


def test_sync_runs_on_recording_and_refresh_events(manager):
    for event in ("recording_start", "recording_end", "channel_start", "m3u_refresh", "epg_refresh"):
        assert (KEY, "sync") in set(manager.iter_actions_for_event(event)), event


def test_waits_for_the_shared_lock(manager):
    ch = channel("ESPN", epg_source())
    start = timezone.now() + hours(2)
    show = programme(ch, start, "SportsCenter")
    recording(ch, start, start + hours(1), "scheduled")

    with HeldLock():
        t, _ = in_thread(lambda: manager.run_action(KEY, "sync"))
        time.sleep(1.5)
        assert t.is_alive(), "Piggyback synced while the shared lock was held"
    t.join(15)

    assert not t.is_alive()
    assert refreshed(show)[0] == "SportsCenter" + DOT

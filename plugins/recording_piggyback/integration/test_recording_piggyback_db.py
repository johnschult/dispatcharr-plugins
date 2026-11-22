import time

from django.utils import timezone

from helpers import HeldLock, hours, in_thread, settings_for

KEY = "recording_piggyback"
REC = "\u0280\u1d07\u1d04\u00a0"  # what Piggyback puts before a recorded title
REC_LINE = "\u0280\u1d07\u1d04 · Recording"


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
    assert refreshed(show) == (REC + "SportsCenter", REC_LINE + "\nBig game.")
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
    assert refreshed(game)[0] == REC + rewritten

    # A refresh runs both again, in either order; the mark and the rewrite both stay
    manager.run_action("guide_titles", "apply")
    manager.run_action(KEY, "sync")
    assert refreshed(game)[0] == REC + rewritten

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
    manager.run_action(KEY, "sync")  # Guide Titles shows its rewrite unmarked; Piggyback marks it again
    assert refreshed(game)[0] == REC + "🏈  Ohio State vs Michigan"

    rec.custom_properties = {"status": "cancelled"}
    rec.save()
    manager.run_action(KEY, "sync")
    assert refreshed(game)[0] == "🏈  Ohio State vs Michigan"


def test_sync_runs_on_recording_channel_and_refresh_events(manager):
    # Sportarr records as an ordinary client: its start and end are a channel starting and stopping
    for event in ("recording_start", "recording_end", "channel_start", "channel_stop", "m3u_refresh", "epg_refresh"):
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
    assert refreshed(show)[0] == REC + "SportsCenter"


class FakeSportarr:
    """Sportarr's two API endpoints, on a local port, answering only with the right API key."""

    def __init__(self, key="sportarr-key"):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.key, self.recordings, self.channels, self.requests = key, [], [], 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake.requests += 1
                body = {"/api/dvr/recordings": fake.recordings, "/api/iptv/channels": fake.channels}.get(self.path)
                code = 401 if self.headers.get("X-Api-Key") != fake.key else 200 if body is not None else 404
                data = json.dumps(body if code == 200 else {"error": code}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def channel(self, sportarr_id, ch):
        self.channels.append({"id": sportarr_id, "name": ch.name,
                              "streamUrl": f"http://192.168.68.115:9191/proxy/ts/stream/{ch.uuid}"})

    def record(self, sportarr_id, start, end, status="Scheduled"):
        rec = {"id": len(self.recordings) + 1, "channelId": sportarr_id, "status": status,
               "scheduledStart": start.isoformat(), "scheduledEnd": end.isoformat(), "prePadding": 0, "postPadding": 0}
        self.recordings.append(rec)
        return rec

    def close(self):
        self.server.shutdown()


import pytest


@pytest.fixture
def sportarr(manager):
    fake = FakeSportarr()
    settings_for(KEY, use_sportarr=True, sportarr_url=fake.url, sportarr_api_key=fake.key)
    yield fake
    fake.close()


def test_sportarr_recording_marks_its_programme(manager, sportarr):
    ch = channel("Big Ten Network", epg_source())
    start = timezone.now() + hours(2)
    game = programme(ch, start, "Purdue vs Oregon")
    sportarr.channel(181, ch)
    rec = sportarr.record(181, start, start + hours(1))

    result = manager.run_action(KEY, "sync")

    assert result["status"] == "ok", result
    assert refreshed(game) == (REC + "Purdue vs Oregon", REC_LINE + "\nBig game.")

    rec["status"] = "Cancelled"
    manager.run_action(KEY, "sync")
    assert refreshed(game) == ("Purdue vs Oregon", "Big game.")


def test_channel_sharing_the_feed_rides_a_sportarr_recording(manager, sportarr):
    from apps.channels.models import ChannelStream, Stream

    feed, other_feed = provider_streams("btn", "btn-backup")
    recorded = channel("NCAA W Volleyball | Purdue", provider=[feed])
    sharing = channel("Big Ten Network", provider=[other_feed, feed])
    sportarr.channel(198, recorded)
    now = timezone.now()
    rec = sportarr.record(198, now - hours(0.5), now + hours(1), status="Recording")

    manager.run_action(KEY, "sync")

    first = ChannelStream.objects.filter(channel=sharing).order_by("order").select_related("stream").first()
    assert first.stream.name == "Piggyback → NCAA W Volleyball | Purdue"
    assert first.stream.url.endswith(f"/proxy/ts/stream/{recorded.uuid}")

    rec["status"] = "Completed"
    manager.run_action(KEY, "sync")
    assert not Stream.objects.filter(name__startswith="Piggyback → ").exists()


def test_sportarr_down_still_syncs_dispatcharr_recordings(manager):
    settings_for(KEY, use_sportarr=True, sportarr_url="http://127.0.0.1:9", sportarr_api_key="x")
    ch = channel("ESPN", epg_source())
    start = timezone.now() + hours(2)
    show = programme(ch, start, "SportsCenter")
    recording(ch, start, start + hours(1), "scheduled")

    result = manager.run_action(KEY, "sync")

    assert result["status"] == "ok", result
    assert "Sportarr" in result["message"]
    assert refreshed(show)[0] == REC + "SportsCenter"


def test_wrong_sportarr_key_is_reported_and_changes_nothing(manager, sportarr):
    settings_for(KEY, sportarr_api_key="wrong")
    ch = channel("Big Ten Network", epg_source())
    start = timezone.now() + hours(2)
    game = programme(ch, start, "Purdue vs Oregon")
    sportarr.channel(181, ch)
    sportarr.record(181, start, start + hours(1))

    result = manager.run_action(KEY, "sync")

    assert result["status"] == "ok", result
    assert "401" in result["message"]
    assert refreshed(game)[0] == "Purdue vs Oregon"


def test_sportarr_settings_sit_behind_a_switch_in_their_own_section(manager):
    fields = {f["id"]: f for f in manager.get_plugin(KEY).fields}
    assert fields["use_sportarr"]["type"] == "boolean" and fields["use_sportarr"]["default"] is False
    assert "section" not in fields["use_sportarr"], "the switch stays visible"
    assert fields["sportarr"]["type"] == "section" and fields["sportarr"]["collapsed"] is True
    assert fields["sportarr_url"]["section"] == fields["sportarr_api_key"]["section"] == "sportarr"
    assert fields["sportarr_api_key"]["input_type"] == "password"


def test_switch_off_never_calls_sportarr(manager, sportarr):
    settings_for(KEY, use_sportarr=False)
    ch = channel("Big Ten Network", epg_source())
    start = timezone.now() + hours(2)
    game = programme(ch, start, "Purdue vs Oregon")
    sportarr.channel(181, ch)
    sportarr.record(181, start, start + hours(1))

    result = manager.run_action(KEY, "sync")

    assert sportarr.requests == 0
    assert "Sportarr" not in result["message"]
    assert refreshed(game)[0] == "Purdue vs Oregon"


def test_switch_on_without_an_address_says_so(manager):
    settings_for(KEY, use_sportarr=True, sportarr_url="", sportarr_api_key="")
    for action in ("sync", "test_sportarr"):
        result = manager.run_action(KEY, action)
        assert "needs its address and API key" in result["message"], (action, result)


def test_test_sportarr_counts_recordings_and_names_what_cannot_be_matched(manager, sportarr):
    from apps.channels.models import Channel

    btn, gone = channel("Big Ten Network"), channel("Old Pacers")
    sportarr.channel(181, btn)
    sportarr.channel(197, gone)
    sportarr.channels.append({"id": 7, "name": "Somewhere Else", "streamUrl": "http://provider.example/1.ts"})
    start = timezone.now() + hours(2)
    for ch in (181, 181, 197, 7, 999):
        sportarr.record(ch, start, start + hours(1))
    sportarr.record(181, start, start + hours(1), status="Completed")
    Channel.objects.filter(id=gone.id).delete()

    result = manager.run_action(KEY, "test_sportarr")

    assert result["status"] == "ok", result
    message = result["message"]
    assert "Sportarr answered: 5 recording(s) scheduled or running, 2 on Dispatcharr channels." in message
    assert "Old Pacers: Dispatcharr has no such channel" in message
    assert "Somewhere Else: its stream isn't from Dispatcharr" in message
    assert "Sportarr channel 999: Sportarr no longer lists the channel" in message


def test_test_sportarr_reports_a_refused_key_and_an_unreachable_address(manager, sportarr):
    settings_for(KEY, sportarr_api_key="wrong")
    assert "refused the API key" in manager.run_action(KEY, "test_sportarr")["message"]

    settings_for(KEY, sportarr_url="http://127.0.0.1:9", sportarr_api_key=sportarr.key)
    assert "Could not reach Sportarr at http://127.0.0.1:9" in manager.run_action(KEY, "test_sportarr")["message"]


def test_status_includes_what_sportarr_is_recording(manager, sportarr):
    ch = channel("Big Ten Network")
    sportarr.channel(181, ch)
    now = timezone.now()
    sportarr.record(181, now - hours(0.5), now + hours(1), status="Recording")

    message = manager.run_action(KEY, "status")["message"]

    assert "Big Ten Network" in message and "(Sportarr)" in message


def test_programme_marked_the_old_way_gets_the_new_mark(manager):
    from apps.epg.models import ProgramData

    ch = channel("ESPN", epg_source())
    start = timezone.now() + hours(2)
    show = programme(ch, start, "SportsCenter\u00a0\u00a0🔴", description="🔴 Recording\nBig game.")
    ProgramData.objects.filter(id=show.id).update(
        custom_properties={"piggyback_original": {"title": "SportsCenter", "description": "Big game."}})
    recording(ch, start, start + hours(1), "scheduled")

    manager.run_action(KEY, "sync")

    assert refreshed(show) == (REC + "SportsCenter", REC_LINE + "\nBig game.")


def test_changing_the_mark_remarks_every_marked_programme(manager):
    ch = channel("ESPN", epg_source())
    start = timezone.now() + hours(2)
    show = programme(ch, start, "SportsCenter")
    recording(ch, start, start + hours(1), "scheduled")
    manager.run_action(KEY, "sync")

    settings_for(KEY, mark="⏺")
    manager.run_action(KEY, "sync")

    assert refreshed(show) == ("⏺\u00a0SportsCenter", "⏺ · Recording\nBig game.")


def test_mark_is_a_setting_defaulting_to_small_caps_rec(manager):
    fields = {f["id"]: f for f in manager.get_plugin(KEY).fields}
    assert fields["mark"]["type"] == "string" and fields["mark"]["default"] == "\u0280\u1d07\u1d04"

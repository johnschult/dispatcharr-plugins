from datetime import datetime, timedelta, timezone

from piggyback import (
    DESCRIPTION_LINE,
    PREFIX,
    TITLE_MARK,
    Recorded,
    is_active,
    loopback_name,
    loopback_target,
    marked,
    overlaps,
    plan,
)

T0 = datetime(2026, 11, 18, 19, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
UUID_A = "3BD5E6AE-0479-47D3-9DEF-081DF6BCAB31"
UUID_B = "11111111-2222-3333-4444-555555555555"


# is_active

def test_recording_status_counts_whenever():
    assert is_active("recording", T0 + 5 * H, T0 + 6 * H, T0)


def test_finished_statuses_never_count():
    for status in ("completed", "stopped", "interrupted", "failed", "cancelled", "canceled", "error"):
        assert not is_active(status, T0 - H, T0 + H, T0)


def test_other_statuses_count_inside_their_window():
    assert is_active("scheduled", T0 - H, T0 + H, T0)
    assert is_active(None, T0, T0 + H, T0)
    assert not is_active("scheduled", T0 + H, T0 + 2 * H, T0)
    assert not is_active("scheduled", T0 - 2 * H, T0 - H, T0)


# marked

def test_mark_puts_the_dot_after_the_title_and_a_line_above_the_description():
    assert marked("🏈  Ohio State vs Michigan", "Big game.") == (
        "🏈  Ohio State vs Michigan" + TITLE_MARK,
        f"{DESCRIPTION_LINE}\nBig game.",
    )


def test_mark_with_no_description():
    assert marked("News", None) == ("News" + TITLE_MARK, DESCRIPTION_LINE)
    assert marked("News", "") == ("News" + TITLE_MARK, DESCRIPTION_LINE)


def test_mark_twice_changes_nothing():
    once = marked("News", "Today.")
    assert marked(*once) == once


def test_mark_trims_trailing_space_before_the_dot():
    assert marked("News  ", None)[0] == "News" + TITLE_MARK


def test_mark_with_no_title():
    assert marked(None, None) == (TITLE_MARK, DESCRIPTION_LINE)


# overlaps

def test_programme_inside_the_recording_overlaps():
    assert overlaps(T0, T0 + H, T0 - timedelta(minutes=5), T0 + H + timedelta(minutes=30))


def test_pregame_clipped_by_the_early_start_does_not():
    # Recording starts 5 minutes early; the hour-long pregame before it is barely inside
    assert not overlaps(T0 - H, T0, T0 - timedelta(minutes=5), T0 + 3 * H)


def test_exactly_half_inside_overlaps():
    assert overlaps(T0, T0 + H, T0 + timedelta(minutes=30), T0 + 2 * H)


def test_zero_length_programme_never_overlaps():
    assert not overlaps(T0, T0, T0 - H, T0 + H)


# loopback streams

def test_loopback_name_is_prefixed_and_fits_the_column():
    assert loopback_name("ESPN") == f"{PREFIX}ESPN"
    assert len(loopback_name("x" * 400)) == 255


def test_loopback_target_reads_the_uuid_lowercased():
    assert loopback_target(f"http://dispatcharr:9191/proxy/ts/stream/{UUID_A}") == UUID_A.lower()


def test_loopback_target_of_anything_else_is_none():
    assert loopback_target("http://provider.example/live/123.ts") is None
    assert loopback_target(None) is None


# plan

def rec(channel_id, uuid, streams):
    return Recorded(channel_id, uuid, f"ch{channel_id}", frozenset(streams))


def test_channel_sharing_a_stream_gets_a_piggyback():
    r = rec(1, UUID_A, {10})
    p = plan([r], {1: {10}, 2: {10, 20}, 3: {30}}, {})
    assert p.add == ((2, r),)
    assert p.remove == ()


def test_existing_piggyback_on_the_right_recording_is_kept():
    r = rec(1, UUID_A, {10})
    p = plan([r], {1: {10}, 2: {10}}, {2: [(500, UUID_A.lower())]})
    assert p.add == ()
    assert p.remove == ()


def test_piggybacks_go_when_the_recording_ends():
    p = plan([], {2: {10}}, {2: [(500, UUID_A.lower())]})
    assert p.remove == (500,)


def test_piggyback_on_the_wrong_recording_is_swapped():
    r = rec(1, UUID_A, {10})
    p = plan([r], {1: {10}, 2: {10}}, {2: [(500, UUID_B.lower())]})
    assert p.add == ((2, r),)
    assert p.remove == (500,)


def test_duplicate_piggybacks_keep_one():
    r = rec(1, UUID_A, {10})
    p = plan([r], {1: {10}, 2: {10}}, {2: [(500, UUID_A.lower()), (501, UUID_A.lower())]})
    assert p.add == ()
    assert p.remove == (501,)


def test_a_recorded_channel_never_rides_another_recording():
    a, b = rec(1, UUID_A, {10}), rec(2, UUID_B, {10})
    p = plan([a, b], {1: {10}, 2: {10}}, {})
    assert p.add == ()


def test_lowest_channel_id_wins_when_two_record_the_same_feed():
    a, b = rec(1, UUID_A, {10}), rec(5, UUID_B, {10})
    p = plan([b, a], {1: {10}, 5: {10}, 7: {10}}, {})
    assert p.add == ((7, a),)


def test_recording_without_provider_streams_carries_no_one():
    p = plan([rec(1, UUID_A, set())], {1: set(), 2: {10}}, {})
    assert p.add == ()


# Sportarr's recordings

from piggyback import Window, from_sportarr

SPORTARR_CHANNELS = [
    {"id": 181, "name": "Big Ten Network", "streamUrl": f"http://192.168.68.115:9191/proxy/ts/stream/{UUID_A}"},
    {"id": 198, "name": "NCAA W Volleyball | Purdue", "streamUrl": f"http://dispatcharr:9191/proxy/ts/stream/{UUID_B}"},
    {"id": 7, "name": "Elsewhere", "streamUrl": "http://provider.example/live/1.ts"},
]


def sportarr(id=1, channel=181, status="Scheduled", start="2026-11-18T19:00:00Z", end="2026-11-18T21:00:00Z",
             pre=5, post=30):
    return {"id": id, "channelId": channel, "status": status, "scheduledStart": start, "scheduledEnd": end,
            "prePadding": pre, "postPadding": post}


def test_sportarr_recording_becomes_its_dispatcharr_channel_and_padded_window():
    assert from_sportarr([sportarr()], SPORTARR_CHANNELS) == [
        Window(uuid=UUID_A.lower(), start=T0 - timedelta(minutes=5), end=T0 + 2 * H + timedelta(minutes=30),
               status="scheduled"),
    ]


def test_sportarr_channel_found_by_its_stream_url_not_its_name():
    (w,) = from_sportarr([sportarr(channel=198)], SPORTARR_CHANNELS)
    assert w.uuid == UUID_B.lower()


def test_sportarr_recording_on_a_channel_not_from_dispatcharr_is_skipped():
    assert from_sportarr([sportarr(channel=7), sportarr(id=2, channel=999)], SPORTARR_CHANNELS) == []


def test_finished_sportarr_recordings_are_skipped():
    done = ["Completed", "Imported", "Importing", "Ignored", "Failed", "Cancelled", "Stopped"]
    assert from_sportarr([sportarr(id=i, status=s) for i, s in enumerate(done)], SPORTARR_CHANNELS) == []


def test_running_sportarr_recording_counts_as_recording():
    (w,) = from_sportarr([sportarr(status="Recording")], SPORTARR_CHANNELS)
    assert w.status == "recording" and is_active(w.status, w.start, w.end, T0 + 10 * H)


def test_sportarr_window_is_capped():
    # Sportarr sometimes stores a football game's end a day late; uncapped, the red dot would cover
    # the channel's next 24 hours of programmes
    (w,) = from_sportarr([sportarr(end="2026-11-19T19:00:00Z", post=0)], SPORTARR_CHANNELS)
    assert w.end - w.start == timedelta(hours=8)


def test_sportarr_recording_without_padding_or_with_bad_times():
    bad = sportarr(id=2, start="soon", end=None)
    (w,) = from_sportarr([{**sportarr(), "prePadding": None, "postPadding": None}, bad], SPORTARR_CHANNELS)
    assert (w.start, w.end) == (T0, T0 + 2 * H)


# Why a Sportarr recording can't be matched (for "Test Sportarr")

from piggyback import NOT_DISPATCHARR, NO_SUCH_SPORTARR_CHANNEL, unmatched


def test_every_recording_matched_means_nothing_to_report():
    assert unmatched([sportarr(), sportarr(id=2, channel=198)], SPORTARR_CHANNELS) == []


def test_channel_whose_stream_is_not_from_dispatcharr_is_named():
    assert unmatched([sportarr(channel=7)], SPORTARR_CHANNELS) == [("Elsewhere", NOT_DISPATCHARR)]


def test_recording_on_a_channel_sportarr_no_longer_lists():
    assert unmatched([sportarr(channel=999)], SPORTARR_CHANNELS) == [("Sportarr channel 999", NO_SUCH_SPORTARR_CHANNEL)]


def test_each_unmatched_channel_is_named_once_and_finished_recordings_are_ignored():
    recs = [sportarr(id=1, channel=7), sportarr(id=2, channel=7), sportarr(id=3, channel=999, status="Completed")]
    assert unmatched(recs, SPORTARR_CHANNELS) == [("Elsewhere", NOT_DISPATCHARR)]

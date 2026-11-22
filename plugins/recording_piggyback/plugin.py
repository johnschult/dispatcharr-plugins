"""Recording Piggyback: other channels ride a recording's connection; recorded programs get a red dot.

All state lives in Dispatcharr's own tables, so every run works it out again from scratch:
piggyback streams are custom streams named "Piggyback → <channel>", and a marked program keeps
its original title and description in custom_properties["piggyback_original"].
"""
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

try:
    from .piggyback import (NO_SUCH_DISPATCHARR_CHANNEL, PREFIX, SPORTARR_DONE, TERMINAL, Recorded, from_sportarr,
                            is_active, loopback_name, loopback_target, marked, overlaps, plan, unmatched)
except ImportError:  # loaded as a top-level module
    from piggyback import (NO_SUCH_DISPATCHARR_CHANNEL, PREFIX, SPORTARR_DONE, TERMINAL, Recorded, from_sportarr,
                           is_active, loopback_name, loopback_target, marked, overlaps, plan, unmatched)

_MANIFEST = json.loads((Path(__file__).parent / "plugin.json").read_text(encoding="utf-8"))
_DEFAULTS = {f["id"]: f.get("default") for f in _MANIFEST["fields"]}
ORIGINAL = "piggyback_original"
# pg advisory lock shared with Guide Titles: both rewrite programme titles and both run on every
# EPG refresh, so they take turns rather than overwrite each other's edits
LOCK_KEY = 7431002
SLACK = timedelta(minutes=2)
MARK_AHEAD = timedelta(days=7)  # how far ahead scheduled recordings get the guide mark
CHANNEL_EVENT_MIN_GAP = 20  # seconds; channel events fire often, and a sync is a few queries
SPORTARR_TIMEOUT = 5  # seconds per request; a sync waits on it
log = logging.getLogger("plugins.recording_piggyback")
# Celery workers start with the root logger at WARNING, and every event runs us in one,
# so without this a sync's summary line never reaches the log
log.setLevel(logging.INFO)
_last_run = {"at": 0.0}


def _sportarr_get(settings, path):
    url = (settings.get("sportarr_url") or "").rstrip("/") + path
    req = urllib.request.Request(url, headers={"X-Api-Key": settings.get("sportarr_api_key") or "",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=SPORTARR_TIMEOUT) as resp:
        return json.load(resp)


NEEDS_SETTINGS = "Sportarr needs its address and API key."


def _read_sportarr(settings):
    """(recordings, channels) from Sportarr's API, or (None, why not) when it can't be read."""
    url = (settings.get("sportarr_url") or "").rstrip("/")
    if not url or not settings.get("sportarr_api_key"):
        return None, NEEDS_SETTINGS
    try:
        return (_sportarr_get(settings, "/api/dvr/recordings"), _sportarr_get(settings, "/api/iptv/channels")), ""
    except urllib.error.HTTPError as e:
        why = "Sportarr refused the API key (401)." if e.code == 401 else f"Sportarr answered {e.code} at {url}."
    except Exception as e:
        why = f"Could not reach Sportarr at {url}: {type(e).__name__}."
    log.warning("Recording Piggyback: %s", why)
    return None, why


def _dispatcharr_channels(uuids):
    from apps.channels.models import Channel
    return {str(c.uuid).lower(): c for c in Channel.objects.filter(uuid__in=uuids)}


def _sportarr_recordings(settings):
    """Sportarr's unfinished recordings, shaped like Dispatcharr's (channel, start_time, end_time,
    status), plus a note for the sync message. Sportarr being down never stops a sync."""
    if not settings.get("use_sportarr"):
        return [], ""
    data, why = _read_sportarr(settings)
    if data is None:
        return [], f" {why}"
    windows = from_sportarr(*data)
    channels = _dispatcharr_channels({w.uuid for w in windows})
    recs = [SimpleNamespace(channel=channels[w.uuid], channel_id=channels[w.uuid].id, start_time=w.start,
                            end_time=w.end, status=w.status, source="Sportarr", show_start=w.show_start,
                            show_end=w.show_end)
            for w in windows if w.uuid in channels]
    return recs, f" Sportarr: {len(recs)} scheduled or recording."


def test_sportarr(settings):
    if not settings.get("use_sportarr"):
        return "Sportarr is switched off."
    data, why = _read_sportarr(settings)
    if data is None:
        return why
    recordings, sportarr_channels = data
    windows = from_sportarr(recordings, sportarr_channels)
    found = _dispatcharr_channels({w.uuid for w in windows})
    problems = unmatched(recordings, sportarr_channels)
    names = {uuid: ch.get("name") for ch in sportarr_channels if (uuid := loopback_target(ch.get("streamUrl")))}
    for w in windows:
        problem = (names.get(w.uuid) or w.uuid, NO_SUCH_DISPATCHARR_CHANNEL)
        if w.uuid not in found and problem not in problems:
            problems.append(problem)
    matched = sum(1 for w in windows if w.uuid in found)
    total = sum(1 for r in recordings if (r.get("status") or "").lower() not in SPORTARR_DONE)
    lines = [f"Sportarr answered: {total} recording(s) scheduled or running, {matched} on Dispatcharr channels."]
    lines += [f"Not matched: {name}: {reason}" for name, reason in problems]
    return "\n".join(lines)


def _active_recordings(now, sportarr=()):
    from apps.channels.models import Recording
    rows = Recording.objects.filter(start_time__lte=now + SLACK, end_time__gte=now - SLACK).select_related("channel")
    ours = [r for r in rows if is_active((r.custom_properties or {}).get("status"), r.start_time, r.end_time, now)]
    return ours + [r for r in sportarr if is_active(r.status, r.start_time, r.end_time, now)]


def _scheduled_recordings(now, sportarr=()):
    """Recordings that are running or still to come: marked in the guide ahead of time, because
    apps like Zaply only reload the guide about once an hour."""
    from apps.channels.models import Recording
    rows = Recording.objects.filter(end_time__gte=now - SLACK, start_time__lte=now + MARK_AHEAD).select_related("channel")
    ours = [r for r in rows if (r.custom_properties or {}).get("status") not in TERMINAL]
    return ours + [r for r in sportarr if r.end_time >= now - SLACK and r.start_time <= now + MARK_AHEAD]


def _skip_groups(settings):
    return {g.strip() for g in (settings.get("skip_groups") or "").split(",") if g.strip()}


def _provider_streams(skip_groups=(), keep=()):
    """{channel id: provider stream ids}, leaving out channels in skipped groups (except `keep`,
    the recorded channels themselves, whose streams are still needed)."""
    from apps.channels.models import ChannelStream
    out = {}
    rows = ChannelStream.objects.filter(stream__is_custom=False).values_list("channel_id", "stream_id", "channel__channel_group__name")
    for channel_id, stream_id, group in rows:
        if group in skip_groups and channel_id not in keep:
            continue
        out.setdefault(channel_id, set()).add(stream_id)
    return out


def _piggybacks():
    """{channel id: [(stream id, target uuid)]} plus piggyback streams on no channel at all."""
    from apps.channels.models import ChannelStream, Stream
    streams = dict(Stream.objects.filter(is_custom=True, name__startswith=PREFIX).values_list("id", "url"))
    linked, out = set(), {}
    for channel_id, stream_id in ChannelStream.objects.filter(stream_id__in=streams).values_list("channel_id", "stream_id"):
        linked.add(stream_id)
        out.setdefault(channel_id, []).append((stream_id, loopback_target(streams[stream_id])))
    return out, [sid for sid in streams if sid not in linked]


def _apply(p, base_url):
    from django.db.models import F
    from apps.channels.models import ChannelStream, Stream
    if p.remove:
        ChannelStream.objects.filter(stream_id__in=p.remove).delete()
        Stream.objects.filter(id__in=p.remove).delete()
    for channel_id, rec in p.add:
        stream = Stream.objects.create(
            name=loopback_name(rec.name), url=f"{base_url}/proxy/ts/stream/{rec.uuid}", is_custom=True,
        )
        # Positions can't go below 0, so make room at the front; the channel's own order is kept
        ChannelStream.objects.filter(channel_id=channel_id).update(order=F("order") + 1)
        ChannelStream.objects.create(channel_id=channel_id, stream=stream, order=0)


def _mark_guide(recordings, enabled, mark):
    """Mark programs overlapping each recording on the recorded channel; restore the rest.
    Returns how many programs changed."""
    from apps.epg.models import ProgramData
    want = set()
    if enabled:
        for r in recordings:
            if not r.channel.epg_data_id:
                continue
            # Sportarr's padding runs into the shows either side of the game; mark the game itself
            start, end = getattr(r, "show_start", r.start_time), getattr(r, "show_end", r.end_time)
            programs = ProgramData.objects.filter(
                epg_id=r.channel.epg_data_id, start_time__lt=end, end_time__gt=start,
            )
            for prog in programs:
                if overlaps(prog.start_time, prog.end_time, start, end):
                    want.add(prog.id)
    changed = 0
    for prog in ProgramData.objects.filter(id__in=want):
        props = dict(prog.custom_properties or {})
        # Marked from the kept original every time, so a mark from an older version, or a title
        # Guide Titles has just rewritten (it updates our copy and shows its text unmarked), gets
        # the current mark back
        original = props.get(ORIGINAL) or {"title": prog.title, "description": prog.description}
        title, description = marked(original.get("title"), original.get("description"), mark)
        if ORIGINAL in props and (prog.title, prog.description) == (title, description):
            continue
        props[ORIGINAL] = original
        prog.title, prog.description = title, description
        prog.custom_properties = props
        prog.save(update_fields=["title", "description", "custom_properties"])
        changed += 1
    for prog in ProgramData.objects.filter(custom_properties__has_key=ORIGINAL).exclude(id__in=want):
        props = dict(prog.custom_properties or {})
        original = props.pop(ORIGINAL)
        prog.title, prog.description = original.get("title", prog.title), original.get("description", prog.description)
        prog.custom_properties = props
        prog.save(update_fields=["title", "description", "custom_properties"])
        changed += 1
    return changed


def sync(settings):
    from django.db import connection, transaction
    from django.utils import timezone
    from apps.channels.tasks import get_dvr_stream_base_url

    sportarr, note = _sportarr_recordings(settings)  # before taking the lock: it's a network call
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK_KEY])
        now = timezone.now()
        recordings = _active_recordings(now, sportarr)
        provider = _provider_streams(_skip_groups(settings), keep={r.channel_id for r in recordings})
        recorded = [
            Recorded(r.channel_id, str(r.channel.uuid), r.channel.name, frozenset(provider.get(r.channel_id, ())))
            for r in recordings
        ]
        existing, strays = _piggybacks()
        p = plan(recorded, provider, existing)
        if strays:
            from apps.channels.models import Stream
            Stream.objects.filter(id__in=strays).delete()
        _apply(p, get_dvr_stream_base_url())
        guide = _mark_guide(_scheduled_recordings(now, sportarr), settings.get("mark_guide", True),
                            settings.get("mark"))
    if guide:
        try:
            from apps.output.streaming_chunk_cache import invalidate_epg_chunk_cache
            invalidate_epg_chunk_cache()
        except Exception:
            log.exception("could not clear the EPG output cache")
    if p.add or p.remove or guide or strays:
        log.info("Recording Piggyback: %s recording, +%s piggybacks, -%s, %s guide changes",
                 len(recordings), len(p.add), len(p.remove) + len(strays), guide)
    return (f"{len(recordings)} recording now; added {len(p.add)} piggyback(s), removed "
            f"{len(p.remove) + len(strays)}; {guide} guide program(s) updated.{note}")


def status(settings):
    from django.utils import timezone
    from apps.channels.models import Channel
    recordings = _active_recordings(timezone.now(), _sportarr_recordings(settings)[0])
    existing, _ = _piggybacks()
    lines = []
    for r in recordings:
        riders = sorted(
            f"{int(c.channel_number or 0)} {c.name}"
            for c in Channel.objects.filter(id__in=[cid for cid, links in existing.items()
                                                     if any(u == str(r.channel.uuid).lower() for _, u in links)])
        )
        source = f" ({r.source})" if getattr(r, "source", None) else ""
        lines.append(f"🔴 {int(r.channel.channel_number or 0)} {r.channel.name}{source} "
                     f"(until {timezone.localtime(r.end_time):%H:%M})"
                     + (f"\n   riding it: {', '.join(riders)}" if riders else "\n   no other channel carries its stream"))
    return "\n".join(lines) or "Nothing is recording right now."


class Plugin:
    name = _MANIFEST["name"]
    version = _MANIFEST["version"]
    description = _MANIFEST["description"]
    fields = _MANIFEST["fields"]
    actions = _MANIFEST["actions"]

    def run(self, action, params, context):
        settings = {**_DEFAULTS, **((context or {}).get("settings") or {})}
        try:
            if action == "status":
                return {"status": "ok", "message": status(settings)}
            if action == "test_sportarr":
                return {"status": "ok", "message": test_sportarr(settings)}
            if action != "sync":
                return {"status": "error", "message": f"Unknown action {action!r}"}
            event = (params or {}).get("event")
            if event in ("channel_start", "channel_stop") and time.monotonic() - _last_run["at"] < CHANNEL_EVENT_MIN_GAP:
                return {"status": "ok", "message": "Synced recently."}
            _last_run["at"] = time.monotonic()
            return {"status": "ok", "message": sync(settings)}
        except Exception as e:
            log.exception("Recording Piggyback %s failed", action)
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

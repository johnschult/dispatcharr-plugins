"""Which channels should ride a recording's connection. No Dispatcharr dependency, so it tests alone.

A recorded channel R holds one provider connection. Any other channel C that carries one of R's
provider streams gets a "piggyback" stream in front of its own: a loopback to Dispatcharr's proxy
of R, so tuning C becomes one more client of R instead of a second provider connection.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

PREFIX = "Piggyback → "
TERMINAL = {"completed", "stopped", "interrupted", "failed", "cancelled", "canceled", "error"}
LOOPBACK_UUID = re.compile(r"/proxy/ts/stream/([0-9a-fA-F-]{36})")


def is_active(status, start, end, now):
    """A recording counts while it says it's recording, or while it's due and hasn't finished."""
    if status == "recording":
        return True
    if status in TERMINAL:
        return False
    return start <= now <= end


REC = "\u0280\u1d07\u1d04"  # "REC" in small capitals: reads as a label, not as part of the title
TITLE_PREFIX = REC + "\u00a0"  # a no-break space, so the mark never wraps away from the title
DESCRIPTION_LINE = f"{REC} · Recording"


def marked(title, description, mark=REC):
    """Guide text for a program being recorded. The mark goes in front, where an app that cuts long
    titles short still shows it, and the description says it plainly. An empty mark means REC."""
    mark = (mark or "").strip() or REC
    line = f"{mark} · Recording"
    title = (title or "").strip()
    if not title.startswith(mark):
        title = f"{mark}\u00a0{title}" if title else mark
    description = description or ""
    if not description.startswith(line):
        description = f"{line}\n{description}" if description else line
    return title, description


def overlaps(program_start, program_end, rec_start, rec_end):
    """Whether a program is what's being recorded: at least half of it falls inside the recording.
    Recordings start early and run late, so the pregame and postgame blocks around a game overlap
    a little; they shouldn't get the red dot."""
    length = (program_end - program_start).total_seconds()
    inside = (min(program_end, rec_end) - max(program_start, rec_start)).total_seconds()
    return length > 0 and inside >= length / 2


def loopback_name(recorded_name):
    return f"{PREFIX}{recorded_name}"[:255]


def loopback_target(url):
    """The recorded channel's uuid a piggyback stream points at, or None."""
    m = LOOPBACK_UUID.search(url or "")
    return m.group(1).lower() if m else None


@dataclass(frozen=True)
class Recorded:
    channel_id: int
    uuid: str
    name: str
    provider_streams: frozenset


@dataclass(frozen=True)
class Plan:
    add: tuple  # (channel id, Recorded) pairs that need a piggyback stream
    remove: tuple  # piggyback stream ids to delete


def plan(recorded, provider_streams, piggybacks):
    """recorded: [Recorded] for active recordings.
    provider_streams: {channel id: set of provider (non-custom) stream ids} for every channel.
    piggybacks: {channel id: [(stream id, target uuid)]} piggyback streams that exist now.
    """
    recorded_ids = {r.channel_id for r in recorded}
    wanted = {}  # channel id -> Recorded it should ride
    for r in sorted(recorded, key=lambda r: r.channel_id):  # lowest id wins if two record the same feed
        if not r.provider_streams:
            continue
        for channel_id, streams in provider_streams.items():
            if channel_id in recorded_ids or channel_id in wanted:
                continue
            if streams & r.provider_streams:
                wanted[channel_id] = r

    add, remove = [], []
    for channel_id, existing in piggybacks.items():
        target = wanted.get(channel_id)
        keep = None
        for stream_id, uuid in existing:
            if target and keep is None and uuid == target.uuid.lower():
                keep = stream_id
            else:
                remove.append(stream_id)
    for channel_id, r in wanted.items():
        has = any(uuid == r.uuid.lower() for _, uuid in piggybacks.get(channel_id, []))
        if not has:
            add.append((channel_id, r))
    return Plan(add=tuple(sorted(add, key=lambda a: a[0])), remove=tuple(sorted(remove)))


# Sportarr records through Dispatcharr's proxy as an ordinary client, so its recordings never show up
# in Dispatcharr's own table; they're read from its API instead.
SPORTARR_DONE = TERMINAL | {"imported", "importing", "ignored"}
# Sportarr sometimes stores a game's end a day late; no game runs this long, and uncapped the red dot
# would cover the channel's next day of programmes
SPORTARR_MAX_SPAN = timedelta(hours=8)


@dataclass(frozen=True)
class Window:
    uuid: str  # the Dispatcharr channel being recorded
    start: datetime
    end: datetime
    status: str


def _when(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def from_sportarr(recordings, channels):
    """Sportarr's unfinished recordings as Dispatcharr channel windows, padding included.

    recordings: Sportarr's /api/dvr/recordings. channels: its /api/iptv/channels, whose stream URLs
    are Dispatcharr's proxy URLs and so name the channel's uuid; a channel from anywhere else is skipped.
    """
    uuids = {}
    for ch in channels:
        uuid = loopback_target(ch.get("streamUrl"))
        if uuid:
            uuids[ch.get("id")] = uuid
    out = []
    for r in recordings:
        status = (r.get("status") or "").lower()
        uuid = uuids.get(r.get("channelId"))
        start, end = _when(r.get("scheduledStart")), _when(r.get("scheduledEnd"))
        if status in SPORTARR_DONE or not uuid or not start or not end:
            continue
        start -= timedelta(minutes=r.get("prePadding") or 0)
        end = min(end + timedelta(minutes=r.get("postPadding") or 0), start + SPORTARR_MAX_SPAN)
        out.append(Window(uuid=uuid, start=start, end=end, status=status))
    return out


NOT_DISPATCHARR = "its stream isn't from Dispatcharr"
NO_SUCH_SPORTARR_CHANNEL = "Sportarr no longer lists the channel"
NO_SUCH_DISPATCHARR_CHANNEL = "Dispatcharr has no such channel"


def unmatched(recordings, channels):
    """[(channel name, reason)] for the channels of unfinished Sportarr recordings that from_sportarr
    has to skip, each named once, in the order first seen."""
    by_id = {ch.get("id"): ch for ch in channels}
    out = []
    for r in recordings:
        if (r.get("status") or "").lower() in SPORTARR_DONE:
            continue
        ch = by_id.get(r.get("channelId"))
        if ch is None:
            problem = (f"Sportarr channel {r.get('channelId')}", NO_SUCH_SPORTARR_CHANNEL)
        elif not loopback_target(ch.get("streamUrl")):
            problem = (ch.get("name") or f"Sportarr channel {ch.get('id')}", NOT_DISPATCHARR)
        else:
            continue
        if problem not in out:
            out.append(problem)
    return out

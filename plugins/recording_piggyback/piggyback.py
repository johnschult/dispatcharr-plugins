"""Which channels should ride a recording's connection. No Dispatcharr dependency, so it tests alone.

A recorded channel R holds one provider connection. Any other channel C that carries one of R's
provider streams gets a "piggyback" stream in front of its own: a loopback to Dispatcharr's proxy
of R, so tuning C becomes one more client of R instead of a second provider connection.
"""
import re
from dataclasses import dataclass

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


TITLE_MARK = "  🔴"  # same spacing Teamarr puts after its sport emoji
DESCRIPTION_LINE = "🔴 Recording"


def marked(title, description):
    """Guide text for a program being recorded: the sport emoji stays first, the red dot goes at
    the end so the two never sit together, and the description says it plainly."""
    title = title or ""
    if not title.endswith(TITLE_MARK):
        title = title.rstrip() + TITLE_MARK
    description = description or ""
    if not description.startswith(DESCRIPTION_LINE):
        description = f"{DESCRIPTION_LINE}\n{description}" if description else DESCRIPTION_LINE
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

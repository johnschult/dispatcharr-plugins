"""Guide title rules. No Dispatcharr dependency, so it tests alone.

The rule language is the one EPG & Sports Editor used (plus our prefix_sport), so the rules
moved over unchanged. The difference is where they apply: that plugin wrote a transformed copy
of each source, which meant moving channels onto the copy, and Schedules Direct stops
downloading stations no channel maps to. We rewrite the source's own programmes instead and keep
their original fields in custom_properties, so every run starts from the originals.
"""
import re
from typing import NamedTuple, Optional

ORIGINAL = "guide_titles_original"
PREFIX_GAP = "  "  # two no-break spaces, so apps don't collapse the gap after the emoji
# Matchups in these sports read "Away vs Home", not "Away at Home"
TEAM_SPORTS = {"🏈", "🏀", "⚾", "🏒", "🏑", "🏐", "🥎", "🥍", "⚽", "🏉"}
MATCHUP_AT = re.compile(r"^(.+?) at (.+)$")
SECTION = re.compile(r"^\[(.+)\]$")
KINDS = {"swap_subtitle", "swap_description", "regex", "replace", "prefix_sport"}

# Recording Piggyback's guide mark (see its piggyback.py)
PIGGYBACK = "piggyback_original"
MARK_TITLE = "\u00a0\u00a0🔴"  # no-break spaces, exactly as Piggyback writes it
MARK_DESCRIPTION = "🔴 Recording"


class Fields(NamedTuple):
    title: Optional[str]
    sub_title: Optional[str]
    description: Optional[str]


class Rule(NamedTuple):
    kind: str
    pattern: Optional[re.Pattern]
    arg: str  # replacement or emoji
    extract: Optional[re.Pattern] = None
    find: str = ""


def parse(text):
    """({source name: [Rule]}, [error lines]) from the rules field."""
    sections, errors, current = {}, [], None
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = SECTION.match(line)
        if m:
            current = sections.setdefault(m.group(1).strip(), [])
            continue
        parts = line.split("::")
        kind = parts[0].strip().lower()
        if current is None:
            errors.append(f"line {n}: rule before any [source] section")
        elif len(parts) < 3 or kind not in KINDS:
            errors.append(f"line {n}: not a rule: {line[:80]}")
        else:
            try:
                current.append(_rule(kind, parts[1], "::".join(parts[2:])))
            except re.error as e:
                errors.append(f"line {n}: bad pattern ({e}): {line[:80]}")
    return sections, errors


def _rule(kind, pattern, arg):
    if kind == "replace":
        return Rule(kind, None, arg, find=pattern)
    if kind == "regex":
        return Rule(kind, re.compile(pattern), re.sub(r"\$(\d+)", r"\\\1", arg))
    if kind == "swap_description":
        return Rule(kind, re.compile(pattern), arg, re.compile(arg, re.DOTALL) if arg.strip() else None)
    return Rule(kind, re.compile(pattern), arg.strip())


def _from_description(description, extract):
    """(title, rest of description) for swap_description, or (None, description). Without an
    extract pattern the title is the first line. Over 150 characters is a paragraph, not a title."""
    description = description or ""
    if extract is not None:
        m = extract.search(description)
        if not m:
            return None, description
        text = (m.group(1) if m.groups() else m.group(0)) or ""
        rest = (description[:m.start()] + description[m.end():]).strip()
    else:
        if "\n" not in description.strip():
            return None, description
        text, rest = description.strip().split("\n", 1)
        rest = rest.strip()
    text = " ".join(text.split())
    if not text or len(text) > 150:
        return None, description
    return text, rest


def _move(f, rules):
    """The one cross-field rule that fires, if any: swap_subtitle first, then swap_description."""
    for r in rules:
        if r.kind == "swap_subtitle" and f.sub_title and r.pattern.search(f.title):
            return Fields(f.sub_title, f.title, f.description)
    for r in rules:
        if r.kind == "swap_description" and f.description and r.pattern.search(f.title):
            title, rest = _from_description(f.description, r.extract)
            if title:
                return Fields(title, f.sub_title or f.title, rest or f.description)
    return f


def transform(f, rules):
    """The fields a programme should show, from its original fields."""
    if not f.title or not rules:
        return f
    f = _move(f, rules)
    title = f.title
    for r in rules:
        if r.kind == "regex":
            title = r.pattern.sub(r.arg, title)
        elif r.kind == "replace":
            title = title.replace(r.find, r.arg)
    title = title.strip() or f.title
    for r in rules:
        if r.kind == "prefix_sport" and any(v and r.pattern.search(v) for v in (f.sub_title, title)):
            if r.arg in TEAM_SPORTS:
                title = MATCHUP_AT.sub(r"\1 vs \2", title, count=1)
            title = f"{r.arg}{PREFIX_GAP}{title}"
            break
    return Fields(title, f.sub_title, f.description)


def _marked(props):
    """Whether Recording Piggyback has marked this programme for a recording."""
    return isinstance(props.get(PIGGYBACK), dict)


def rewrite(current, props, rules):
    """(fields, custom_properties) a stored programme should have, or None if it already does.

    The originals are the source's own text: what we kept the first time we changed it, or,
    if Recording Piggyback marked it before we got to it, the unmarked text Piggyback kept.
    A programme the rules leave alone keeps no copy, and one we changed gets its originals back
    when the rules no longer touch it."""
    before = dict(props or {})
    props = dict(before)
    if ORIGINAL in props:
        original = Fields(**props[ORIGINAL])
    elif _marked(props):
        original = Fields(props[PIGGYBACK].get("title"), current.sub_title, props[PIGGYBACK].get("description"))
    else:
        original = current
    shown = transform(original, rules)
    if shown == original:
        props.pop(ORIGINAL, None)
    else:
        props[ORIGINAL] = original._asdict()
    if _marked(props):
        # Piggyback restores from its copy when the recording ends, so that copy gets our text,
        # and the shown text keeps its mark on top of ours.
        props[PIGGYBACK] = {**props[PIGGYBACK], "title": shown.title, "description": shown.description}
        shown = Fields(
            (shown.title or "").rstrip() + MARK_TITLE,
            shown.sub_title,
            f"{MARK_DESCRIPTION}\n{shown.description}" if shown.description else MARK_DESCRIPTION,
        )
    return None if shown == current and props == before else (shown, props)

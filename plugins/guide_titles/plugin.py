"""Guide Titles: rewrite programme titles by rule after each EPG refresh, in the source itself.

Everything it needs lives in Dispatcharr's own tables: a programme we changed keeps its original
fields in custom_properties["guide_titles_original"] (see titles.py), so every run works from the
originals and running twice changes nothing.
"""
import json
import logging
from pathlib import Path

try:
    from .titles import Fields, parse, rewrite
except ImportError:  # loaded as a top-level module
    from titles import Fields, parse, rewrite

_MANIFEST = json.loads((Path(__file__).parent / "plugin.json").read_text(encoding="utf-8"))
_DEFAULTS = {f["id"]: f.get("default") for f in _MANIFEST["fields"]}
# pg advisory lock shared with Recording Piggyback: both rewrite programme titles and both run on
# every EPG refresh, so they take turns rather than overwrite each other's edits
LOCK_KEY = 7431002
BATCH = 1000
SAMPLES = 8
log = logging.getLogger("plugins.guide_titles")
# Celery workers start with the root logger at WARNING, and the refresh event runs us in one,
# so without this a run after each refresh would leave no line in the log
log.setLevel(logging.INFO)


def run_rules(settings, only_source=None, dry_run=False):
    """Apply the rules to each source named in them (or just `only_source`). Returns a summary."""
    from django.db import connection, transaction
    from apps.epg.models import EPGSource, ProgramData

    sections, errors = parse(settings.get("rules"))
    lines = [f"⚠️ {e}" for e in errors]
    changed_total = 0
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK_KEY])
        for name, rules in sections.items():
            if only_source is not None and name != only_source:
                continue
            source = EPGSource.objects.filter(name=name).first()
            if source is None:
                lines.append(f"{name}: no EPG source by that name")
                continue
            programs = (
                ProgramData.objects.filter(epg__epg_source=source)
                .only("id", "title", "sub_title", "description", "custom_properties")
                .order_by("id")
            )
            seen, changed, samples, batch = 0, 0, [], []
            for p in programs.iterator(chunk_size=2000):
                seen += 1
                result = rewrite(Fields(p.title, p.sub_title, p.description), p.custom_properties, rules)
                if result is None:
                    continue
                changed += 1
                fields, props = result
                if len(samples) < SAMPLES and fields.title != p.title:
                    samples.append(f"   {p.title!r} → {fields.title!r}")
                p.title, p.sub_title, p.description = fields
                p.custom_properties = props
                batch.append(p)
                if len(batch) >= BATCH:
                    if not dry_run:
                        ProgramData.objects.bulk_update(batch, ["title", "sub_title", "description", "custom_properties"])
                    batch = []
            if batch and not dry_run:
                ProgramData.objects.bulk_update(batch, ["title", "sub_title", "description", "custom_properties"])
            changed_total += changed
            verb = "would change" if dry_run else "changed"
            lines.append(f"{name}: {verb} {changed} of {seen} programmes")
            lines.extend(samples)
    if changed_total and not dry_run:
        try:
            from apps.output.streaming_chunk_cache import invalidate_epg_chunk_cache
            invalidate_epg_chunk_cache()
        except Exception:
            log.exception("could not clear the EPG output cache")
    if errors:
        log.warning("Guide Titles: %s rule line(s) skipped: %s", len(errors), "; ".join(errors))
    if not dry_run:
        log.info("Guide Titles: %s", "; ".join(l for l in lines if not l.startswith("   ")))
    return "\n".join(lines) or "No [source] sections in the rules."


class Plugin:
    name = _MANIFEST["name"]
    version = _MANIFEST["version"]
    description = _MANIFEST["description"]
    fields = _MANIFEST["fields"]
    actions = _MANIFEST["actions"]

    def run(self, action, params, context):
        settings = {**_DEFAULTS, **((context or {}).get("settings") or {})}
        try:
            if action == "preview":
                return {"status": "ok", "message": run_rules(settings, dry_run=True)}
            if action != "apply":
                return {"status": "error", "message": f"Unknown action {action!r}"}
            # A refresh event names its source; only that one needs redoing
            payload = (params or {}).get("payload") or {}
            return {"status": "ok", "message": run_rules(settings, only_source=payload.get("source_name"))}
        except Exception as e:
            log.exception("Guide Titles %s failed", action)
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

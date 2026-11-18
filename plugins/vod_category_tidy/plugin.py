"""VOD Category Tidy: renames and merges movie/series categories in the Xtream Codes API.

On import it wraps four functions in apps.output.views that the XC API looks up by name on
every request. The wrappers read this plugin's settings each time (cached briefly), pass
everything through untouched when the plugin is disabled, and fall back to the original
output on any error, so a Dispatcharr change can't break VOD browsing.
"""
import json
import logging
import time
from pathlib import Path

try:
    from .tidy import Tidy, parse_rules
except ImportError:  # loaded as a top-level module
    from tidy import Tidy, parse_rules

_MANIFEST = json.loads((Path(__file__).parent / "plugin.json").read_text(encoding="utf-8"))
_DEFAULTS = {f["id"]: f.get("default") for f in _MANIFEST["fields"]}
KEY = "vod_category_tidy"
CACHE_SECONDS = 30
log = logging.getLogger("plugins.vod_category_tidy")

_cache = {"at": 0.0, "value": None}


def _config():
    """(settings, enabled) from the database, cached for CACHE_SECONDS."""
    now = time.monotonic()
    if now - _cache["at"] > CACHE_SECONDS:
        from apps.plugins.models import PluginConfig
        cfg = PluginConfig.objects.filter(key=KEY).first()
        settings = {**_DEFAULTS, **((cfg.settings or {}) if cfg else {})}
        _cache.update(at=now, value=(settings, bool(cfg and cfg.enabled)))
    return _cache["value"]


def _tidy(category_type):
    """A Tidy for one category type, or None when the plugin is off."""
    settings, enabled = _config()
    if not enabled:
        return None
    from apps.vod.models import VODCategory
    rows = VODCategory.objects.filter(category_type=category_type).values_list("id", "name")
    return Tidy(list(rows), parse_rules(settings.get("rules")), settings.get("strip_pattern"), settings.get("hide_words"))


_warned = set()


def _safe(name, fallback, work):
    try:
        return work()
    except Exception:
        if name not in _warned:  # once per function, so a broken hook doesn't flood the log
            _warned.add(name)
            log.exception("VOD Category Tidy: %s failed; serving the original output", name)
        return fallback()


def _wrap_categories(original, category_type):
    def categories(user):
        result = original(user)

        def work():
            tidy = _tidy(category_type)
            return result if tidy is None else tidy.categories(result)

        return _safe(original.__name__, lambda: result, work)

    return categories


def _wrap_items(original, category_type):
    def items(request, user, category_id=None):
        def work():
            tidy = _tidy(category_type)
            if tidy is None:
                return original(request, user, category_id)
            if not category_id:
                return tidy.by_category(tidy.relabel(original(request, user, None)))
            members = tidy.expand(int(category_id))
            out = []
            for member in members:
                out.extend(original(request, user, str(member)))
            if len(members) > 1:
                out.sort(key=lambda i: (i.get("name") or "").lower())
                for num, item in enumerate(out, 1):
                    item["num"] = num
            return tidy.relabel(out)

        return _safe(original.__name__, lambda: original(request, user, category_id), work)

    return items


HOOKS = {
    "xc_get_vod_categories": (_wrap_categories, "movie"),
    "xc_get_series_categories": (_wrap_categories, "series"),
    "xc_get_vod_streams": (_wrap_items, "movie"),
    "xc_get_series": (_wrap_items, "series"),
}


def install():
    """Wrap the XC functions. Safe to call again on plugin reload: it always wraps the originals."""
    from apps.output import views
    installed = []
    for name, (make, category_type) in HOOKS.items():
        current = getattr(views, name, None)
        if current is None:
            log.warning("VOD Category Tidy: Dispatcharr has no %s; that part stays untidied", name)
            continue
        original = getattr(current, "_vct_original", current)
        wrapper = make(original, category_type)
        wrapper._vct_original = original
        wrapper.__name__ = name
        setattr(views, name, wrapper)
        installed.append(name)
    return installed


try:
    _INSTALLED = install()
    log.info("VOD Category Tidy: wrapped %s", ", ".join(_INSTALLED) or "nothing")
except Exception:
    _INSTALLED = []
    log.exception("VOD Category Tidy: could not install; apps see the provider's categories")


class Plugin:
    name = _MANIFEST["name"]
    version = _MANIFEST["version"]
    description = _MANIFEST["description"]
    fields = _MANIFEST["fields"]
    actions = _MANIFEST["actions"]

    def run(self, action, params, context):
        if action != "preview":
            return {"status": "error", "message": f"Unknown action {action!r}"}
        _cache["at"] = 0.0  # use the settings just saved
        settings = {**_DEFAULTS, **(context.get("settings") or {})}
        from apps.vod.models import VODCategory
        lines = []
        for category_type in ("movie", "series"):
            rows = list(VODCategory.objects.filter(category_type=category_type).values_list("id", "name"))
            tidy = Tidy(rows, parse_rules(settings.get("rules")), settings.get("strip_pattern"), settings.get("hide_words"))
            lines.append(f"{category_type.title()} ({len(tidy.members)} categories, {len(tidy.hidden)} hidden):")
            lines.extend(tidy.describe(rows))
        hooked = ", ".join(_INSTALLED) or "none (see the Dispatcharr log)"
        return {"status": "ok", "message": f"Hooks: {hooked}\n\n" + "\n".join(lines)}

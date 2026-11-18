"""Helpers for the integration tests (imported by the tests and conftest.py)."""
import sys
import threading
from datetime import timedelta

OURS = ("guide_titles", "recording_piggyback", "vod_category_tidy")
LOCK_KEY = 7431002  # the advisory lock Guide Titles and Recording Piggyback share


def settings_for(key, **values):
    from apps.plugins.models import PluginConfig

    cfg = PluginConfig.objects.get(key=key)
    cfg.settings = {**(cfg.settings or {}), **values}
    cfg.save(update_fields=["settings"])


def module_of(pm, key):
    """The plugin module Dispatcharr loaded, for reaching its constants and caches."""
    loaded = pm.get_plugin(key).instance  # the Plugin class or an instance of it
    return sys.modules[(loaded if isinstance(loaded, type) else type(loaded)).__module__]


class HeldLock:
    """Holds the shared advisory lock from another thread (another DB connection) until released."""

    def __init__(self):
        self.acquired, self.release = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self):
        from django.db import connection, transaction

        try:
            with transaction.atomic(), connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK_KEY])
                self.acquired.set()
                self.release.wait(30)
        finally:
            connection.close()

    def __enter__(self):
        self.thread.start()
        assert self.acquired.wait(10)
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(10)


def in_thread(fn):
    """Run fn on its own thread (and DB connection); returns (thread, result holder)."""
    out = {}

    def run():
        from django.db import connection

        try:
            out["value"] = fn()
        finally:
            connection.close()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def hours(n):
    return timedelta(hours=n)

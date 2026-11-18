"""Shared fixtures for the integration tests, which run inside the Dispatcharr image (see ../test.sh).

Our plugins are copied into /data/plugins and loaded through Dispatcharr's own PluginManager, so
discovery, manifests, enabling and event wiring are exercised the way a real install runs them. pytest-django
gives each test a fresh test database.
"""
import pytest

from helpers import OURS


@pytest.fixture
def manager(transactional_db):
    """Dispatcharr's PluginManager with our plugins discovered and enabled."""
    from apps.plugins.loader import PluginManager
    from apps.plugins.models import PluginConfig

    pm = PluginManager.get()
    pm.discover_plugins(sync_db=True, force_reload=True, use_cache=False)
    for key in OURS:
        assert pm.get_plugin(key) is not None, f"Dispatcharr didn't discover {key}"
    PluginConfig.objects.filter(key__in=OURS).update(enabled=True)
    # Dispatcharr only imports a plugin's code once it's enabled, so load them now
    pm.discover_plugins(sync_db=True, force_reload=True, use_cache=False)
    for key in OURS:
        assert pm.get_plugin(key).instance is not None, f"Dispatcharr didn't load {key}"
    return pm


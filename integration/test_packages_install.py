"""The ZIPs publish.py builds install through Dispatcharr's own ZIP installer."""
import io
from pathlib import Path

import pytest

from helpers import OURS
from publish import package


@pytest.mark.parametrize("slug", OURS)
def test_package_installs_as_the_same_plugin(slug, tmp_path, db):
    from apps.plugins.api_views import _install_plugin_from_zip

    source = Path("/data/plugins") / slug
    result = _install_plugin_from_zip(io.BytesIO(package(source)), str(tmp_path), file_name=f"{slug}.zip")

    assert result.get("success"), result
    assert result["plugin_key"] == slug
    installed = tmp_path / slug
    expected = sorted(p.relative_to(source) for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    assert sorted(p.relative_to(installed) for p in installed.rglob("*") if p.is_file()) == expected
    for rel in expected:
        assert (installed / rel).read_bytes() == (source / rel).read_bytes()

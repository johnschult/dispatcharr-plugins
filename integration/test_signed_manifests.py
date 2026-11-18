"""Manifests publish.py signs pass Dispatcharr's own signature check, and tampered ones don't."""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from publish import build, gpg_signer, write_manifests


@pytest.fixture
def key(tmp_path, monkeypatch):
    """A throwaway signing key in its own keyring; returns (key id, armored public key)."""
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    subprocess.run(
        ["gpg", "--batch", "--passphrase", "", "--quick-gen-key", "Test Signer <test@example.invalid>",
         "ed25519", "sign", "never"],
        check=True, capture_output=True,
    )
    public = subprocess.run(["gpg", "--armor", "--export", "test@example.invalid"],
                            check=True, capture_output=True).stdout.decode()
    return "test@example.invalid", public


@pytest.fixture
def published(tmp_path, key):
    result = build(Path("/data/plugins"), {}, "0" * 40, datetime.now(timezone.utc))
    write_manifests(tmp_path / "releases", result.metadata, gpg_signer(key[0]))
    return tmp_path / "releases"


def fetched(path):
    """A manifest as Dispatcharr sees it after fetching: parsed JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def files(releases):
    return [releases / "manifest.json", *sorted((releases / "metadata").glob("*/manifest.json"))]


def test_every_manifest_verifies(published, key):
    from apps.plugins.api_views import _verify_manifest_signature

    for path in files(published):
        data = fetched(path)
        assert _verify_manifest_signature(data["manifest"], data["signature"], key[1]) is True, path


def test_a_changed_manifest_fails(published, key):
    from apps.plugins.api_views import _verify_manifest_signature

    data = fetched(published / "manifest.json")
    data["manifest"]["plugins"][0]["latest_sha256"] = "0" * 64

    assert _verify_manifest_signature(data["manifest"], data["signature"], key[1]) is False


def test_another_key_fails(published, key, tmp_path, monkeypatch):
    from apps.plugins.api_views import _verify_manifest_signature

    other_home = tmp_path / "other"
    other_home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(other_home)}
    subprocess.run(["gpg", "--batch", "--passphrase", "", "--quick-gen-key", "Other <other@example.invalid>",
                    "ed25519", "sign", "never"], check=True, capture_output=True, env=env)
    other = subprocess.run(["gpg", "--armor", "--export", "other@example.invalid"],
                           check=True, capture_output=True, env=env).stdout.decode()
    data = fetched(published / "manifest.json")

    assert _verify_manifest_signature(data["manifest"], data["signature"], other) is False

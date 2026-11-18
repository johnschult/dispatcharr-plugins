import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

import pytest

from publish import (
    MIN_DISPATCHARR,
    REGISTRY,
    ReleaseError,
    build,
    package,
    release_entry,
    root_manifest,
)

WHEN = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def make_plugin(root, slug, version="0.1.0", extra=None):
    d = root / slug
    (d / "tests").mkdir(parents=True)
    (d / "integration").mkdir()
    (d / "__pycache__").mkdir()
    (d / "plugin.json").write_text(json.dumps({
        "name": slug.title(), "version": version, "description": f"{slug} does things", "author": "johnschult",
    }))
    (d / "plugin.py").write_text("class Plugin: pass\n")
    (d / "logo.png").write_bytes(b"\x89PNG")
    (d / "tests" / "test_x.py").write_text("")
    (d / "integration" / "test_y.py").write_text("")
    (d / "__pycache__" / "plugin.cpython-313.pyc").write_bytes(b"x")
    (d / ".DS_Store").write_bytes(b"x")
    for name, text in (extra or {}).items():
        (d / name).write_text(text)
    return d


def names(data):
    return sorted(zipfile.ZipFile(io.BytesIO(data)).namelist())


# package

def test_package_holds_only_the_plugin_files_under_its_folder(tmp_path):
    data = package(make_plugin(tmp_path, "guide_titles"))
    assert names(data) == ["guide_titles/logo.png", "guide_titles/plugin.json", "guide_titles/plugin.py"]


def test_package_is_byte_for_byte_repeatable(tmp_path):
    d = make_plugin(tmp_path, "guide_titles")
    first = package(d)
    (d / "plugin.py").touch()  # a newer mtime must not change the bytes
    assert package(d) == first


def test_package_changes_when_a_file_does(tmp_path):
    d = make_plugin(tmp_path, "guide_titles")
    first = package(d)
    (d / "plugin.py").write_text("class Plugin: x = 1\n")
    assert package(d) != first


# release_entry

def test_release_entry_describes_the_zip(tmp_path):
    data = package(make_plugin(tmp_path, "guide_titles"))
    e = release_entry("guide_titles", "0.1.0", data, COMMIT, WHEN)
    assert e["version"] == "0.1.0"
    assert e["url"] == e["latest_url"] == "guide_titles-0.1.0/guide_titles-0.1.0.zip"
    assert e["checksum_sha256"] == hashlib.sha256(data).hexdigest()
    assert e["checksum_md5"] == hashlib.md5(data).hexdigest()
    assert e["size"] == len(data) // 1024  # Dispatcharr's sizes are whole KiB
    assert e["commit_sha"] == COMMIT and e["commit_sha_short"] == COMMIT[:7]
    assert e["build_timestamp"] == e["last_updated"] == "2026-11-18T12:00:00Z"
    assert e["min_dispatcharr_version"] == MIN_DISPATCHARR


# build: what to release, and the manifests Dispatcharr reads

def test_first_build_releases_every_plugin(tmp_path):
    make_plugin(tmp_path / "plugins", "guide_titles")
    make_plugin(tmp_path / "plugins", "vod_category_tidy", "0.2.2")

    result = build(tmp_path / "plugins", {}, COMMIT, WHEN)

    assert [(r.slug, r.version) for r in result.new] == [("guide_titles", "0.1.0"), ("vod_category_tidy", "0.2.2")]
    meta = result.metadata["vod_category_tidy"]["manifest"]
    assert meta["slug"] == "vod_category_tidy"
    assert meta["name"] == "Vod_Category_Tidy"
    assert meta["license"] == "MIT"
    assert meta["registry_url"] == REGISTRY["registry_url"]
    assert meta["repo_url"] == REGISTRY["registry_url"]
    assert meta["latest"]["version"] == "0.2.2"
    assert [v["version"] for v in meta["versions"]] == ["0.2.2"]


def test_unchanged_plugin_at_the_same_version_is_left_alone(tmp_path):
    make_plugin(tmp_path / "plugins", "guide_titles")
    first = build(tmp_path / "plugins", {}, COMMIT, WHEN)

    again = build(tmp_path / "plugins", first.metadata, "f" * 40, WHEN)

    assert again.new == []
    assert again.metadata == first.metadata


def test_new_version_goes_on_top_of_the_history(tmp_path):
    d = make_plugin(tmp_path / "plugins", "guide_titles")
    first = build(tmp_path / "plugins", {}, COMMIT, WHEN)
    meta = json.loads((d / "plugin.json").read_text())
    (d / "plugin.json").write_text(json.dumps({**meta, "version": "0.2.0"}))

    second = build(tmp_path / "plugins", first.metadata, "f" * 40, WHEN)

    assert [(r.slug, r.version) for r in second.new] == [("guide_titles", "0.2.0")]
    versions = second.metadata["guide_titles"]["manifest"]["versions"]
    assert [v["version"] for v in versions] == ["0.2.0", "0.1.0"]
    assert second.metadata["guide_titles"]["manifest"]["latest"]["version"] == "0.2.0"


def test_changed_files_without_a_version_bump_refuse_to_publish(tmp_path):
    d = make_plugin(tmp_path / "plugins", "guide_titles")
    first = build(tmp_path / "plugins", {}, COMMIT, WHEN)
    (d / "plugin.py").write_text("class Plugin: changed = True\n")

    with pytest.raises(ReleaseError, match="guide_titles.*0.1.0"):
        build(tmp_path / "plugins", first.metadata, "f" * 40, WHEN)


def test_folders_without_a_manifest_are_not_plugins(tmp_path):
    make_plugin(tmp_path / "plugins", "guide_titles")
    (tmp_path / "plugins" / "notes").mkdir()

    assert list(build(tmp_path / "plugins", {}, COMMIT, WHEN).metadata) == ["guide_titles"]


def test_root_manifest_lists_each_plugins_latest(tmp_path):
    make_plugin(tmp_path / "plugins", "guide_titles")
    result = build(tmp_path / "plugins", {}, COMMIT, WHEN)

    root = root_manifest(result.metadata)["manifest"]

    assert root["registry_url"] == REGISTRY["registry_url"]
    assert root["root_url"] == REGISTRY["registry_url"] + "/releases/download"
    (p,) = root["plugins"]
    latest = result.metadata["guide_titles"]["manifest"]["latest"]
    assert p["slug"] == "guide_titles"
    assert p["manifest_url"] == f"{REGISTRY['metadata_base']}/guide_titles/manifest.json"
    assert p["latest_version"] == "0.1.0"
    assert p["latest_url"] == latest["url"]
    assert p["latest_sha256"] == latest["checksum_sha256"]
    assert p["latest_size"] == latest["size"]
    assert p["min_dispatcharr_version"] == MIN_DISPATCHARR


# Signing: Dispatcharr verifies a detached GPG signature over json.dumps(manifest, separators=(",", ":"))
# plus a newline (apps/plugins/api_views.py, _verify_manifest_signature), stored next to "manifest".

def test_canonical_bytes_are_what_dispatcharr_verifies():
    from publish import canonical

    m = {"b": 1, "a": "🏈 vs", "list": [1, 2]}
    assert canonical(m) == (json.dumps(m, separators=(",", ":")) + "\n").encode("utf-8")
    assert canonical(m) == b'{"b":1,"a":"\\ud83c\\udfc8 vs","list":[1,2]}\n'


def test_write_manifests_signs_every_file(tmp_path):
    from publish import canonical, write_manifests

    make_plugin(tmp_path / "plugins", "guide_titles")
    result = build(tmp_path / "plugins", {}, COMMIT, WHEN)
    signed = []

    def signer(data):
        signed.append(data)
        return f"SIG({len(data)})"

    write_manifests(tmp_path / "releases", result.metadata, signer)

    root = json.loads((tmp_path / "releases" / "manifest.json").read_text())
    meta = json.loads((tmp_path / "releases" / "metadata" / "guide_titles" / "manifest.json").read_text())
    assert root["signature"] == f"SIG({len(canonical(root['manifest']))})"
    assert meta["signature"] == f"SIG({len(canonical(meta['manifest']))})"
    assert sorted(signed) == sorted([canonical(root["manifest"]), canonical(meta["manifest"])])


def test_write_manifests_without_a_signer_writes_no_signature(tmp_path):
    from publish import write_manifests

    make_plugin(tmp_path / "plugins", "guide_titles")
    result = build(tmp_path / "plugins", {}, COMMIT, WHEN)

    write_manifests(tmp_path / "releases", result.metadata, None)

    assert "signature" not in json.loads((tmp_path / "releases" / "manifest.json").read_text())


def test_previous_signature_is_dropped_and_redone(tmp_path):
    from publish import write_manifests

    make_plugin(tmp_path / "plugins", "guide_titles")
    first = build(tmp_path / "plugins", {}, COMMIT, WHEN)
    old = {k: {**v, "signature": "OLD"} for k, v in first.metadata.items()}
    again = build(tmp_path / "plugins", old, COMMIT, WHEN)

    write_manifests(tmp_path / "releases", again.metadata, lambda data: "NEW")

    meta = json.loads((tmp_path / "releases" / "metadata" / "guide_titles" / "manifest.json").read_text())
    assert meta["signature"] == "NEW"

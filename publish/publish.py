"""Builds this repo's Dispatcharr plugin repository: a ZIP per plugin release, and the manifests
Dispatcharr reads (manifest.json, and metadata/<slug>/manifest.json with each plugin's history).

A plugin is released when its plugin.json version has none yet. Its ZIP is built byte-for-byte
repeatably, so a plugin whose files changed but whose version didn't is caught and refused rather
than published under a version that already means something else.

    python publish/publish.py --plugins plugins --releases <releases checkout> --dist dist --commit <sha>

reads and rewrites the manifests in --releases, writes new ZIPs to --dist, and prints one
"<tag> <zip path>" line per new release for the workflow to upload.
"""
import argparse
import hashlib
import io
import json
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = "johnschult/dispatcharr-plugins"
REGISTRY = {
    "registry_url": f"https://github.com/{REPO}",
    "registry_name": "Schultiverse",
    "metadata_base": f"https://raw.githubusercontent.com/{REPO}/releases/metadata",
}
LICENSE = "MIT"
# manifest_version 2 plugins don't install on Dispatcharr 0.33.0 or older
MIN_DISPATCHARR = "v0.34.0"
SKIP = {"tests", "integration", "__pycache__"}
ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # fixed, so the same files always make the same ZIP


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    slug: str
    version: str
    data: bytes

    @property
    def tag(self):
        return f"{self.slug}-{self.version}"


@dataclass(frozen=True)
class Result:
    new: list  # [Release]
    metadata: dict  # {slug: per-plugin manifest document}


def package(plugin_dir):
    """The plugin as Dispatcharr installs it: <slug>/ and its files, no tests or caches."""
    plugin_dir = Path(plugin_dir)
    files = sorted(
        p for p in plugin_dir.rglob("*")
        if p.is_file()
        and not p.name.startswith(".")
        and not SKIP & set(p.relative_to(plugin_dir).parts)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            info = zipfile.ZipInfo(f"{plugin_dir.name}/{p.relative_to(plugin_dir).as_posix()}", ZIP_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, p.read_bytes())
    return buf.getvalue()


def _stamp(when):
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def release_entry(slug, version, data, commit, when):
    path = f"{slug}-{version}/{slug}-{version}.zip"
    return {
        "version": version,
        "commit_sha": commit,
        "commit_sha_short": commit[:7],
        "build_timestamp": _stamp(when),
        "last_updated": _stamp(when),
        "checksum_md5": hashlib.md5(data).hexdigest(),
        "checksum_sha256": hashlib.sha256(data).hexdigest(),
        "min_dispatcharr_version": MIN_DISPATCHARR,
        "url": path,
        "latest_url": path,
        "size": len(data) // 1024,
    }


def _plugin_document(slug, plugin_json, versions):
    latest = versions[0]
    return {"manifest": {
        "slug": slug,
        "name": plugin_json.get("name", slug),
        "description": plugin_json.get("description", ""),
        "author": plugin_json.get("author", ""),
        "license": LICENSE,
        "repo_url": REGISTRY["registry_url"],
        "registry_url": REGISTRY["registry_url"],
        "registry_name": REGISTRY["registry_name"],
        "last_updated": latest["last_updated"],
        "latest": latest,
        "versions": versions,
    }}


def build(plugins_dir, previous, commit, when):
    """What to release now, and every plugin's manifest document afterwards.
    previous: {slug: manifest document} as last published."""
    new, metadata = [], {}
    for d in sorted(p for p in Path(plugins_dir).iterdir() if (p / "plugin.json").is_file()):
        slug, plugin_json = d.name, json.loads((d / "plugin.json").read_text(encoding="utf-8"))
        version, data = plugin_json["version"], package(d)
        versions = list((previous.get(slug) or {}).get("manifest", {}).get("versions", []))
        published = next((v for v in versions if v["version"] == version), None)
        if published is None:
            new.append(Release(slug, version, data))
            versions.insert(0, release_entry(slug, version, data, commit, when))
        elif published["checksum_sha256"] != hashlib.sha256(data).hexdigest():
            raise ReleaseError(f"{slug} {version} is already published with different files; bump its version")
        else:
            metadata[slug] = previous[slug]
            continue
        metadata[slug] = _plugin_document(slug, plugin_json, versions)
    return Result(new, metadata)


def root_manifest(metadata):
    plugins = []
    for slug, doc in sorted(metadata.items()):
        m, latest = doc["manifest"], doc["manifest"]["latest"]
        plugins.append({
            "slug": slug,
            "name": m["name"],
            "description": m["description"],
            "author": m["author"],
            "license": m["license"],
            "manifest_url": f"{REGISTRY['metadata_base']}/{slug}/manifest.json",
            "latest_version": latest["version"],
            "latest_url": latest["url"],
            "latest_sha256": latest["checksum_sha256"],
            "latest_md5": latest["checksum_md5"],
            "latest_size": latest["size"],
            "min_dispatcharr_version": latest["min_dispatcharr_version"],
            "last_updated": latest["last_updated"],
        })
    return {"manifest": {
        "registry_url": REGISTRY["registry_url"],
        "registry_name": REGISTRY["registry_name"],
        "root_url": f"{REGISTRY['registry_url']}/releases/download",
        "plugins": plugins,
    }}


def _write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plugins", type=Path, required=True)
    ap.add_argument("--releases", type=Path, required=True, help="checkout of the releases branch")
    ap.add_argument("--dist", type=Path, required=True)
    ap.add_argument("--commit", required=True)
    args = ap.parse_args(argv)

    meta_dir = args.releases / "metadata"
    previous = {
        p.parent.name: json.loads(p.read_text(encoding="utf-8")) for p in meta_dir.glob("*/manifest.json")
    } if meta_dir.is_dir() else {}
    try:
        result = build(args.plugins, previous, args.commit, datetime.now(timezone.utc))
    except ReleaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    args.dist.mkdir(parents=True, exist_ok=True)
    for r in result.new:
        out = args.dist / f"{r.tag}.zip"
        out.write_bytes(r.data)
        print(f"{r.tag} {out}")
    for slug, doc in result.metadata.items():
        _write_json(meta_dir / slug / "manifest.json", doc)
    _write_json(args.releases / "manifest.json", root_manifest(result.metadata))
    return 0


if __name__ == "__main__":
    sys.exit(main())

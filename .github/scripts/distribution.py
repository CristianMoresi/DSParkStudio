#!/usr/bin/env python3
# Copyright (c) 2026 Cristian Moresi -- Proprietary. All rights reserved.
"""Validate and promote distribution-only assets. No compilation or installation.

Dependencies: Python standard library. Serial, bounded GitHub requests.
This file is intentionally mirrored into the distribution repository's CI.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

REPOSITORY = "CristianMoresi/DSParkStudio"
MANIFEST = "distribution-manifest.json"
CHECKSUMS = "SHA256SUMS"
TARGETS = {
    "windows-x64": ("DSParkStudio-windows-x64-setup.exe",),
    "linux-x64": ("DSParkStudio-linux-x64.tar.gz", "DSParkStudio-linux-x64.deb"),
    "macos-arm64": ("DSParkStudio-macos-arm64.pkg",),
}
LICENSES = ("LICENSE", "DSPARK-LICENSE.txt")
MAX_ASSET = 64 * 1024 * 1024
MAX_COHORT = 192 * 1024 * 1024


class Refused(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Refused(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")


def decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate JSON key")
            result[key] = value
        return result
    def constant(_):
        raise Refused("Non-finite JSON value")
    require(len(data) <= 65536, "Manifest exceeds its size bound")
    return json.loads(data, object_pairs_hook=unique, parse_constant=constant)


def fields(value, expected):
    require(type(value) is dict and set(value) == set(expected), "Unexpected or missing manifest fields")


def digest(value, length=64):
    require(type(value) is str and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value), "Invalid digest")


def version(value):
    require(type(value) is str and re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){3}", value),
            "Invalid four-part package version")
    result = tuple(map(int, value.split(".")))
    require(all(part <= 65535 for part in result), "Package version exceeds native bounds")
    return result


def release_tag(value):
    """Use the product tag for its first package; retain later revision tags."""
    parts = version(value)
    return "v" + (value.rsplit(".", 1)[0] if parts[3] == 0 else value)


def tag_version(tag):
    require(type(tag) is str and tag.startswith("v"), "Invalid release tag")
    value = tag[1:]
    if value.count(".") == 2:
        value += ".0"
    parsed = version(value)
    require(release_tag(value) == tag, "Noncanonical release tag")
    return parsed


def record(name, data):
    return {"name": name, "size": len(data), "sha256": sha(data)}


def manifest_tag(manifest):
    """An official product release may include a later native package revision."""
    current = version(manifest["version"])
    if manifest.get("schemaVersion") == 2:
        tag = manifest.get("releaseTag")
        require(tag_version(tag) == (*current[:3], 0), "Official tag must match the product version")
        return tag
    return release_tag(manifest["version"])


def records(manifest):
    require(type(manifest) is dict, "Expected a manifest object")
    schema = manifest.get("schemaVersion")
    require(type(schema) is int and schema in (1, 2), "Unknown schema")
    fields(manifest, ("schemaVersion", "version", "candidate", "framework", "targets", "licenses") +
           (("releaseTag",) if schema == 2 else ()))
    current = version(manifest["version"])
    manifest_tag(manifest)
    digest(manifest["candidate"])
    digest(manifest["framework"], 40)
    fields(manifest["targets"], TARGETS)
    fields(manifest["licenses"], LICENSES)
    result = dict(manifest["licenses"])
    for target, names in TARGETS.items():
        item = manifest["targets"][target]
        fields(item, ("inputDigest", "packageVersion", "carried", "assets"))
        digest(item["inputDigest"])
        require(type(item["carried"]) is bool, "Carry status must be explicit")
        prior = version(item["packageVersion"])
        require(prior <= current and (item["carried"] or prior == current), "Wrong package version")
        fields(item["assets"], names)
        result.update(item["assets"])
    total = 0
    for name, item in result.items():
        fields(item, ("name", "size", "sha256"))
        require(item["name"] == name, "Asset name mismatch")
        limit = 65536 if name in LICENSES else MAX_ASSET
        require(type(item["size"]) is int and 0 < item["size"] <= limit, "Asset size out of bounds")
        digest(item["sha256"])
        total += item["size"]
    require(total <= MAX_COHORT, "Candidate exceeds the artifact storage budget")
    return result


def read(path, limit=MAX_ASSET):
    require(path.is_file() and not path.is_symlink(), "Expected a regular asset file: " + path.name)
    require(0 < path.stat().st_size <= limit, "File size out of bounds: " + path.name)
    data = path.read_bytes()
    require(len(data) <= limit, "File grew while reading")
    return data


def checksums(files):
    return "".join(f"{sha(files[name])}  {name}\n" for name in sorted(files)).encode("ascii")


def verify(directory, expected_digest=None, expected_license=None):
    raw = read(directory / MANIFEST, 65536)
    if expected_digest is not None:
        digest(expected_digest)
        require(sha(raw) == expected_digest, "Manifest differs from the dispatched candidate")
    manifest = decode(raw)
    inventory = records(manifest)
    require({p.name for p in directory.iterdir()} == set(inventory) | {MANIFEST, CHECKSUMS},
            "Distribution asset allowlist differs")
    files = {MANIFEST: raw}
    for name, expected in inventory.items():
        data = read(directory / name, expected["size"])
        require(record(name, data) == expected, "Asset bytes differ: " + name)
        files[name] = data
    require(read(directory / CHECKSUMS, 65536) == checksums(files), "Detached checksum list differs")
    if expected_license is not None:
        require(files["LICENSE"] == expected_license, "Distribution license is stale")
    require(files["LICENSE"].startswith(b"DSPark Studio License Agreement\n"), "Unknown Studio license")
    return manifest, files


def seal(directory, manifest):
    inventory = records(manifest)
    require({p.name for p in directory.iterdir()} == set(inventory), "Unexpected files before sealing")
    (directory / MANIFEST).write_bytes(encoded(manifest))
    files = {name: read(directory / name) for name in (*inventory, MANIFEST)}
    (directory / CHECKSUMS).write_bytes(checksums(files))
    return verify(directory)[0]


class AssetRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urllib.parse.urlsplit(newurl)
        require(url.scheme == "https" and not url.username and not url.password
                and url.port in (None, 443) and url.hostname is not None
                and (url.hostname == "api.github.com" or url.hostname.endswith(".githubusercontent.com")),
                "Unexpected GitHub asset redirect")
        changed = super().redirect_request(req, fp, code, msg, headers, newurl)
        if changed is not None and url.hostname != urllib.parse.urlsplit(req.full_url).hostname:
            changed.remove_header("Authorization")
        return changed


class GitHub:
    def __init__(self, token=None):
        self.token = token
        self.opener = urllib.request.build_opener(AssetRedirect())

    def request(self, method, path, data=None, binary=False, maximum=2 * 1024 * 1024):
        require(path.startswith("repos/"), "Unexpected API path")
        headers = {"Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "DSParkStudio-Distribution"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request("https://api.github.com/" + path, data=encoded(data) if data is not None else None,
                                     headers=headers, method=method)
        # Retry reads only. Mutating operations are reconciled by the caller.
        for attempt in range(3):
            try:
                with self.opener.open(req, timeout=45) as response:
                    raw = response.read(maximum + 1)
                require(len(raw) <= maximum, "GitHub response exceeds its bound")
                return raw if binary else (json.loads(raw) if raw else None)
            except urllib.error.HTTPError as error:
                if method != "GET" or error.code not in (429, 502, 503, 504) or attempt == 2:
                    raise
                time.sleep(2 ** attempt)

    def asset(self, asset, maximum=MAX_ASSET):
        require(type(asset.get("id")) is int and asset["id"] > 0, "Invalid release asset ID")
        require(type(asset.get("size")) is int and 0 < asset["size"] <= maximum, "Remote asset too large")
        require(asset.get("state") == "uploaded", "Release upload is incomplete")
        data = self.request("GET", f"repos/{REPOSITORY}/releases/assets/{asset['id']}", binary=True, maximum=maximum)
        require(len(data) == asset["size"], "Remote asset size differs")
        return data

    def latest(self):
        try:
            return self.request("GET", f"repos/{REPOSITORY}/releases/latest")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                error.close()
                return None
            raise

    def find_release(self, tag):
        # The tag endpoint is insufficient for resuming an unpublished draft.
        # Bound the authenticated inventory; never create a duplicate on overflow.
        for page in range(1, 11):
            releases = self.request("GET", f"repos/{REPOSITORY}/releases?per_page=100&page={page}")
            require(type(releases) is list, "Invalid release listing")
            matches = [item for item in releases if item.get("tag_name") == tag]
            require(len(matches) <= 1, "Duplicate candidate releases")
            if matches:
                return matches[0]
            if len(releases) < 100:
                return None
        raise Refused("Release inventory exceeds the reconciliation bound")

    def upload(self, release_id, name, data):
        # The host and asset name are fixed/validated, never taken from upload_url.
        require(name in {MANIFEST, CHECKSUMS, *LICENSES, *(n for v in TARGETS.values() for n in v)},
                "Refusing an unapproved upload")
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/octet-stream",
                   "User-Agent": "DSParkStudio-Distribution", "Accept": "application/vnd.github+json"}
        url = f"https://uploads.github.com/repos/{REPOSITORY}/releases/{release_id}/assets?name={name}"
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method="POST"),
                                    timeout=60) as response:
            return json.load(response)


def asset_map(release):
    require(type(release.get("assets")) is list and len(release["assets"]) <= 16, "Unexpected release inventory")
    result = {}
    for asset in release["assets"]:
        name = asset.get("name")
        require(type(name) is str and name not in result, "Duplicate or invalid release asset")
        result[name] = asset
    return result


def published_version(api, release):
    inventory = asset_map(release)
    require(MANIFEST in inventory, "Previous release has no manifest")
    manifest = decode(api.asset(inventory[MANIFEST], 65536))
    records(manifest)
    require(release.get("tag_name") == manifest_tag(manifest), "Previous release tag differs")
    return version(manifest["version"])


def download(api, release, destination, expected_digest=None):
    inventory = asset_map(release)
    require(MANIFEST in inventory, "Release has no distribution manifest")
    raw = api.asset(inventory[MANIFEST], 65536)
    if expected_digest is not None:
        require(sha(raw) == expected_digest, "Remote manifest differs")
    manifest = decode(raw)
    expected = records(manifest)
    require(release.get("tag_name") == manifest_tag(manifest), "Release tag differs from its manifest")
    require(set(inventory) == set(expected) | {MANIFEST, CHECKSUMS}, "Remote asset allowlist differs")
    destination.mkdir(parents=True, exist_ok=False)
    (destination / MANIFEST).write_bytes(raw)
    for name in (*expected, CHECKSUMS):
        (destination / name).write_bytes(api.asset(inventory[name], expected[name]["size"] if name in expected else 65536))
    return verify(destination, expected_digest)[0]


def stage(api, directory, expected_license):
    manifest, files = verify(directory, expected_license=expected_license)
    files[CHECKSUMS] = read(directory / CHECKSUMS, 65536)
    metadata = api.request("GET", f"repos/{REPOSITORY}")
    require(type(metadata.get("private")) is bool, "Repository visibility is unavailable")
    private = metadata["private"]
    latest = api.latest()
    if latest is not None:
        require(published_version(api, latest) <= version(manifest["version"]), "Candidate is stale")
    tag = manifest_tag(manifest)
    release = api.find_release(tag)
    if release is None:
        public_head = api.request("GET", f"repos/{REPOSITORY}/commits/{metadata['default_branch']}")["sha"]
        digest(public_head, 40)
        release = api.request("POST", f"repos/{REPOSITORY}/releases", {
            "tag_name": tag, "target_commitish": public_head, "name": "DSPark Studio " + tag[1:],
            "body": "Verified candidate " + manifest["candidate"] + ". Pending distribution checks.",
            "draft": True, "prerelease": False, "make_latest": "false"})
    existing = asset_map(release)
    require(not private or release.get("draft") is True, "Private preparation requires a draft")
    require(set(existing) <= set(files), "Draft contains unexpected assets")
    for name, data in files.items():
        if name in existing:
            require(api.asset(existing[name], len(data)) == data, "Existing asset differs; never overwrite: " + name)
        else:
            require(release.get("draft") is True, "Published releases are immutable")
            api.upload(release["id"], name, data)
    # Independently read the uploaded cohort before dispatch; retries reuse exact assets.
    release = api.request("GET", f"repos/{REPOSITORY}/releases/{release['id']}")
    with tempfile.TemporaryDirectory(prefix="studio-upload-verify-") as tmp:
        download(api, release, Path(tmp) / "assets", sha(files[MANIFEST]))
    if release.get("draft") is True and not private:
        api.request("POST", f"repos/{REPOSITORY}/dispatches", {"event_type": "studio-candidate", "client_payload": {
            "release_id": release["id"], "version": manifest["version"], "manifest_sha256": sha(files[MANIFEST])}})
    return {"releaseId": release["id"], "version": manifest["version"], "manifestSha256": sha(files[MANIFEST]),
            "state": "private-draft-ready" if private else
                     "awaiting-public-ci" if release.get("draft") else "already-published"}


def promote(api, release_id, expected_digest, expected_license):
    require(type(release_id) is int and release_id > 0, "Invalid release ID")
    digest(expected_digest)
    metadata = api.request("GET", f"repos/{REPOSITORY}")
    require(metadata.get("private") is False, "Public release approval is required")
    release = api.request("GET", f"repos/{REPOSITORY}/releases/{release_id}")
    require(release.get("prerelease") is False, "Prereleases are not stable candidates")
    with tempfile.TemporaryDirectory(prefix="studio-distribution-check-") as tmp:
        directory = Path(tmp) / "assets"
        manifest = download(api, release, directory, expected_digest)
        verify(directory, expected_digest, expected_license)
    latest = api.latest()
    if latest is not None:
        require(published_version(api, latest) <= version(manifest["version"]),
                "A newer public version already exists")
    if release.get("draft") is True:
        api.request("PATCH", f"repos/{REPOSITORY}/releases/{release_id}", {
            "draft": False, "make_latest": "true", "body": (
                "DAW for AI Agents. Free for personal and commercial content creation.\n\n"
                "Download the package for your system and verify SHA256SUMS. See LICENSE for product integration, "
                "redistribution and hosted-service terms. Packages are unsigned.\n\n"
                "Distribution checks verified the exact candidate assets and license. No package was rebuilt.\n"
                "Manifest SHA-256: " + expected_digest)})
    return {"version": manifest["version"], "releaseId": release_id, "manifestSha256": expected_digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_subparsers(dest="mode", required=True)
    for name in ("verify", "stage"):
        sub = modes.add_parser(name)
        sub.add_argument("--directory", type=Path, required=True)
        sub.add_argument("--license", type=Path, required=True)
    sub = modes.add_parser("promote")
    sub.add_argument("--release-id", type=int, required=True)
    sub.add_argument("--manifest-sha256", required=True)
    sub.add_argument("--license", type=Path, required=True)
    args = parser.parse_args()
    license_text = read(args.license, 65536)
    if args.mode == "verify":
        result = verify(args.directory, expected_license=license_text)[0]
        print("Verified distribution " + result["version"])
        return
    token = os.environ.get("GH_TOKEN")
    require(bool(token), "GH_TOKEN must be supplied by the authorized publishing job")
    api = GitHub(token)
    result = (stage(api, args.directory, license_text) if args.mode == "stage" else
              promote(api, args.release_id, args.manifest_sha256, license_text))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

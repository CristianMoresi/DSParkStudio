#!/usr/bin/env python3
"""Distribution contract controls with synthetic files and an in-memory GitHub API.

Dependencies: Python standard library and distribution.py. No network or installation.
"""
import copy
from pathlib import Path
import tempfile
import unittest
import urllib.request

import distribution as d

LICENSE = b"DSPark Studio License Agreement\nTest fixture only.\n"


def cohort(folder, version="0.1.0.21", release_tag=None):
    folder.mkdir()
    manifest = dict(schemaVersion=1, version=version, candidate="a" * 64,
                    framework="b" * 40, targets={}, licenses={})
    if release_tag is not None:
        manifest.update(schemaVersion=2, releaseTag=release_tag)
    for target, names in d.TARGETS.items():
        assets = {}
        for name in names:
            data = ("synthetic " + name + version).encode()
            (folder / name).write_bytes(data)
            assets[name] = d.record(name, data)
        manifest["targets"][target] = dict(inputDigest="c" * 64, packageVersion=version, carried=False, assets=assets)
    for name in d.LICENSES:
        data = LICENSE if name == "LICENSE" else b"Synthetic dependency license\n"
        (folder / name).write_bytes(data)
        manifest["licenses"][name] = d.record(name, data)
    d.seal(folder, manifest)
    return manifest


class MemoryGitHub(d.GitHub):
    def __init__(self):
        self.private = False
        self.releases = []
        self.blobs = {}
        self.calls = []
        self.uploads = 0
        self.interrupt_after = None

    def latest(self):
        published = [r for r in self.releases if not r["draft"]]
        return copy.deepcopy(max(published, key=lambda r: d.tag_version(r["tag_name"]))) if published else None

    def request(self, method, path, data=None, binary=False, maximum=2 * 1024 * 1024):
        self.calls.append((method, path, copy.deepcopy(data)))
        route = path.removeprefix("repos/" + d.REPOSITORY)
        if method == "GET" and route == "":
            return dict(private=self.private, default_branch="main")
        if method == "GET" and route.startswith("/commits/"):
            return dict(sha="d" * 40)
        if method == "GET" and route.startswith("/releases?"):
            return copy.deepcopy(self.releases)
        if method == "GET" and route.startswith("/releases/assets/"):
            value = self.blobs[int(route.rsplit("/", 1)[1])]
            d.require(len(value) <= maximum, "Response exceeds its bound")
            return value
        if method in ("GET", "PATCH") and route.startswith("/releases/"):
            release = next(r for r in self.releases if r["id"] == int(route.rsplit("/", 1)[1]))
            if method == "PATCH":
                release.update(data)
            return copy.deepcopy(release)
        if method == "POST" and route == "/releases":
            release = dict(data, id=len(self.releases) + 1, assets=[])
            self.releases.append(release)
            return copy.deepcopy(release)
        if method == "POST" and route == "/dispatches":
            return None
        raise AssertionError((method, route))

    def upload(self, release_id, name, data):
        if self.interrupt_after is not None and self.uploads == self.interrupt_after:
            raise ConnectionError("Synthetic upload interruption")
        asset_id = len(self.blobs) + 1
        self.blobs[asset_id] = data
        self.uploads += 1
        next(r for r in self.releases if r["id"] == release_id)["assets"].append(
            dict(id=asset_id, name=name, size=len(data), state="uploaded"))


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="studio-distribution-control-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.files = self.root / "cohort"
        self.manifest = cohort(self.files)
        self.api = MemoryGitHub()

    def test_complete_cohort_round_trip(self):
        self.assertEqual(d.verify(self.files, expected_license=LICENSE)[0], self.manifest)
        staged = d.stage(self.api, self.files, LICENSE)
        self.assertEqual(staged["state"], "awaiting-public-ci")
        self.assertTrue(self.api.releases[0]["draft"])
        self.assertEqual(self.api.releases[0]["target_commitish"], "d" * 40)
        d.promote(self.api, 1, staged["manifestSha256"], LICENSE)
        self.assertFalse(self.api.releases[0]["draft"])
        self.assertEqual(d.stage(self.api, self.files, LICENSE)["state"], "already-published")
        self.assertEqual(self.api.uploads, 8)

    def test_product_release_and_subsequent_package_revision(self):
        first = self.root / "official"
        cohort(first, "1.0.0.0")
        staged = d.stage(self.api, first, LICENSE)
        self.assertEqual(self.api.releases[0]["tag_name"], "v1.0.0")
        d.promote(self.api, staged["releaseId"], staged["manifestSha256"], LICENSE)
        newer = self.root / "revision"
        cohort(newer, "1.0.0.1")
        staged = d.stage(self.api, newer, LICENSE)
        self.assertEqual(self.api.releases[1]["tag_name"], "v1.0.0.1")
        d.promote(self.api, staged["releaseId"], staged["manifestSha256"], LICENSE)
        with self.assertRaises(d.Refused):
            d.stage(self.api, first, LICENSE)

    def test_release_tag_contract(self):
        self.assertEqual(d.tag_version("v1.0.0"), (1, 0, 0, 0))
        self.assertEqual(d.tag_version("v0.1.0.21"), (0, 1, 0, 21))
        for tag in ("1.0.0", "v1.0", "v01.0.0", "v1.0.0.0", "v1.0.0.65536", "v1.0.0-rc1"):
            with self.subTest(tag=tag), self.assertRaises(d.Refused):
                d.tag_version(tag)

    def test_official_release_keeps_native_upgrade_revision(self):
        folder = self.root / "official-rebuilt"
        cohort(folder, "1.0.0.2", release_tag="v1.0.0")
        staged = d.stage(self.api, folder, LICENSE)
        self.assertEqual(self.api.releases[0]["tag_name"], "v1.0.0")
        self.assertEqual(self.api.releases[0]["name"], "DSPark Studio 1.0.0")
        d.promote(self.api, staged["releaseId"], staged["manifestSha256"], LICENSE)
        readback = d.download(self.api, self.api.releases[0], self.root / "readback")
        self.assertEqual(readback["version"], "1.0.0.2")
        older = self.root / "older-revision"
        cohort(older, "1.0.0.1")
        with self.assertRaises(d.Refused):
            d.stage(self.api, older, LICENSE)

    def test_official_tag_is_closed_and_matches_product(self):
        for tag in ("v0.1.0", "v1.0.0.2", "v1.0.1", "1.0.0", None):
            invalid = copy.deepcopy(self.manifest)
            invalid.update(schemaVersion=2, version="1.0.0.2", releaseTag=tag)
            with self.subTest(tag=tag), self.assertRaises(d.Refused):
                d.records(invalid)
        for schema, extra in ((1, {"releaseTag": "v0.1.0"}), (2, {}), (3, {}), (True, {})):
            invalid = copy.deepcopy(self.manifest)
            invalid.update(schemaVersion=schema, **extra)
            with self.subTest(schema=schema), self.assertRaises(d.Refused):
                d.records(invalid)

    def test_each_asset_is_hash_checked(self):
        for name in d.records(self.manifest):
            with self.subTest(name=name):
                path = self.files / name
                old = path.read_bytes()
                path.write_bytes(old[:-1] + b"!")
                with self.assertRaises(d.Refused):
                    d.verify(self.files)
                path.write_bytes(old)

    def test_exact_inventory(self):
        path = self.files / "source.cpp"
        path.write_text("Private source must never be accepted")
        with self.assertRaises(d.Refused):
            d.verify(self.files)
        path.unlink()
        (self.files / "LICENSE").unlink()
        with self.assertRaises(d.Refused):
            d.verify(self.files)

    def test_duplicate_and_nonfinite_json(self):
        for raw in (b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(d.Refused):
                d.decode(raw)

    def test_closed_manifest_and_asset_names(self):
        invalid = copy.deepcopy(self.manifest)
        invalid["sourceCommit"] = "f" * 40
        with self.assertRaises(d.Refused):
            d.records(invalid)
        invalid = copy.deepcopy(self.manifest)
        assets = invalid["targets"]["windows-x64"]["assets"]
        assets["../source"] = assets.pop(next(iter(assets)))
        with self.assertRaises(d.Refused):
            d.records(invalid)

    def test_bounds_and_boolean_confusion(self):
        for size in (True, 0, -1, d.MAX_ASSET + 1, "10"):
            invalid = copy.deepcopy(self.manifest)
            invalid["licenses"]["LICENSE"]["size"] = size
            with self.subTest(size=size), self.assertRaises(d.Refused):
                d.records(invalid)
        with self.assertRaises(d.Refused):
            d.decode(b" " * 65537)

    def test_version_and_carry_contract(self):
        for value in ("1.0.1", "1.2.3.65536", "01.0.0.21", "v0.1.0.21"):
            with self.subTest(value=value), self.assertRaises(d.Refused):
                d.version(value)
        carried = copy.deepcopy(self.manifest)
        entry = carried["targets"]["linux-x64"]
        entry["packageVersion"] = "0.1.0.20"
        with self.assertRaises(d.Refused):
            d.records(carried)
        entry["carried"] = True
        d.records(carried)
        entry["packageVersion"] = "0.1.0.22"
        with self.assertRaises(d.Refused):
            d.records(carried)

    def test_dispatch_digest_and_license_binding(self):
        with self.assertRaises(d.Refused):
            d.verify(self.files, expected_digest="0" * 64)
        with self.assertRaises(d.Refused):
            d.stage(self.api, self.files, LICENSE + b"changed")
        staged = d.stage(self.api, self.files, LICENSE)
        with self.assertRaises(d.Refused):
            d.promote(self.api, 1, "0" * 64, LICENSE)
        with self.assertRaises(d.Refused):
            d.promote(self.api, 1, staged["manifestSha256"], LICENSE + b"changed")
        self.assertTrue(self.api.releases[0]["draft"])

    def test_checksums_must_be_complete_and_exact(self):
        (self.files / d.CHECKSUMS).write_text("0  LICENSE\n")
        with self.assertRaises(d.Refused):
            d.verify(self.files)

    def test_interrupted_upload_resumes_the_same_draft(self):
        self.api.interrupt_after = 3
        with self.assertRaises(ConnectionError):
            d.stage(self.api, self.files, LICENSE)
        self.assertEqual(len(self.api.releases), 1)
        self.api.interrupt_after = None
        staged = d.stage(self.api, self.files, LICENSE)
        self.assertEqual(staged["releaseId"], 1)
        self.assertEqual(len(self.api.releases), 1)
        self.assertEqual(self.api.uploads, 8)

    def test_differing_existing_assets_never_overwritten(self):
        d.stage(self.api, self.files, LICENSE)
        first = self.api.releases[0]["assets"][0]
        self.api.blobs[first["id"]] = b"x" * first["size"]
        before = copy.deepcopy(self.api.blobs)
        with self.assertRaises(d.Refused):
            d.stage(self.api, self.files, LICENSE)
        self.assertEqual(self.api.blobs, before)

    def test_missing_published_asset_is_not_repaired_in_place(self):
        staged = d.stage(self.api, self.files, LICENSE)
        d.promote(self.api, 1, staged["manifestSha256"], LICENSE)
        self.api.releases[0]["assets"].pop()
        with self.assertRaises(d.Refused):
            d.stage(self.api, self.files, LICENSE)
        self.assertEqual(self.api.uploads, 8)

    def test_stale_candidate_cannot_replace_latest(self):
        old = d.stage(self.api, self.files, LICENSE)
        newer = self.root / "new"
        cohort(newer, "0.1.0.22")
        new = d.stage(self.api, newer, LICENSE)
        d.promote(self.api, new["releaseId"], new["manifestSha256"], LICENSE)
        with self.assertRaises(d.Refused):
            d.promote(self.api, old["releaseId"], old["manifestSha256"], LICENSE)
        with self.assertRaises(d.Refused):
            d.stage(self.api, self.files, LICENSE)

    def test_private_draft_is_verified_without_dispatch_or_publication(self):
        self.api.private = True
        staged = d.stage(self.api, self.files, LICENSE)
        self.assertEqual(staged["state"], "private-draft-ready")
        self.assertTrue(self.api.releases[0]["draft"])
        self.assertEqual(self.api.uploads, 8)
        dispatches = lambda: [call for call in self.api.calls if call[1].endswith('/dispatches')]
        self.assertFalse(dispatches())
        with self.assertRaises(d.Refused):
            d.promote(self.api, 1, staged["manifestSha256"], LICENSE)
        self.api.private = False
        staged = d.stage(self.api, self.files, LICENSE)
        self.assertEqual(staged["state"], "awaiting-public-ci")
        self.assertEqual(self.api.uploads, 8)
        self.assertEqual(len(dispatches()), 1)
        self.api.private = True
        with self.assertRaises(d.Refused):
            d.promote(self.api, 1, staged["manifestSha256"], LICENSE)

    def test_private_repository_does_not_modify_an_existing_published_release(self):
        staged = d.stage(self.api, self.files, LICENSE)
        d.promote(self.api, 1, staged["manifestSha256"], LICENSE)
        self.api.private = True
        before = copy.deepcopy(self.api.releases)
        with self.assertRaises(d.Refused):
            d.stage(self.api, self.files, LICENSE)
        self.assertEqual(self.api.releases, before)

    def test_remote_inventory_and_tag_are_bound(self):
        staged = d.stage(self.api, self.files, LICENSE)
        release = self.api.releases[0]
        release["tag_name"] = "v0.1.0.22"
        with self.assertRaises(d.Refused):
            d.promote(self.api, 1, staged["manifestSha256"], LICENSE)
        release["tag_name"] = "v0.1.0.21"
        release["assets"][0]["state"] = "starter"
        with self.assertRaises(d.Refused):
            d.promote(self.api, 1, staged["manifestSha256"], LICENSE)

    def test_redirect_strips_cross_host_authorization(self):
        req = urllib.request.Request("https://api.github.com/asset", headers={"Authorization": "Bearer fixture"})
        redirect = d.AssetRedirect()
        changed = redirect.redirect_request(req, None, 302, "", {}, "https://release-assets.githubusercontent.com/x")
        self.assertFalse(changed.has_header("Authorization"))
        for url in ("http://api.github.com/x", "https://example.com/x", "https://api.github.com@evil.com/x"):
            with self.subTest(url=url), self.assertRaises(d.Refused):
                redirect.redirect_request(req, None, 302, "", {}, url)


if __name__ == "__main__":
    unittest.main()

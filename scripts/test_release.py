"""Offline release state-machine tests. Run with python3 -B scripts/test_release.py."""
import copy
import hashlib
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import release as r


class FakeGitHub:
    def __init__(self):
        self.rows = []
        self.files = {}
        self.next_id = 0
        self.fail_rename = None
        self.commit_comparison = "ahead"

    def new_id(self):
        self.next_id += 1
        return self.next_id

    def releases(self):
        return copy.deepcopy(self.rows)

    def api(self, path, *, method="GET", body=None, all_pages=False):
        if path.startswith("tags?"):
            return []
        if path.startswith("compare/"):
            return {"status": self.commit_comparison}
        row = next(v for v in self.rows if v["id"] == int(path.split("/")[1]))
        if body:
            row.update(body)
        return copy.deepcopy(row)

    def create(self, tag, title, body):
        assert not any(row["tag_name"] == tag for row in self.rows)
        row = dict(tag_name=tag, draft=True, name=title, body=body, id=self.new_id(), assets=[])
        self.rows.append(row)
        return copy.deepcopy(row)

    def upload(self, rid, path, name, label):
        row = next(v for v in self.rows if v["id"] == rid)
        assert not r.asset(row, name)
        data = path.read_bytes()
        item = dict(id=self.new_id(), name=name, label=label, size=len(data), state="uploaded",
                    digest="sha256:" + hashlib.sha256(data).hexdigest())
        self.files[item["id"]] = data
        row["assets"].append(item)
        return copy.deepcopy(item)

    def rename(self, old, name, label):
        if name == self.fail_rename:
            self.fail_rename = None
            raise OSError("Interrupted asset rename")
        item = next(a for row in self.rows for a in row["assets"] if a["id"] == old["id"])
        item.update(name=name, label=label)
        return copy.deepcopy(item)

    def remove(self, old):
        for row in self.rows:
            row["assets"][:] = [a for a in row["assets"] if a["id"] != old["id"]]
        del self.files[old["id"]]

    def download(self, item, path):
        path.write_bytes(self.files[item["id"]])
        assert r.matches(item, r.digest(path), path.stat().st_size)



class PluginReleaseTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(self.enterContext(TemporaryDirectory()))
        self.gh = FakeGitHub()
        self.enterContext(patch.object(r, "verify"))
        self.enterContext(patch.dict(os.environ, STAGING_TAG="", GITHUB_SHA="a" * 40))
        self.info = {"package": r.PACKAGE, "version": "0.170.0", "versionCode": 1700003,
                     "buildId": "1" * 64, "signerSha256": r.digest(r.PIN)}
        self.write_apks("first")

    def write_apks(self, value):
        self.info["artifacts"] = []
        for abi, name in r.ABIFILES.items():
            file = self.out / name
            file.write_bytes((abi + value).encode())
            self.info["artifacts"].append(dict(abi=abi, file=name, size=file.stat().st_size, sha256=r.digest(file)))
        r.write_json(self.out / r.BUILD, self.info)

    def test_publish_retry_and_downgrade(self):
        r.publish(self.gh, self.out)
        fixed, = self.gh.releases()
        self.assertEqual({a["name"] for a in fixed["assets"]}, {*r.ABIFILES.values(), r.MANIFEST, r.ICON.name})
        self.assertFalse(fixed["draft"])
        self.write_apks("second")
        with self.assertRaisesRegex(ValueError, "newer versionCode"):
            r.publish(self.gh, self.out)
        self.info["versionCode"] += 1
        self.write_apks("second")
        self.gh.fail_rename = r.MANIFEST
        with self.assertRaisesRegex(OSError, "Interrupted"):
            r.publish(self.gh, self.out)
        r.publish(self.gh, self.out)
        _, _, published = r.current(self.gh, self.out)
        self.assertEqual(published["artifacts"], self.info["artifacts"])
        self.assertEqual(published["versionCode"], 1700004)

    def test_staging_and_missing_variant(self):
        tag = "incoming-codex-1700003"
        draft = self.gh.create(tag, "Incoming", "")
        for name in (r.BUILD, *r.ABIFILES.values()):
            self.gh.upload(draft["id"], self.out/name, name, "")
        os.environ["STAGING_TAG"] = tag
        self.assertEqual(r.prepare(self.gh, self.out), {"publish": True, "build": False})
        self.info["artifacts"].pop()
        with self.assertRaisesRegex(ValueError, "Codex ABI"):
            r.codex_entry(self.info)

    def test_android_publish_preserves_codex_and_skips_unchanged_build(self):
        r.publish(self.gh, self.out)
        plan = r.prepare(self.gh, self.out, "android")
        self.assertTrue(plan["build"])
        artifacts = []
        for abi, name in r.ANDROID_FILES.items():
            path = self.out / name
            path.write_bytes(abi.encode())
            artifacts.append(dict(abi=abi, file=name, size=path.stat().st_size, sha256=r.digest(path)))
        info = dict(package=r.ANDROID_PACKAGE, version=plan["version"], versionCode=plan["code"],
                    sourceCommit=plan["sourceCommit"], signerSha256=r.digest(r.PIN), artifacts=artifacts)
        r.write_json(self.out / "android-build.json", info)
        r.publish(self.gh, self.out, "android")
        _, store, android = r.current(self.gh, self.out, r.ANDROID_PACKAGE)
        self.assertEqual({entry["package"] for entry in store["plugins"]}, {r.PACKAGE, r.ANDROID_PACKAGE})
        self.assertEqual(android["category"], "development-environment")
        self.assertFalse(r.prepare(self.gh, self.out, "android")["publish"])
        artifacts.pop()
        with self.assertRaisesRegex(ValueError, "environment ABI"):
            r.android_entry(info)

    def test_rejects_universal_plugin(self):
        plugin = r.codex_entry(self.info)
        artifact = dict(plugin["artifacts"][0], abi="universal")
        with self.assertRaisesRegex(ValueError, "distinct Android ABI"):
            r.validate_store({"schemaVersion": 2, "plugins": [dict(plugin, artifacts=[artifact])]})

    def test_preserves_other_plugins(self):
        r.publish(self.gh, self.out)
        release, store, codex = r.current(self.gh, self.out)
        other = dict(codex, name="Another", package=r.PREFIX + "another",
                     artifacts=[dict(codex["artifacts"][0], file="Another-arm64-v8a.apk")])
        store["plugins"].append(other)
        r.write_json(self.out / r.MANIFEST, store)
        r.replace_asset(self.gh, release["id"], self.out / r.MANIFEST, "Catalog")
        r.publish(self.gh, self.out)
        self.assertIn(other, r.current(self.gh, self.out)[1]["plugins"])


if __name__ == "__main__":
    unittest.main()

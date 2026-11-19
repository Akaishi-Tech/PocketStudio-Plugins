"""Check publication and recovery of the store catalog using an in-memory release."""
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
        self.fail_promotion = False

    def releases(self):
        return copy.deepcopy(self.rows)

    def assets(self, release):
        row = next(v for v in self.rows if v["id"] == release["id"])
        release["assets"] = copy.deepcopy(row["assets"])
        return release

    def api(self, path, *, method="GET", body=None, all_pages=False):
        self.assert_path(path)
        row = next(v for v in self.rows if v["id"] == int(path.split("/")[1]))
        if body:
            row.update(body)
        return copy.deepcopy(row)

    @staticmethod
    def assert_path(path):
        assert path.startswith("releases/")

    def create(self, tag, body):
        row = dict(tag_name=tag, draft=True, body=body, id=len(self.rows) + 1, assets=[])
        self.rows.append(row)
        return copy.deepcopy(row)

    def upload(self, rid, path, name, label):
        row = next(v for v in self.rows if v["id"] == rid)
        assert not r.asset(row, name)
        data = path.read_bytes()
        item = dict(id=len(self.files) + 1, name=name, label=label, size=len(data), state="uploaded",
                    digest="sha256:" + hashlib.sha256(data).hexdigest())
        self.files[item["id"]] = data
        row["assets"].append(item)
        return copy.deepcopy(item)

    def rename(self, item, name):
        for release in self.rows:
            for row in release["assets"]:
                if row["id"] == item["id"]:
                    if self.fail_promotion and name == r.MANIFEST and row["name"] == r.STAGED_CATALOG:
                        self.fail_promotion = False
                        raise OSError("Interrupted catalog promotion")
                    assert not r.asset(release, name)
                    row["name"] = name
                    return copy.deepcopy(row)
        raise AssertionError("Unknown asset ID")

    def remove(self, item):
        for release in self.rows:
            release["assets"] = [v for v in release["assets"] if v["id"] != item["id"]]

    def download(self, item, path):
        path.write_bytes(self.files[item["id"]])
        assert r.matches(item, r.digest(path), path.stat().st_size)


class PluginReleaseTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(self.enterContext(TemporaryDirectory()))
        self.gh = FakeGitHub()
        self.enterContext(patch.object(r, "verify"))
        self.enterContext(patch.dict(os.environ, STAGING_TAG="", GITHUB_SHA="a" * 40))

    def inputs(self, plugin, code=2, content="example"):
        info = dict(package=r.listing(plugin)["package"], version="1.0.0", versionCode=code,
                    sourceCommit=r.source_commit(), signerSha256=r.digest(r.PIN), artifacts=[])
        if plugin == "codex":
            info["buildId"] = "1" * 64
        for abi, name in r.abi_files(plugin).items():
            path = self.out / name
            path.write_bytes((abi + plugin + content).encode())
            info["artifacts"].append(dict(abi=abi, file=name, size=path.stat().st_size, sha256=r.digest(path)))
        r.write_json(self.out / f"{plugin}-build.json", info)
        return info

    def test_all_publish_versioned_and_retry(self):
        for plugin in r.PLUGINS:
            self.inputs(plugin)
        r.publish(self.gh, self.out, r.PLUGINS, True)
        release, store, _ = r.current(self.gh, self.out)
        self.assertNotIn("name", release)
        self.assertEqual({p["package"] for p in store["plugins"]}, {r.listing(k)["package"] for k in r.PLUGINS})
        self.assertIsNotNone(r.asset(release, r.MANIFEST))
        for row in release["assets"]:
            self.assertRegex(row["name"], r"1\.0\.0-2|^plugin-store\.json$")
        r.publish(self.gh, self.out, r.PLUGINS)
        self.assertEqual(release, self.gh.releases()[0])

    def test_failed_promotion_restores_catalog(self):
        self.inputs("codex")
        r.publish(self.gh, self.out)
        self.inputs("codex", content="changed")
        with self.assertRaisesRegex(ValueError, "newer versionCode"):
            r.publish(self.gh, self.out)
        self.inputs("codex", code=3, content="changed")
        self.gh.fail_promotion = True
        with self.assertRaisesRegex(OSError, "Interrupted"):
            r.publish(self.gh, self.out)
        self.assertEqual(r.current(self.gh, self.out)[2]["versionCode"], 2)
        r.publish(self.gh, self.out)
        self.assertEqual(r.current(self.gh, self.out)[2]["versionCode"], 3)

        release = self.gh.releases()[0]
        self.assertFalse(r.asset(release, r.STAGED_CATALOG))
        self.assertFalse(r.asset(release, r.BACKUP_CATALOG))

    def test_recover_interrupted_swap_and_clear_title(self):
        self.inputs("codex")
        r.publish(self.gh, self.out)
        release, original, _ = r.current(self.gh, self.out)
        self.gh.rows[0]["name"] = "Historical title"
        self.gh.rename(r.asset(release, r.MANIFEST), r.BACKUP_CATALOG)
        self.assertEqual(r.current(self.gh, self.out)[1], original)
        self.inputs("android")
        r.publish(self.gh, self.out, ("android",))
        release, store, _ = r.current(self.gh, self.out)
        self.assertEqual(release["name"], "")
        self.assertIn(original["plugins"][0], store["plugins"])
        self.assertFalse(r.asset(release, r.BACKUP_CATALOG))
        self.assertFalse(r.asset(release, r.STAGED_CATALOG))
        self.assertFalse(r.prepare(self.gh, self.out, "android")["publish"])

    def test_missing_existing_artifact_prevents_catalog_change(self):
        self.inputs("codex")
        r.publish(self.gh, self.out)
        release, original, item = r.current(self.gh, self.out)
        self.gh.remove(r.asset(release, item["artifacts"][0]["file"]))
        self.inputs("android")
        with self.assertRaisesRegex(ValueError, "Missing or damaged release APK"):
            r.publish(self.gh, self.out, ("android",))
        self.assertEqual(r.current(self.gh, self.out)[1], original)

    def test_staging_and_missing_variant(self):
        info = self.inputs("avf")
        tag = "incoming-avf-2"
        draft = self.gh.create(tag, "")
        for name in ("avf-build.json", *r.abi_files("avf").values()):
            self.gh.upload(draft["id"], self.out/name, name, "")
        os.environ["STAGING_TAG"] = tag
        self.assertEqual(r.prepare(self.gh, self.out, "avf"), {"publish": True, "build": False})
        info["artifacts"].pop()
        with self.assertRaisesRegex(ValueError, "avf ABI"):
            r.plugin_entry("avf", info)

    def test_check_source_pin(self):
        info = self.inputs("avf")
        info["sourceCommit"] = "b" * 40
        r.write_json(self.out / "avf-build.json", info)
        with self.assertRaisesRegex(ValueError, "Wrong source commit"):
            r.checked_inputs(self.out, ["avf"], True)


if __name__ == "__main__":
    unittest.main()

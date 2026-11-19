"""Check publication and recovery of the store catalog using an in-memory release."""
import copy
import gzip
import hashlib
import io
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile
from unittest.mock import patch

import release as r


class ManifestTests(unittest.TestCase):
    def test_verifies_package_and_both_versions(self):
        sdk, apk = Path("/sdk"), Path("signed.apk")
        entry = dict(package="com.hcgstudio.pocketstudio.avfplugin", versionCode=3, version="1.0.2")
        with patch.object(r.subprocess, "check_output", return_value="com.hcgstudio.pocketstudio.avfplugin 3 1.0.2\n") as run:
            r.verify_apk_manifest(sdk, apk, entry)
            run.assert_called_once_with([sdk / "cmdline-tools/latest/bin/apkanalyzer", "apk", "summary", apk], text=True)
        for mismatch in ("other.app 3 1.0.2", "com.hcgstudio.pocketstudio.avfplugin 4 1.0.2",
                         "com.hcgstudio.pocketstudio.avfplugin 3 1.0.1"):
            with self.subTest(summary=mismatch), patch.object(r.subprocess, "check_output", return_value=mismatch):
                with self.assertRaisesRegex(ValueError, "Package or version"):
                    r.verify_apk_manifest(sdk, apk, entry)


    def test_apk_verification_checks_manifest_and_embedded_payload(self):
        with TemporaryDirectory() as work, patch.dict(os.environ, ANDROID_HOME="/sdk"):
            folder = Path(work)
            apk = folder / "codex.apk"
            build_id = "1" * 64
            with zipfile.ZipFile(apk, "w") as archive:
                archive.writestr("lib/x86_64/libcodex_app_server.so", b"test payload")
                archive.writestr("assets/codex-version", "1.0.2")
                archive.writestr("assets/codex-build", build_id)
            artifact = dict(file=apk.name, abi="x86_64", size=apk.stat().st_size, sha256=r.digest(apk))
            entry = dict(package=r.PACKAGE, version="1.0.2", versionCode=3, buildId=build_id, artifacts=[artifact])
            with patch.object(r.subprocess, "check_output", return_value=f"{r.PACKAGE} 3 1.0.2\n") as runner:
                r.verify(entry, folder)
                runner.assert_called_once_with([Path("/sdk/cmdline-tools/latest/bin/apkanalyzer"),
                                                "apk", "summary", apk], text=True)
                entry["buildId"] = "2" * 64
                with self.assertRaisesRegex(ValueError, "Codex payload identity mismatch"):
                    r.verify(entry, folder)


class LinuxImageTests(unittest.TestCase):
    def test_gzip_bytes_survive_apk_packaging(self):
        compressed = gzip.compress(b"Debian filesystem")
        for payload, checksum in (("debian-image.bin", "debian.img.sha256"),
                                  ("debian-rootfs.bin", "debian-rootfs.sha256")):
            for content, method, error in [
                (compressed, zipfile.ZIP_STORED, None),
                (b"expanded by aapt", zipfile.ZIP_STORED, "gzip stream"),
                (compressed, zipfile.ZIP_DEFLATED, "stored verbatim"),
                (compressed + b"changed", zipfile.ZIP_STORED, "checksum mismatch"),
            ]:
                with self.subTest(payload=payload, error=error):
                    buffer = io.BytesIO()
                    with zipfile.ZipFile(buffer, "w") as apk:
                        apk.writestr("assets/" + checksum, hashlib.sha256(compressed).hexdigest() + "\n")
                        apk.writestr("assets/" + payload, content, compress_type=method)
                    with zipfile.ZipFile(buffer) as apk:
                        if error is None:
                            r.verify_gzip_asset(apk, payload, checksum)
                        else:
                            with self.assertRaisesRegex(ValueError, error):
                                r.verify_gzip_asset(apk, payload, checksum)


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

    def upload(self, rid, path, name):
        row = next(v for v in self.rows if v["id"] == rid)
        assert not r.asset(row, name)
        data = path.read_bytes()
        item = dict(id=len(self.files) + 1, name=name, label=None, size=len(data), state="uploaded",
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
                    collision = r.asset(release, name)
                    assert collision is None or collision["id"] == item["id"]
                    row.update(name=name, label="")
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

    def run_record(self, number=1, attempt=1):
        return dict(id=900000 + number, run_number=number, run_attempt=attempt, head_sha="a" * 40,
                    repository={"full_name": r.SOURCE_REPO}, head_repository={"full_name": r.SOURCE_REPO},
                    status="completed", conclusion="success", head_branch="main", event="push",
                    path=".github/workflows/build-all.yml")

    def inputs(self, plugin, code=2, content="example"):
        info = dict(package=r.listing(plugin)["package"], version="1.0.0", versionCode=code,
                    sourceCommit="a" * 40, signerSha256="a" * 64, artifacts=[])
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
        r.publish(self.gh, self.out, r.PLUGINS, self.run_record())
        release, store, _ = r.current(self.gh, self.out)
        self.assertNotIn("name", release)
        self.assertEqual({p["package"] for p in store["plugins"]}, {r.listing(k)["package"] for k in r.PLUGINS})
        self.assertIsNotNone(r.asset(release, r.MANIFEST))
        for row in release["assets"]:
            self.assertFalse(row.get("label"))
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
        restored = r.current(self.gh, self.out)
        self.assertEqual(restored[2]["versionCode"], 2)
        self.assertFalse(r.asset(restored[0], r.MANIFEST).get("label"))
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
        with patch.object(r.subprocess, "check_output", return_value="a" * 40):
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
            self.gh.upload(draft["id"], self.out/name, name)
        os.environ["STAGING_TAG"] = tag
        self.assertEqual(r.prepare(self.gh, self.out, "avf"), {"publish": True, "build": False})
        info["artifacts"].pop()
        with self.assertRaisesRegex(ValueError, "avf ABI"):
            r.plugin_entry("avf", info)

    def test_signer_is_metadata_from_the_build(self):
        info = self.inputs("codex")
        for fingerprint in ("0" * 64, "f" * 64):
            info["signerSha256"] = fingerprint
            self.assertEqual(r.plugin_entry("codex", info)["signerSha256"], fingerprint)
        info["signerSha256"] = "not-a-sha256"
        with self.assertRaisesRegex(ValueError, "signerSha256 metadata"):
            r.plugin_entry("codex", info)

    def test_check_artifact_provenance(self):
        info = self.inputs("avf")
        info["sourceCommit"] = "b" * 40
        r.write_json(self.out / "avf-build.json", info)
        with self.assertRaisesRegex(ValueError, "Wrong source commit"):
            r.checked_inputs(self.out, ["avf"], self.run_record())

    def test_completed_source_runs_and_retries(self):
        latest = self.run_record(10)
        self.inputs("codex", code=4)
        with patch.object(r, "resolve_source_run", return_value=latest):
            self.assertEqual(r.select(self.gh, self.out, ["codex"], "")["changed"], "true")
            r.publish(self.gh, self.out, ["codex"], latest)
            self.assertEqual(r.select(self.gh, self.out, ["codex"], "")["changed"], "false")
        release, _, entry = r.current(self.gh, self.out)
        self.assertEqual((entry["sourceRunNumber"], entry["sourceRunId"], entry["sourceRunAttempt"]), (10, 900010, 1))
        self.inputs("codex", code=2, content="stale")
        r.publish(self.gh, self.out, ["codex"], self.run_record(9))
        self.assertEqual(release, self.gh.releases()[0])
        with patch.object(r, "resolve_source_run", return_value=self.run_record(10, 2)):
            self.assertEqual(r.select(self.gh, self.out, ["codex"], "")["changed"], "true")

    def test_selects_successful_default_branch_source_runs(self):
        newest = self.run_record(12)
        failed = {**self.run_record(14), "conclusion": "failure"}
        other = {**self.run_record(13), "head_repository": {"full_name": "some/fork"}}
        with patch.object(r, "source_api", side_effect=[
                {"default_branch": "main"}, {"workflow_runs": [failed, other, newest, self.run_record(11)]}]):
            self.assertEqual(r.resolve_source_run("latest"), newest)
        with patch.object(r, "source_api", side_effect=[{"default_branch": "main"}, other]):
            with self.assertRaisesRegex(ValueError, "default branch"):
                r.resolve_source_run(str(other["id"]))
        with patch.object(r, "source_api", side_effect=[{"default_branch": "main"}, {"workflow_runs": []}]):
            self.assertIsNone(r.resolve_source_run(""))

    def test_listing_only_updates_description_and_icon_without_new_apks(self):
        self.inputs("avf")
        r.publish(self.gh, self.out, ["avf"], self.run_record(4))
        release, _, before = r.current(self.gh, self.out, r.PREFIX + "avfplugin")
        metadata = r.listing("avf")
        updated = dict(metadata, description="A new description")
        with patch.object(r, "listing", return_value=updated):
            r.update_listings(self.gh, self.out, ["avf"])
        release, store, after = r.current(self.gh, self.out, before["package"])
        self.assertEqual(after, dict(before, description="A new description"))
        replacement = self.out / "updated.png"
        replacement.write_bytes(b"Different icon")
        with patch.object(r, "icon_path", return_value=replacement), patch.object(r, "listing", return_value=updated):
            r.update_listings(self.gh, self.out, ["avf"])
            changed, _, after = r.current(self.gh, self.out, before["package"])
            self.assertRegex(after["icon"], r"^AVF-1\.0\.0-2-[0-9a-f]{16}\.png$")
            self.assertEqual(after["artifacts"], before["artifacts"])
            self.assertEqual(after["sourceRunId"], before["sourceRunId"])
            self.assertTrue(r.matches(r.asset(changed, after["icon"]), r.digest(replacement), replacement.stat().st_size))
            self.assertEqual({a["id"] for a in release["assets"] if a["name"].endswith(".apk")},
                             {a["id"] for a in changed["assets"] if a["name"].endswith(".apk")})
            r.update_listings(self.gh, self.out, ["avf"])
            self.assertEqual(changed, self.gh.releases()[0])


if __name__ == "__main__":
    unittest.main()

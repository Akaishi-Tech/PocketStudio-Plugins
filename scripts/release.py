"""Verify and publish first-party Pocket Studio plugins to the dedicated store release."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlencode
import zipfile

TAG = "plugin-store"
MANIFEST = "plugin-store.json"
BUILD = "codex-build.json"
ABIS = ("arm64-v8a", "x86_64")
PACKAGE = "com.hcgstudio.pocketstudio.codexplugin"
PREFIX = "com.hcgstudio.pocketstudio."
ABIFILES = {abi: f"PocketStudio-Codex-Plugin-{abi}.apk" for abi in ABIS}
ANDROID_PACKAGE = "com.hcgstudio.pocketstudio.androidplugin"
ANDROID_FLAVORS = {"arm64-v8a": "arm64V8a", "armeabi-v7a": "armeabiV7a", "x86_64": "x86_64", "x86": "x86"}
ANDROID_FILES = {abi: f"PocketStudio-Android-Plugin-{abi}.apk" for abi in ANDROID_FLAVORS}
PIN = Path("release_signer.der")
ICON = Path("icons/Codex.png")
SOURCE = Path("source")
SCRIPT = SOURCE / "scripts/build-codex-plugin.py"
HEX = r"[0-9a-f]{64}"
VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(0|[1-9][0-9]*))?"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def asset(release, name):
    return next((a for a in release.get("assets", []) if a["name"] == name), None)


def matches(item, sha, size):
    return bool(item and item["state"] == "uploaded" and item.get("digest") == "sha256:" + sha
                and item["size"] == size)


class GitHub:
    def __init__(self):
        self.repo = os.environ["GITHUB_REPOSITORY"]
        self.root = "repos/" + self.repo

    def api(self, path, *, method="GET", body=None, all_pages=False):
        args = ["gh", "api", self.root + "/" + path, "--method", method]
        if all_pages:
            args.extend(["--paginate", "--slurp"])
        if body is not None:
            args.extend(["--input", "-"])
        result = subprocess.check_output(args, input=json.dumps(body).encode() if body is not None else None)
        value = json.loads(result) if result else None
        return [row for page in value for row in page] if all_pages else value

    def releases(self):
        # The publishing token can see draft releases as well as public ones.
        return self.api("releases?per_page=100", all_pages=True)

    def download(self, item, path):
        with path.open("wb") as out:
            subprocess.run(["gh", "api", f"{self.root}/releases/assets/{item['id']}",
                            "-H", "Accept: application/octet-stream"], stdout=out, check=True)
        require(matches(item, digest(path), path.stat().st_size), f"GitHub asset digest mismatch: {path.name}")

    def create(self, tag, title, body):
        return self.api("releases", method="POST", body={"tag_name": tag, "name": title, "body": body,
                        "draft": True, "target_commitish": os.environ["GITHUB_SHA"], "make_latest": "false"})

    def rename(self, item, name, label):
        return self.api(f"releases/assets/{item['id']}", method="PATCH", body={"name": name, "label": label})

    def upload(self, rid, path, name, label):
        query = urlencode({"name": name, "label": label})
        url = f"https://uploads.github.com/repos/{self.repo}/releases/{rid}/assets?{query}"
        mime = {".apk": "application/vnd.android.package-archive", ".json": "application/json", ".png": "image/png"}[path.suffix]
        return json.loads(subprocess.check_output(["gh", "api", url, "--method", "POST",
                          "--header", "Content-Type: " + mime, "--input", str(path)]))

    def remove(self, item):
        self.api(f"releases/assets/{item['id']}", method="DELETE")


def replace_asset(gh, rid, path, label):
    """Upload before changing the canonical name. Keep the old bytes until the new asset is readable."""
    release = gh.api(f"releases/{rid}")
    sha, size = digest(path), path.stat().st_size
    old = asset(release, path.name)
    if matches(old, sha, size):
        if old.get("label") != label:
            gh.rename(old, path.name, label)
        return
    stage_name = f"stage-{sha}-{path.name}"
    staged = asset(release, stage_name)
    if not matches(staged, sha, size):
        if staged:
            gh.remove(staged)
        staged = gh.upload(rid, path, stage_name, label)
        require(matches(staged, sha, size), "GitHub did not confirm the staged asset digest")
    if old:
        gh.rename(old, f"previous-{old['id']}-{path.name}", old.get("label") or "")
    try:
        current = gh.rename(staged, path.name, label)
        require(matches(current, sha, size), "Published asset digest mismatch")
    except Exception:
        if old and not asset(gh.api(f"releases/{rid}"), path.name):
            gh.rename(old, path.name, old.get("label") or "")
        raise



def version_components(value):
    match = re.fullmatch(VERSION, value)
    require(match, "Invalid plugin version")
    return tuple(int(part or 0) for part in match.groups())


def validate_store(store):
    require(store["schemaVersion"] == 2 and isinstance(store["plugins"], list), "Invalid plugin catalog")
    packages, files = set(), set()
    for plugin in store["plugins"]:
        pkg = plugin["package"]
        require(pkg.startswith(PREFIX) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+", pkg)
                and pkg not in packages, "Invalid or duplicate first-party plugin ID")
        packages.add(pkg)
        require(all(isinstance(plugin[k], str) and plugin[k].strip()
                    for k in ("name", "version", "category", "description", "developer")), "Missing plugin metadata")
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.png", plugin["icon"]), "Invalid icon filename")
        require(type(plugin["versionCode"]) is int and 0 < plugin["versionCode"] <= 2_100_000_000
                and plugin["signerSha256"] == digest(PIN), "Invalid versionCode or signer")
        require(isinstance(plugin["artifacts"], list) and plugin["artifacts"], "No plugin artifacts")
        abis = set()
        for artifact in plugin["artifacts"]:
            abi, name = artifact["abi"], artifact["file"]
            require(abi in {"arm64-v8a", "armeabi-v7a", "x86_64", "x86", "riscv64"} and abi not in abis,
                    "Expected a distinct Android ABI for each APK")
            require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.apk", name) and name not in files,
                    "Unsafe or duplicate plugin filename")
            require(type(artifact["size"]) is int and 0 < artifact["size"] <= 1_500_000_000
                    and re.fullmatch(HEX, artifact["sha256"]), "Invalid artifact size or digest")
            abis.add(abi)
            files.add(name)
    return store


def previous_store(gh, release, out):
    item = asset(release, MANIFEST)
    if not item or item["state"] != "uploaded":
        backups = [a for a in release.get("assets", []) if a["state"] == "uploaded"
                   and re.fullmatch(r"previous-\d+-" + re.escape(MANIFEST), a["name"])]
        item = max(backups, key=lambda a: a["id"], default=None)
    if not item:
        require(not release or release["draft"], "Published store catalog is missing")
        return {"schemaVersion": 2, "plugins": []}
    require(0 < item["size"] <= 262144, "Invalid catalog size")
    dest = out / "previous-store.json"
    gh.download(item, dest)
    return validate_store(read_json(dest))


def current(gh, out, package=PACKAGE):
    release = next((rel for rel in gh.releases() if rel["tag_name"] == TAG), {})
    store = previous_store(gh, release, out)
    previous = next((entry for entry in store["plugins"] if entry["package"] == package), None)
    return release, store, previous


def codex_entry(info):
    descriptor = read_json(Path("codex.json"))
    require(info["package"] == descriptor["package"] == PACKAGE, "Incorrect Codex package")
    entry = {key: info[key] for key in ("version", "versionCode", "buildId", "signerSha256", "artifacts")}
    entry.update(descriptor)
    validate_store({"schemaVersion": 2, "plugins": [entry]})
    version_components(entry["version"])
    require(re.fullmatch(HEX, entry["buildId"]), "Invalid Codex build ID")
    require({a["abi"]: a["file"] for a in entry["artifacts"]} == ABIFILES, "Missing or unexpected Codex ABI")
    return entry


def android_entry(info):
    descriptor = read_json(Path("android.json"))
    require(info["package"] == descriptor["package"] == ANDROID_PACKAGE, "Incorrect Android environment package")
    require(re.fullmatch(r"[0-9a-f]{40}", info["sourceCommit"]), "Invalid Android environment source revision")
    entry = {key: info[key] for key in ("version", "versionCode", "sourceCommit", "signerSha256", "artifacts")}
    entry.update(descriptor)
    validate_store({"schemaVersion": 2, "plugins": [entry]})
    require({a["abi"]: a["file"] for a in entry["artifacts"]} == ANDROID_FILES, "Missing or unexpected Android environment ABI")
    version_components(entry["version"])
    return entry


def build_android(out, plan):
    env = {**os.environ, "POCKETSTUDIO_VERSION_NAME": plan["version"], "POCKETSTUDIO_VERSION_CODE": str(plan["code"])}
    subprocess.run(["./gradlew", "--no-daemon", ":android-plugin:assembleRelease"], cwd=SOURCE, env=env, check=True)
    artifacts = []
    for abi, flavor in ANDROID_FLAVORS.items():
        variant = SOURCE / "build-support/android-plugin/build/outputs/apk" / flavor / "release"
        apk, = read_json(variant / "output-metadata.json")["elements"]
        path = out / ANDROID_FILES[abi]
        shutil.copyfile(variant / apk["outputFile"], path)
        artifacts.append({"abi": abi, "file": path.name, "size": path.stat().st_size, "sha256": digest(path)})
    write_json(out / "android-build.json", {"package": ANDROID_PACKAGE, "version": plan["version"],
               "versionCode": plan["code"], "sourceCommit": plan["sourceCommit"],
               "signerSha256": digest(PIN), "artifacts": artifacts})


def next_code(version, previous):
    major, minor, patch, revision = version_components(version)
    # Existing app-store releases allocated codes starting at base + 2.
    floor = major * 100000000 + minor * 10000 + patch * 100 + revision + 2
    return max(1700003, floor, previous["versionCode"] + 1 if previous else 0)


def verify(entry, out):
    tools = Path(os.environ["ANDROID_HOME"]) / "build-tools/37.0.0"
    for artifact in entry["artifacts"]:
        abi = artifact["abi"]
        apk = out / artifact["file"]
        require(apk.stat().st_size == artifact["size"] and digest(apk) == artifact["sha256"],
                f"APK size or SHA-256 mismatch: {apk.name}")
        signing = subprocess.check_output([tools / "apksigner", "verify", "--verbose", "--print-certs", apk], text=True)
        signers = re.findall(r"^(?:Signer #\d+|V[\d.]+ Signer:) certificate SHA-256 digest: ([0-9a-f]{64})$",
                             signing, re.M)
        require("Number of signers: 1" in signing.splitlines() and set(signers) == {digest(PIN)}
                and "Verified using v2 scheme (APK Signature Scheme v2): true" in signing.splitlines(),
                f"APK signer or v2 signature mismatch: {apk.name}")
        pkg = subprocess.check_output([tools / "aapt", "dump", "badging", apk], text=True)
        fields = re.search(r"^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']*)'", pkg, re.M)
        require(fields and fields.groups() == (entry["package"], str(entry["versionCode"]), entry["version"]),
                f"Package or version mismatch: {apk.name}")
        with zipfile.ZipFile(apk) as data:
            native = {name.split("/")[1] for name in data.namelist() if name.startswith("lib/")}
            require(native == {abi}, f"Incorrect native ABI in {apk.name}")
            if entry["package"] == ANDROID_PACKAGE:
                required = {f"lib/{abi}/libaapt2.so", f"lib/{abi}/libstudio_jdk_bin_java.so",
                            f"assets/local-jdk/{abi}.zip", "assets/template/gradlew"}
                require(required.issubset(data.namelist()), f"Android toolchain missing from {apk.name}")
                continue
            require(f"lib/{abi}/libcodex_app_server.so" in data.namelist(), f"Codex payload missing from {apk.name}")
            require(data.read("assets/codex-build").decode() == entry["buildId"] and
                    data.read("assets/codex-version").decode() == entry["version"], "Codex payload identity mismatch")


def prepare(gh, out, plugin="codex"):
    android = plugin == "android"
    release, _, previous = current(gh, out, ANDROID_PACKAGE if android else PACKAGE)
    build_file = plugin + "-build.json"
    staging = os.environ.get("STAGING_TAG", "")
    if staging:
        match = re.fullmatch(r"incoming-" + plugin + r"-([1-9][0-9]{0,9})", staging)
        require(match, "Invalid staging tag")
        draft = next((r for r in gh.releases() if r["tag_name"] == staging and r["draft"]), None)
        require(draft, "Staging draft missing")
        metadata = asset(draft, build_file)
        require(metadata and 0 < metadata["size"] <= 262144, "Missing staged metadata")
        gh.download(metadata, out / build_file)
        info = read_json(out / build_file)
        entry = android_entry(info) if android else codex_entry(info)
        require(str(entry["versionCode"]) == match[1], "Staging tag/versionCode mismatch")
        for artifact in entry["artifacts"]:
            item = asset(draft, artifact["file"])
            require(matches(item, artifact["sha256"], artifact["size"]), "Missing or invalid staged APK")
            gh.download(item, out / artifact["file"])
        return {"publish": True, "build": False}

    if android:
        source_commit = Path("SOURCE_COMMIT").read_text().strip()
        icon = Path("icons/Android.png")
        if (previous and not release["draft"] and previous["sourceCommit"] == source_commit and
                previous == android_entry(previous) and matches(asset(release, icon.name), digest(icon), icon.stat().st_size)
                and all(matches(asset(release, a["file"]), a["sha256"], a["size"]) for a in previous["artifacts"])):
            return {"publish": False, "build": False}
        code = previous["versionCode"] + 1 if previous else 1
        return {"publish": True, "build": True, "code": code, "version": f"1.0.{code - 1}", "sourceCommit": source_commit}

    subprocess.run([sys.executable, SCRIPT, "resolve", "--output", out / "lock.json"], check=True)
    lock = read_json(out / "lock.json")
    if previous:
        require(version_components(lock["version"]) >= version_components(previous["version"]), "Upstream downgrade")
        if (not release["draft"] and previous["buildId"] == lock["buildId"] and previous == codex_entry(previous)
                and matches(asset(release, ICON.name), digest(ICON), ICON.stat().st_size)
                and all(matches(asset(release, a["file"]), a["sha256"], a["size"]) for a in previous["artifacts"])):
            return {"publish": False, "build": False}
    return {"publish": True, "build": True, "code": next_code(lock["version"], previous)}


def publish(gh, out, plugin="codex"):
    info = read_json(out / (plugin + "-build.json"))
    entry = android_entry(info) if plugin == "android" else codex_entry(info)
    icon = Path("icons") / entry["icon"]
    verify(entry, out)
    release, store, previous = current(gh, out, entry["package"])
    if previous:
        require(version_components(entry["version"]) >= version_components(previous["version"]), "Version downgrade")
        require(entry["versionCode"] > previous["versionCode"] or
                (entry["versionCode"] == previous["versionCode"] and entry["artifacts"] == previous["artifacts"]),
                "A changed APK requires a newer versionCode")
    store["plugins"] = [other for other in store["plugins"] if other["package"] != entry["package"]] + [entry]
    write_json(out / MANIFEST, validate_store(store))
    if not release:
        release = gh.create(TAG, "Pocket Studio Plugin Store", "Open **Plugin store** in Pocket Studio to install plugins.")
    rid = release["id"]
    for a in entry["artifacts"]:
        replace_asset(gh, rid, out / a["file"], f"{entry['name']} {entry['version']} ({entry['versionCode']}) · {a['abi']}")
    replace_asset(gh, rid, icon, entry["name"])
    replace_asset(gh, rid, out / MANIFEST, "Pocket Studio plugin catalog")
    gh.api(f"releases/{rid}", method="PATCH", body={"draft": False, "make_latest": "true"})
    names = (MANIFEST, icon.name, *(a["file"] for a in entry["artifacts"]))
    for item in gh.api(f"releases/{rid}")["assets"]:
        if item["name"].startswith(("previous-", "stage-")) and item["name"].endswith(names):
            gh.remove(item)


def main():
    command, = sys.argv[1:]
    plugin = os.environ.get("PLUGIN", "codex")
    require(plugin in ("android", "codex"), "Unknown plugin")
    out = Path(os.environ["RUNNER_TEMP"]) / "plugin-release"
    out.mkdir(exist_ok=True)
    if command == "plan":
        plan = prepare(GitHub(), out, plugin)
        write_json(out / "plan.json", plan)
        with open(os.environ["GITHUB_OUTPUT"], "a") as outputs:
            for key in ("build", "publish"):
                print(f"{key}={str(plan[key]).lower()}", file=outputs)
    elif command == "build":
        plan = read_json(out / "plan.json")
        if plugin == "android":
            build_android(out, plan)
            return
        subprocess.run([sys.executable, SCRIPT, "build", "--lock", out / "lock.json", "--out", out,
                        "--version-code", str(plan["code"])], check=True)
    elif command == "publish":
        publish(GitHub(), out, plugin)
    else:
        raise SystemExit("usage: release.py plan|build|publish")


if __name__ == "__main__":
    main()

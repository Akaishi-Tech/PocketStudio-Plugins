"""Verify and publish first-party Pocket Studio plugins to the dedicated store release."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
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
PLUGINS = ("codex", "android", "avf", "proot")
TITLES = {"codex": "Codex", "android": "Android", "avf": "AVF", "proot": "PRoot"}
SOURCE_REPO = "Akaishi-Tech/PocketStudio"
CATALOG_POINTER = re.compile(r"^PocketStudio-Catalog: (plugin-store-v[1-9][0-9]*\.json) sha256=([a-f0-9]{64})$", re.M)
CATALOG_FILENAME = re.compile(r"plugin-store-v([1-9][0-9]*)\.json")
ICON = Path("plugins/codex/Codex.png")
SOURCE = Path("source")
SCRIPT = SOURCE / "scripts/build-codex-plugin.py"
HEX = r"[0-9a-f]{64}"
VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(0|[1-9][0-9]*))?"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest_stream(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


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

    def assets(self, release):
        release["assets"] = self.api(f"releases/{release['id']}/assets?per_page=100", all_pages=True)
        return release

    def download(self, item, path):
        with path.open("wb") as out:
            subprocess.run(["gh", "api", f"{self.root}/releases/assets/{item['id']}",
                            "-H", "Accept: application/octet-stream"], stdout=out, check=True)
        require(matches(item, digest(path), path.stat().st_size), f"GitHub asset digest mismatch: {path.name}")

    def create(self, tag, body):
        return self.api("releases", method="POST", body={"tag_name": tag, "body": body,
                        "draft": True, "target_commitish": os.environ["GITHUB_SHA"], "make_latest": "false"})

    def upload(self, rid, path, name, label):
        query = urlencode({"name": name, "label": label})
        url = f"https://uploads.github.com/repos/{self.repo}/releases/{rid}/assets?{query}"
        mime = {".apk": "application/vnd.android.package-archive", ".json": "application/json", ".png": "image/png"}[path.suffix]
        return json.loads(subprocess.check_output(["gh", "api", url, "--method", "POST",
                          "--header", "Content-Type: " + mime, "--input", str(path)]))

    def remove(self, item):
        self.api(f"releases/assets/{item['id']}", method="DELETE")


def immutable_asset(gh, release, path, label):
    previous = asset(release, path.name)
    sha, size = digest(path), path.stat().st_size
    if previous and previous["state"] == "uploaded":
        require(matches(previous, sha, size), f"Immutable release filename already exists: {path.name}")
        return
    if previous:
        gh.remove(previous)  # A failed GitHub upload can leave an empty starter asset.
    item = gh.upload(release["id"], path, path.name, label)
    require(matches(item, sha, size), f"GitHub did not confirm the upload of {path.name}")
    release["assets"].append(item)


def source_commit():
    value = Path("SOURCE_COMMIT").read_text().strip()
    require(re.fullmatch(r"[a-f0-9]{40}", value), "Invalid source pin")
    return value


def listing(plugin):
    return read_json(Path("plugins") / plugin / "listing.json")


def icon_path(plugin):
    return Path("plugins") / plugin / listing(plugin)["icon"]


def abi_files(plugin, info=None):
    flavors = ANDROID_FLAVORS if plugin == "android" else ABIS
    version = f"-{info['version']}-{info['versionCode']}" if info else ""
    return {abi: f"PocketStudio-{TITLES[plugin]}-Plugin{version}-{abi}.apk" for abi in flavors}


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
    body = release.get("body") or ""
    pointers = CATALOG_POINTER.findall(body)
    if "PocketStudio-Catalog:" in body:
        require(len(pointers) == 1 and body.count("PocketStudio-Catalog:") == 1, "Invalid or duplicate catalog pointer")
        name, sha = pointers[0]
        item = asset(release, name)
        require(item and item.get("digest") == "sha256:" + sha and item["state"] == "uploaded",
                "Committed catalog is missing or damaged")
    else:
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
    if release:
        gh.assets(release)
    store = previous_store(gh, release, out)
    previous = next((entry for entry in store["plugins"] if entry["package"] == package), None)
    return release, store, previous


def plugin_entry(plugin, info):
    descriptor = listing(plugin)
    require(info["package"] == descriptor["package"] == f"{PREFIX}{plugin}plugin", "Incorrect plugin package")
    entry = {key: info[key] for key in ("version", "versionCode", "signerSha256", "artifacts")}
    entry.update(descriptor)
    if "sourceCommit" in info or plugin != "codex":
        require(re.fullmatch(r"[0-9a-f]{40}", info["sourceCommit"]), "Invalid source revision")
        entry["sourceCommit"] = info["sourceCommit"]
    if plugin == "codex":
        require(re.fullmatch(HEX, info["buildId"]), "Invalid Codex build ID")
        entry["buildId"] = info["buildId"]
    validate_store({"schemaVersion": 2, "plugins": [entry]})
    version_components(entry["version"])
    files = {a["abi"]: a["file"] for a in entry["artifacts"]}
    require(files in (abi_files(plugin), abi_files(plugin, entry)), f"Missing or unexpected {plugin} ABI")
    return entry


def public_entry(plugin, info):
    entry = plugin_entry(plugin, info)
    names = abi_files(plugin, entry)
    entry["icon"] = f"{TITLES[plugin]}-{entry['version']}-{entry['versionCode']}.png"
    entry["artifacts"] = [dict(a, file=names[a["abi"]]) for a in entry["artifacts"]]
    return entry


def build_android(out, plan):
    env = {**os.environ, "POCKETSTUDIO_VERSION_NAME": plan["version"], "POCKETSTUDIO_VERSION_CODE": str(plan["code"])}
    subprocess.run(["./gradlew", "--no-daemon", ":android-plugin:assembleRelease"], cwd=SOURCE, env=env, check=True)
    artifacts = []
    for abi, flavor in ANDROID_FLAVORS.items():
        variant = SOURCE / "plugins/android/build/outputs/apk" / flavor / "release"
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
        # For apps with minSdk >= 28, apksigner skips v2 if a newer block is verified.
        modern_scheme = re.search(r"^Verified using v(?:2|3(?:\.1)?) scheme [^\n]+: true$", signing, re.M)
        require("Number of signers: 1" in signing.splitlines() and set(signers) == {digest(PIN)} and modern_scheme,
                f"APK signer or modern signature mismatch: {apk.name}")
        pkg = subprocess.check_output([tools / "aapt", "dump", "badging", apk], text=True)
        fields = re.search(r"^package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']*)'", pkg, re.M)
        require(fields and fields.groups() == (entry["package"], str(entry["versionCode"]), entry["version"]),
                f"Package or version mismatch: {apk.name}")
        with zipfile.ZipFile(apk) as data:
            native = {name.split("/")[1] for name in data.namelist() if name.startswith("lib/")}
            if entry["package"] == PREFIX + "avfplugin":
                require(not native, f"AVF plugin unexpectedly includes Android native libraries: {apk.name}")
                bundle = {"assets/debian.img.gz", "assets/debian.img.sha256", "assets/replace.sh",
                          "assets/linux-languages.json", "assets/linux-image-abi"}
                require(bundle.issubset(data.namelist()) and data.read("assets/linux-image-abi").decode().strip() == abi,
                        f"Missing or wrong AVF disk payload for {abi}")
                sha = data.read("assets/debian.img.sha256").decode().split()[0]
                with data.open("assets/debian.img.gz") as image:
                    require(digest_stream(image) == sha, "AVF disk checksum mismatch")
                continue
            require(native == {abi}, f"Incorrect native ABI in {apk.name}")
            if entry["package"] == PREFIX + "prootplugin":
                required = {"assets/linux-languages.json", "assets/debian-rootfs.tar.gz", "assets/debian-rootfs.sha256",
                            f"lib/{abi}/libproot_runtime.so", f"lib/{abi}/libproot_loader.so"}
                require(required.issubset(data.namelist()), f"PRoot environment missing from {apk.name}")
                sha = data.read("assets/debian-rootfs.sha256").decode().split()[0]
                with data.open("assets/debian-rootfs.tar.gz") as image:
                    require(digest_stream(image) == sha, "PRoot image checksum mismatch")
                continue
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
    release, _, previous = current(gh, out, listing(plugin)["package"])
    build_file = plugin + "-build.json"
    staging = os.environ.get("STAGING_TAG", "")
    if staging:
        match = re.fullmatch(r"incoming-" + plugin + r"-([1-9][0-9]{0,9})", staging)
        require(match, "Invalid staging tag")
        draft = next((r for r in gh.releases() if r["tag_name"] == staging and r["draft"]), None)
        require(draft, "Staging draft missing")
        gh.assets(draft)
        metadata = asset(draft, build_file)  # Older staging drafts used an unversioned metadata name.
        if not metadata:
            pattern = re.compile(re.escape(plugin) + r"-build-(?:" + VERSION + r")-" + match[1] + r"\.json")
            candidates = [item for item in draft["assets"] if pattern.fullmatch(item["name"])]
            require(len(candidates) == 1, "Expected one versioned staging metadata asset")
            metadata, = candidates
        require(metadata and 0 < metadata["size"] <= 262144, "Missing staged metadata")
        gh.download(metadata, out / build_file)
        info = read_json(out / build_file)
        entry = plugin_entry(plugin, info)
        require(str(entry["versionCode"]) == match[1], "Staging tag/versionCode mismatch")
        for artifact in entry["artifacts"]:
            item = asset(draft, artifact["file"])
            require(matches(item, artifact["sha256"], artifact["size"]), "Missing or invalid staged APK")
            gh.download(item, out / artifact["file"])
        return {"publish": True, "build": False}

    require(plugin in ("codex", "android"), "AVF and PRoot use signed artifacts from the source build workflow")
    if android:
        source_commit = Path("SOURCE_COMMIT").read_text().strip()
        icon = icon_path(plugin)
        if (previous and not release["draft"] and previous["sourceCommit"] == source_commit and
                previous == public_entry(plugin, previous) and matches(asset(release, previous["icon"]), digest(icon), icon.stat().st_size)
                and all(matches(asset(release, a["file"]), a["sha256"], a["size"]) for a in previous["artifacts"])):
            return {"publish": False, "build": False}
        code = previous["versionCode"] + 1 if previous else 1
        return {"publish": True, "build": True, "code": code, "version": f"1.0.{code - 1}", "sourceCommit": source_commit}

    subprocess.run([sys.executable, SCRIPT, "resolve", "--output", out / "lock.json"], check=True)
    lock = read_json(out / "lock.json")
    if previous:
        require(version_components(lock["version"]) >= version_components(previous["version"]), "Upstream downgrade")
        if (not release["draft"] and previous["buildId"] == lock["buildId"] and previous == public_entry(plugin, previous)
                and matches(asset(release, previous["icon"]), digest(ICON), ICON.stat().st_size)
                and all(matches(asset(release, a["file"]), a["sha256"], a["size"]) for a in previous["artifacts"])):
            return {"publish": False, "build": False}
    return {"publish": True, "build": True, "code": next_code(lock["version"], previous)}


def checked_inputs(out, plugins, require_pin):
    selected = []
    for plugin in plugins:
        info = read_json(out / f"{plugin}-build.json")
        entry = plugin_entry(plugin, info)
        if require_pin:
            require(entry.get("sourceCommit") == source_commit(), f"Wrong source commit in {plugin} build")
        verify(entry, out)
        selected.append((plugin, info, public_entry(plugin, info)))
    return selected


def payloads(entry):
    return {a["abi"]: (a["sha256"], a["size"]) for a in entry["artifacts"]}


def publish(gh, out, plugins=("codex",), require_pin=False):
    """Stage every immutable asset, then switch the single catalog pointer after all plugins verify."""
    selected = checked_inputs(out, plugins, require_pin)
    release, store, _ = current(gh, out)
    previous = {entry["package"]: entry for entry in store["plugins"]}
    with TemporaryDirectory(dir=out) as directory:
        work = Path(directory)
        for plugin, info, entry in selected:
            old = previous.get(entry["package"])
            if old:
                require(version_components(entry["version"]) >= version_components(old["version"]), "Version downgrade")
                require(entry["versionCode"] > old["versionCode"] or
                        (entry["versionCode"] == old["versionCode"] and
                         payloads(entry) == payloads(old)),
                        "A changed APK requires a newer versionCode")
            src_files = {a["abi"]: a["file"] for a in info["artifacts"]}
            for artifact in entry["artifacts"]:
                os.link(out / src_files[artifact["abi"]], work / artifact["file"])
            shutil.copyfile(icon_path(plugin), work / entry["icon"])
            write_json(work / f"{plugin}-build-{entry['version']}-{entry['versionCode']}.json",
                       {**info, "artifacts": entry["artifacts"]})
            previous[entry["package"]] = entry
        updated = validate_store({"schemaVersion": 2, "plugins": list(previous.values())})
        if not release:
            release = gh.create(TAG, "Open the plugin store in Pocket Studio to install these packages.")
        for path in sorted(work.iterdir()):
            immutable_asset(gh, release, path, path.name)
        if store == updated and CATALOG_POINTER.search(release.get("body") or "") and not release["draft"] and not release.get("name"):
            return
        generations = [int(match[1]) for row in release["assets"] if (match := CATALOG_FILENAME.fullmatch(row["name"]))]
        catalog = work / f"plugin-store-v{max(generations, default=0) + 1}.json"
        write_json(catalog, updated)
        immutable_asset(gh, release, catalog, "Pocket Studio plugin catalog")
        pointer = f"PocketStudio-Catalog: {catalog.name} sha256={digest(catalog)}"
        body = CATALOG_POINTER.sub("", release.get("body") or "").rstrip()
        changes = {"body": (body + "\n\n" + pointer).strip(), "draft": False, "make_latest": "true"}
        if release.get("name"):
            changes["name"] = ""
        gh.api(f"releases/{release['id']}", method="PATCH", body=changes)


def fetch_source_run(out, plugins):
    run_id = os.environ["SOURCE_RUN_ID"]
    require(re.fullmatch(r"[1-9][0-9]{0,19}", run_id), "Invalid source workflow run ID")
    root = f"repos/{SOURCE_REPO}"
    def read(path):
        return json.loads(subprocess.check_output(["gh", "api", path]))
    run = read(f"{root}/actions/runs/{run_id}")
    branch = read(root)["default_branch"]
    require(run["repository"]["full_name"] == run["head_repository"]["full_name"] == SOURCE_REPO
            and run["head_branch"] == branch and run["head_sha"] == source_commit()
            and run["path"] == ".github/workflows/build-all.yml" and run["event"] in ("workflow_dispatch", "push")
            and run["status"] == "completed" and run["conclusion"] == "success",
            "Choose a successful build-all run of the pinned source on the default branch")
    for plugin in plugins:
        with TemporaryDirectory(dir=out) as directory:
            download = Path(directory)
            subprocess.run(["gh", "run", "download", run_id, "--repo", SOURCE_REPO, "--name",
                            "plugin-" + plugin, "--dir", str(download)], check=True)
            metadata = download / (plugin + "-build.json")
            require(metadata.is_file() and not metadata.is_symlink() and metadata.stat().st_size <= 262144,
                    f"Missing or invalid source metadata for {plugin}")
            entry = plugin_entry(plugin, read_json(metadata))
            require(entry.get("sourceCommit") == source_commit(), "Source artifact metadata does not match pin")
            files = {metadata.name, *(a["file"] for a in entry["artifacts"])}
            require({file.name for file in download.iterdir()} == files, f"Unexpected files in plugin-{plugin}")
            for name in files:
                path = download / name
                require(path.is_file() and not path.is_symlink(), f"Unexpected file type in plugin-{plugin}")
                path.replace(out / name)


def main():
    command, = sys.argv[1:]
    plugin = os.environ.get("PLUGIN", "codex")
    require(plugin in (*PLUGINS, "all"), "Unknown plugin")
    selected = PLUGINS if plugin == "all" else (plugin,)
    out = Path(os.environ["RUNNER_TEMP"]) / "plugin-release"
    out.mkdir(exist_ok=True)
    if command == "fetch":
        fetch_source_run(out, selected)
    elif command == "verify":
        checked_inputs(out, selected, bool(os.environ.get("SOURCE_RUN_ID")))
    elif command == "publish":
        publish(GitHub(), out, selected, bool(os.environ.get("SOURCE_RUN_ID")))
    elif command == "plan":
        require(plugin != "all", "Building all plugins requires a signed source workflow run")
        plan = prepare(GitHub(), out, plugin)
        write_json(out / "plan.json", plan)
        with open(os.environ["GITHUB_OUTPUT"], "a") as outputs:
            for key in ("build", "publish"):
                print(f"{key}={str(plan[key]).lower()}", file=outputs)
    elif command == "build":
        plan = read_json(out / "plan.json")
        if plugin == "android":
            build_android(out, plan)
        else:
            require(plugin == "codex", "Request a build-all run in the source repository for this plugin")
            subprocess.run([sys.executable, SCRIPT, "build", "--lock", out / "lock.json", "--out", out,
                            "--version-code", str(plan["code"])], check=True)
    else:
        raise SystemExit("usage: release.py plan|build|fetch|verify|publish")


if __name__ == "__main__":
    main()

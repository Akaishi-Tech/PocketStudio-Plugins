# Pocket Studio Plugins

The plugin store in Pocket Studio reads the catalog from the `plugin-store` GitHub release. The four listings in this repository describe the Android environment, the Codex agent, and two alternative Debian environments. [Plugin store](https://github.com/Akaishi-Tech/PocketStudio-Plugins/releases/tag/plugin-store) · [Pocket Studio](https://github.com/Akaishi-Tech/PocketStudio)

| Plugin | Android package | Architectures |
| --- | --- | --- |
| [Android](plugins/android/listing.json) | `com.hcgstudio.pocketstudio.androidplugin` | arm64-v8a, armeabi-v7a, x86_64, x86 |
| [Codex](plugins/codex/listing.json) | `com.hcgstudio.pocketstudio.codexplugin` | arm64-v8a, x86_64 |
| [AVF](plugins/avf/listing.json) | `com.hcgstudio.pocketstudio.avfplugin` | arm64-v8a, x86_64 |
| [PRoot](plugins/proot/listing.json) | `com.hcgstudio.pocketstudio.prootplugin` | arm64-v8a, x86_64 |

## Verify a signed workflow build

The main repository builds and signs the plugins in GitHub Actions. Update `SOURCE_COMMIT` in this repository to the commit built by a successful `build-all.yml` run on the source repository’s default branch. In **Release Pocket Studio plugins**, select **all** or an individual plugin and enter the source run ID. Leave **Publish verified artifacts to the plugin store** unchecked to run verification. The workflow downloads each `plugin-<id>` artifact and checks the source revision, package, ABI, signing certificate, payload identity, file size and SHA-256. If the repository token cannot read source artifacts, configure `SOURCE_ACTIONS_READ_TOKEN` with Actions read access to `Akaishi-Tech/PocketStudio`. [Store workflow](.github/workflows/release-plugins.yml) · [Source builds](https://github.com/Akaishi-Tech/PocketStudio/actions/workflows/build-all.yml)

Each source artifact contains `<id>-build.json` and one signed APK for each supported ABI. Its JSON records `package`, `version`, `versionCode`, `sourceCommit`, `signerSha256` and `artifacts`. Each APK record contains `abi`, `file`, `size` and `sha256`. Codex also records the packager’s `buildId`. The package version code must increase when the APK contents change.

## Publish

To publish an approved run, select the same source run and enable **Publish verified artifacts to the plugin store**. The workflow uploads immutable versioned APKs, plugin icons and build records, then switches the catalog pointer in a single release update. A release asset can be downloaded by name directly from the selected catalog. The workflow preserves previous catalog generations and the files they reference.

Each catalog has a name such as `plugin-store-v3.json`. The release body identifies the committed generation on a line with this format.

```text
PocketStudio-Catalog: plugin-store-v3.json sha256=<64 lowercase hex digits>
```

A legacy release can still supply `plugin-store.json` until a versioned catalog is committed. When publishing from such a release, unrelated plugin listings retain their existing filenames and digests. Catalog entries use `schemaVersion` 2.

The existing draft-release input accepts an `incoming-<id>-<versionCode>` staging tag for one plugin. The draft contains its signed APKs and matching build metadata, either with versioned public filenames or in the earlier staging format. The scheduled Codex and Android checks use the pinned source recipe if signing secrets are configured in this repository. [Verification and publication](scripts/release.py)

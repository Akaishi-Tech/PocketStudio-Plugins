# Pocket Studio Plugins

The plugin store in Pocket Studio reads the catalog from the `plugin-store` GitHub release. The four listings in this repository describe the Android environment, the Codex agent, and two alternative Debian environments. [Plugin store](https://github.com/Akaishi-Tech/PocketStudio-Plugins/releases/tag/plugin-store) · [Pocket Studio](https://github.com/Akaishi-Tech/PocketStudio)

| Plugin | Android package | Architectures |
| --- | --- | --- |
| [Android](plugins/android/listing.json) | `com.hcgstudio.pocketstudio.androidplugin` | arm64-v8a, armeabi-v7a, x86_64, x86 |
| [Codex](plugins/codex/listing.json) | `com.hcgstudio.pocketstudio.codexplugin` | arm64-v8a, x86_64 |
| [AVF](plugins/avf/listing.json) | `com.hcgstudio.pocketstudio.avfplugin` | arm64-v8a, x86_64 |
| [PRoot](plugins/proot/listing.json) | `com.hcgstudio.pocketstudio.prootplugin` | arm64-v8a, x86_64 |

## Automatic publishing

Changes to plugin code, shared APIs, image recipes or packaging on PocketStudio’s main branch start the plugin build workflow. The source repository also checks for changes to the upstream Codex package daily. This store checks the source workflow every ten minutes and selects its latest successful push or manual build on the default branch. GitHub may delay scheduled checks. New artifacts are downloaded and published automatically. The store records the run number, attempt, ID and source revision from GitHub. Checks with no updates skip the artifact downloads. [Plugin builds](https://github.com/Akaishi-Tech/PocketStudio/actions/workflows/build-all.yml) · [Store synchronization](https://github.com/Akaishi-Tech/PocketStudio-Plugins/actions/workflows/release-plugins.yml)

Pushing changes under `plugins/` runs store synchronization immediately. Descriptions, translations and icons can change independently of the APKs. A changed icon gets a versioned filename containing its content hash.

The publisher checks the build provenance, package name, version, ABI, payload, size and SHA-256. The catalog carries the certificate fingerprint supplied by the build for the Android client's installation checks. Verified versioned assets are uploaded before the fixed `plugin-store.json` is replaced. Retrying an interrupted update restores or promotes the catalog. [Live catalog](https://github.com/Akaishi-Tech/PocketStudio-Plugins/releases/download/plugin-store/plugin-store.json)

## Manual runs

Select **Release Pocket Studio plugins** to check again immediately. **All** selects the latest successful source build automatically. An explicit run ID can select a particular build for inspection. Clear **Publish the verified packages to the store** for a verification-only run. Previously published newer runs take precedence.

For an individual plugin, the workflow also accepts an `incoming-<id>-<versionCode>` staging draft. With signing secrets configured here, selecting Android or Codex with both input IDs empty builds from the current source default branch. [Publisher](scripts/release.py)

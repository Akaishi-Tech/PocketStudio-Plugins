# Pocket Studio Plugins

Pocket Studio reads the versioned plugin catalog from the `plugin-store` release. Open **Settings → Plugin store** in the app to browse listings and install the APK for your device. The Codex plugin retains the Android package name `com.hcgstudio.pocketstudio.codexplugin` and ships separate ARM64 and x86_64 APKs. [Plugin store release](https://github.com/Akaishi-Tech/PocketStudio-Plugins/releases/tag/plugin-store) · [Pocket Studio app](https://github.com/Akaishi-Tech/PocketStudio/releases)

## Publish Codex

`SOURCE_COMMIT` pins the Pocket Studio source revision containing the plugin and packaging recipe. `codex.json` and `icons/Codex.png` supply the store listing. The publisher verifies the package, Android version, signing certificate, build ID, native ABI, size and SHA-256 digest of each APK. It uploads the two APKs and the icon before publishing the v2 catalog and preserves other plugins’ listings. [Publishing workflow](.github/workflows/release-plugins.yml)

To release locally signed packages, run the packager for the pinned source and assign a version code greater than the published Codex build. It produces `codex-build.json` and the two APKs. Upload those three files to a draft release, then select that staging tag in **Release Pocket Studio plugins**. The staging workflow uses the public signing certificate checked into this repository. [Build instructions](https://github.com/Akaishi-Tech/PocketStudio/tree/main/build-support/codex-plugin)

```sh
tag="incoming-codex-$PLUGIN_VERSION_CODE"
gh release create "$tag" --draft --title "Codex staging $PLUGIN_VERSION_CODE" --notes "Signed ABI artifacts" \
  "$OUT/codex-build.json" "$OUT/PocketStudio-Codex-Plugin-arm64-v8a.apk" "$OUT/PocketStudio-Codex-Plugin-x86_64.apk"
gh workflow run release-plugins.yml -f staging_tag="$tag"
```

With the Android signing secrets configured, a manual dispatch with an empty staging tag builds from `SOURCE_COMMIT`. The daily check at 03:23 UTC uses that same mode. It rebuilds when the upstream Codex package, packaging inputs or listing change. The signing job reads `ANDROID_KEYSTORE_BASE64`, `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS` and `ANDROID_KEY_PASSWORD` as Actions secrets.

## Catalog format

`plugin-store.json` uses `schemaVersion: 2` and a `plugins` array. First-party entries use the `com.hcgstudio.pocketstudio.` package prefix. Each entry declares a category, publisher, description, display name, icon asset, Android version, version code and signer fingerprint. Optional `names` and `descriptions` maps provide localized text. Each item in `artifacts` provides the ABI, release filename, byte size and SHA-256. [Codex listing](codex.json)

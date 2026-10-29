# Building BCD on a Mac and installing it on a device

`tools/local_ci.py` runs the build steps of `.github/workflows/build-ipa.yml` on this Mac, in order. It skips the host
checks and the steps that only make sense on GitHub. Xcode then builds a signed app for the device. Tried on an
M5 Pro with Xcode 27.1, for an iPhone on iOS 26.6.2 (2026-10-29).

## Before you start

- Xcode, with the iOS platform installed.
- The native Homebrew at `/opt/homebrew`. The workflow installs what it needs there itself (bison, llvm, lld, ninja,
  meson, ...). An Intel Homebrew at `/usr/local` gets no prebuilt bottles any more and fails building them.
- About 40 GB free on the disk.
- The submodules checked out at their pinned commits:
  ```bash
  git submodule update --init --recursive
  ```
- `app/Madeira/d3d12/libmetalirconverter.dylib`, Apple's Metal Shader Converter for iOS. It is not in this public
  repository. Copy it from an official BCD IPA: unzip the IPA and take `Payload/Madeira.app/d3d12/libmetalirconverter.dylib`.
  `build/madeira-d3d12/deps.sh` accepts the pinned original or the copy that BCD 0.1.622's IPA ships.

## Build

```bash
python3 tools/local_ci.py . --to 411
```

Every step's log is in `toolchains/local-ci/logs/`. After fixing a failed step, resume from it:

```bash
python3 tools/local_ci.py . --from N --to 411
```

`--list` shows the steps and which ones are skipped.

## Sign, install, run

Set your team and bundle identifiers in `app/Madeira.xcodeproj`, then:

```bash
xcodebuild -project app/Madeira.xcodeproj -scheme Madeira -sdk iphoneos -configuration Debug -destination 'generic/platform=iOS' -derivedDataPath build-out/dd-signed -allowProvisioningUpdates ENABLE_DEBUG_DYLIB=NO ENABLE_PREVIEWS=NO build
```

Leave `ENABLE_DEBUG_DYLIB=NO` in. Without it, Xcode 27 turns the app's executable into a stub that aborts at launch
(`abort_could_not_find_entry_point___debug_dylib`).

```bash
xcrun devicectl device install app --device <UDID> build-out/dd-signed/Build/Products/Debug-iphoneos/Madeira.app
```

```bash
xcrun devicectl device process launch --device <UDID> <bundle id>
```

## What Xcode 27 needed

These are in the repository and change nothing under Xcode 26:

| Problem | Fix |
|---|---|
| The iOS 27 SDK declares `pipe2`. Wine's configure takes it, iOS 26 has none, and Wine crashes connecting to the wineserver. | Step "Generate cross-tree headers and patch config.h" adds `#undef HAVE_PIPE2`. |
| Xcode 27's Metal compiler wants a fifth argument for an atomic builtin in DXMT's tessellation shader. | `tools/patch-dxmt-metal27-atomic.py`. |
| FEX's Windows builds found Homebrew's `fmt` for macOS: `IMPORTED_IMPLIB not set`. | `tools/build-xtajit64.sh` and `tools/build-xtajit-wow64.sh` pass `CMAKE_IGNORE_PREFIX_PATH`. |

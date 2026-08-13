# Bundled CodeGraph runtime

The framework vendors unmodified, pinned CodeGraph 1.5.0 release archives for
`darwin-arm64` and `linux-x64`. At first use it verifies the upstream SHA-256
checksum and extracts only the current platform into the ignored `runtime/`
directory. Repair runs never download, install, upgrade, or globally configure
CodeGraph.

`manifest.json` is the authoritative version/platform inventory. The runtime
is optional when `DEBUGGING_CONTEXT_MODE=auto`, disabled with `off`, and a
preflight requirement with `required`.

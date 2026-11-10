`esper-startup-hook.base64` is an exact copy of `RESIDENT_HOOK_BASE64` already
embedded in this repository's `deploy/esper-utau/termux/resampler.py`. Its source
is `deploy/esper-utau/termux/resident-hook/StartupHook.cs`. No upstream engine
binary is embedded or reimplemented. The Rust installer verifies SHA-256
`9a7a67253fafdd9e2a25aa265f40ab1a8789cfb7825bfa03b0abaa49ee3d9da4`
before writing or loading the hook. The pinned official ESPER-Utau and
`esper-config.ini` remain separate, verified installation artifacts.

WORLDLINE's public API layout follows OpenUtau 0.1.565, commit
`a60ca5830b9064556157245d4bf8f5920d93e5f8`, and this repository's original
`teto_renderer/worldline_native.py`. The adapter retains the upstream API's
MIT attribution in `../LICENSE-OpenUtau.txt`. The library itself remains an
external artifact and must match its architecture-specific pinned SHA-256.

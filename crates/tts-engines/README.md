# Native TTS engine boundaries

`tts-engines` runs WORLDLINE and ESPER without a Python renderer or bridge.
The crate contains no replacement voice model and never substitutes another
engine when the requested motor is unavailable.

## WORLDLINE

Build `tts-worldline-native` for **Linux/glibc ARM64** to run inside the existing
Ubuntu guest, separately from the Android/Bionic Termux daemon. Run:

```sh
tts-worldline-native --library /absolute/libworldline.so --probe
tts-worldline-native --library /absolute/libworldline.so --job /job/job.json --output /job/out.wav
```

The helper accepts the existing schema 1 phrase job: 44,100 Hz mono sources,
`requests`, all five 10 ms control curves, optional duration and silence
intervals. Job and source limits, OTO trimming, frame rounding and the 25,280
aggregate model budget match the existing adapter. Input WAVs must be flat
regular files in a dedicated job directory. The pinned library's ELF machine,
SHA-256, symbols and 144-byte `SynthRequest` ABI are checked before rendering.
The C++ library's `PhraseSynth` API is used directly; its output is freed with
C++ `operator delete[]`, and the phrase is destroyed on success and errors.

`render_worldline_pcm` is for a process already isolated in the guest;
`run_worldline_isolated` owns a helper command, deadline, cancellation and
physical process-group cleanup. The parent constructs the `proot-distro`
command; this crate does not relocate engines or alter network ownership.
`render_worldline` atomically promotes a completed WAV and reports measured
levels without claiming pronunciation or listening-test approval.

## ESPER

`EsperResident::spawn` takes a command launching the verified .NET resident host
or startup hook directly, optionally through proot. It checks protocol 1,
engine, readiness and pinned source commit. JSON lines and base64 TSV are
negotiated from the existing handshake; each reply must confirm its request ID.
The existing .NET runtime and startup-hook artifact remain required; installation
and artifact SHA verification belong to the release manager.

`render` sends native UTAU arguments. `render_utau` additionally preserves the
existing wrapper's volume headroom (`min(requested * 0.5, 50)`). Both validate
all 13 arguments, integer LENGTH, the 20-second ESPER output bound and bounded
pitchbend expansion. Callers must copy voicebank WAVs into a private analysis
cache and hold the source lease through the call: ESPER writes `.esp` and
`_wav.frq` beside its source. Interrupted analyses must be invalidated before
releasing that lease. No engine fallback is attempted after an error.

Pipe operations have 64 KiB limits, monotonic deadlines and cooperative
cancellation checked at most every 25 ms. Cancellation, invalid response,
timeout and Drop close stdin, kill the isolated process group and reap the
child. Native execution is serialized through `&mut EsperResident`; callers
must use a bounded blocking executor rather than block an async runtime.

`process::run_resampler` also runs a native one-shot 13-argument UTAU command,
with the same physical cancellation ownership. Output WAV verification and
source-cache leasing remain the render pipeline's responsibility.

## Verification

`cargo test -p tts-engines` verifies both resident transports, invalid handshakes,
argument limits, deadline/cancellation, native ABI and PhraseSynth input
lifetimes against a tiny compiled C++ contract motor. The C++ compiler is
required for this gate. The fake library is deliberately rejected by the
production pin check. Real pinned WORLDLINE, voicebanks, ESPER/.NET and Android
devices are required for the separate acoustic and deployment gates.

## Compatibility rollout

The existing Python adapters opt into native engine execution with
`TTS_ENGINES_RUST=1`. WORLDLINE launches the Rust helper in the Linux guest;
`PHONE_WORKER_WORLDLINE_RUST_HELPER` defaults to
`/usr/local/bin/tts-worldline-native`. ESPER delegates resident protocol and
process ownership to the prebuilt PyO3 extension, loaded from
`TTS_NATIVE_LIBRARY` or the installed `tts_fluxo_experimental` package. Missing
helpers, invalid native modules and resident failures fail explicitly while
this flag is on. The adapter never silently selects a Python engine.

These compatibility wrappers retain Python planning during rollout. The
separate native worker uses the Rust render pipeline and this engine crate
directly. PyO3 blocking calls release the GIL and must be driven via
`asyncio.to_thread` when called from asyncio. A cancellation token must be
signaled and the physical call awaited before dropping the source lease.

`cogs.tts.engines_rust` provides that async boundary and drains even repeated
coroutine cancellations. `esper::installed_resident_command` also prepares the
existing ESPER installation entirely in Rust: it verifies the official ARM64
engine and INI, installs the original embedded startup-hook bytes after SHA
verification, and constructs the guest command. Keep its sources and jobs
under the supplied private cache root; absolute paths are preserved by a
single cache bind. Native artifact verification is reused only while device,
inode, size, mtime and ctime remain unchanged.

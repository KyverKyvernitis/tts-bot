"""Discord AudioSource bridge: native replay and FFmpeg own the audio path."""
from __future__ import annotations

import os
import shlex
import discord


def enabled() -> bool:
    return (os.environ.get("TTS_BACKEND", "").strip().lower() == "rust"
            or os.environ.get("TTS_SOURCE_RUST", "0").strip() == "1")


def uses_native_source(handle) -> bool:
    if not enabled() or handle is None:
        return False
    buffer = getattr(getattr(handle, "shared_job", None), "buffer", None)
    if getattr(buffer, "backend", "") != "rust":
        raise RuntimeError("source Rust exige replay Rust para a mesma reprodução")
    return True


class NativePCMSource(getattr(discord, "AudioSource", object)):
    def __init__(self, buffer, *, before_options: str, options: str):
        from .replay_rust import native_module
        native = native_module()
        if getattr(native, "SOURCE_PROTOCOL_VERSION", None) != 1:
            raise RuntimeError("extensão Rust incompatível com protocolo source 1")
        self._handle = native.NativeSourceHandle(
            buffer._native, shlex.split(before_options), shlex.split(options))
        self._closed = False

    def read(self):
        return self._handle.read_frame()

    def is_opus(self):
        return False

    def cleanup(self):
        if not self._closed:
            self._closed = True
            self._handle.cancel()

    def snapshot(self):
        return self._handle.snapshot()

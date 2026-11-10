#!/usr/bin/env python3
"""Build native TTS artifacts on a development/CI host, never on the VPS.

The wheel is installed into this interpreter and its extension is also copied
atomically to the historical in-tree loader location. Cross builds are packaged
only and can never replace the running host extension.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, env: dict[str, str], cwd: Path = ROOT) -> str:
    print("+ " + " ".join(command), flush=True)
    result = subprocess.run(command, cwd=cwd, env=env, text=True, check=True,
                            stdout=subprocess.PIPE)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    return result.stdout


def environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = os.fspath(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    # Work chats may install Rust beside the checkout. Ordinary CI uses PATH.
    local_cargo = ROOT.parent / ".cargo"
    local_rustup = ROOT.parent / ".rustup"
    if not shutil.which("cargo") and (local_cargo / "bin/cargo").exists():
        env["PATH"] = os.fspath(local_cargo / "bin") + os.pathsep + env.get("PATH", "")
        env.setdefault("CARGO_HOME", os.fspath(local_cargo))
        if local_rustup.is_dir():
            env.setdefault("RUSTUP_HOME", os.fspath(local_rustup))
    env.setdefault("CARGO_INCREMENTAL", "0")
    env.setdefault("CARGO_BUILD_JOBS", "2")
    return env


def atomic_write(destination: Path, data: bytes, *, mode: int = 0o644) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent,
                                         prefix="." + destination.name + ".",
                                         delete=False) as output:
            staging = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        staging.chmod(mode)
        os.replace(staging, destination)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


def glibc_requirement(path: Path, maximum: str) -> str | None:
    """Reject artifacts requiring a newer libc than the documented VPS/guest."""
    readelf = shutil.which("readelf")
    if readelf is None:
        raise RuntimeError("readelf is required to verify GNU/Linux artifacts")
    output = subprocess.run([readelf, "--version-info", os.fspath(path)],
                            check=True, text=True, capture_output=True,
                            env=dict(os.environ, LC_ALL="C")).stdout
    # Rust std probes optional new libc functions through ELF weak references.
    # Their version-need entries are WEAK and do not impose a runtime minimum.
    # Count mandatory entries, including the same version on another library.
    versions = {tuple(int(part) for part in version.split("."))
                for version, flags in re.findall(
                    r"Name:\s+GLIBC_([0-9]+(?:\.[0-9]+)+)\s+Flags:\s+(\w+)", output)
                if flags.upper() != "WEAK"}
    if not versions:
        return None  # Fully static ELF, or no versioned libc references.
    newest = max(versions)
    allowed = tuple(int(part) for part in maximum.split("."))
    if newest > allowed:
        raise RuntimeError(f"{path.name} requires GLIBC {'.'.join(map(str, newest))}, maximum is {maximum}")
    return ".".join(map(str, newest))


def install_extension(wheel: Path, *, env: dict[str, str], install_wheel: bool) -> Path:
    with zipfile.ZipFile(wheel) as archive:
        candidates = [name for name in archive.namelist()
                      if Path(name).name == "_nucleo_fluxo_tts.abi3.so"]
        if len(candidates) != 1:
            raise RuntimeError(f"expected one extension in {wheel.name}: {candidates}")
        destination = ROOT / "vinculo_python/python/tts_fluxo_experimental/_nucleo_fluxo_tts.abi3.so"
        atomic_write(destination, archive.read(candidates[0]))
    if install_wheel:
        run([sys.executable, "-m", "pip", "install", "--no-deps", "--force-reinstall", os.fspath(wheel)], env=env)
    return destination


def wheel_compatibility_args(target: str, *, zig: bool, maximum_glibc: str) -> list[str]:
    if not target.endswith("linux-gnu"):
        return []
    if zig:
        return ["--zig", "--compatibility", "manylinux_" + maximum_glibc.replace(".", "_")]
    # Auditwheel includes optional Rust WEAK libc probes when choosing a tag.
    # These are private deployment wheels; readelf below checks mandatory ABI
    # requirements, while a plain Linux tag avoids an inaccurate pip minimum.
    return ["--compatibility", "linux"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("dev", "release"), default="release")
    parser.add_argument("--out", type=Path, default=ROOT / "native-dist")
    parser.add_argument("--target", help="Rust target triple; foreign targets are package-only")
    parser.add_argument("--wheel-only", action="store_true", help="omit the worker and engine helper binaries")
    parser.add_argument("--binaries-only", action="store_true", help="build portable worker/helper without PyO3")
    parser.add_argument("--no-install", action="store_true", help="do not install/copy a host wheel")
    parser.add_argument("--max-glibc", default="2.35", help="maximum GNU/Linux GLIBC requirement (default: 2.35)")
    parser.add_argument("--zig", action="store_true", help="link GNU/Linux artifacts against --max-glibc with Zig")
    args = parser.parse_args()
    if args.wheel_only and args.binaries_only:
        parser.error("--wheel-only and --binaries-only are mutually exclusive")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", args.max_glibc):
        parser.error("--max-glibc must be a dotted numeric version")
    started = datetime.now(timezone.utc).isoformat()
    env = environment()
    rustc_version = run(["rustc", "-vV"], env=env)
    host = next(line.partition(":")[2].strip()
                for line in rustc_version.splitlines()
                if line.startswith("host:"))
    target = args.target or host
    if args.zig and not target.endswith("linux-gnu"):
        parser.error("--zig is only supported for GNU/Linux targets")
    foreign = target != host
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    native_artifacts: list[dict[str, object]] = []
    target_args = ["--target", target] if args.target or args.zig else []
    if not args.wheel_only:
        packages = ["-p", "tts-worker"]
        binaries = ("tts-worker",) if "android" in target else ("tts-worker", "tts-worldline-native")
        if "android" not in target:
            packages += ["-p", "tts-engines"]
        cargo_target_args = ["--target", target + "." + args.max_glibc] if args.zig else target_args
        run(["cargo", "zigbuild" if args.zig else "build", "--locked", "--profile", args.profile,
             *cargo_target_args, *packages, "--bins"], env=env)
        target_dir = Path(env.get("CARGO_TARGET_DIR", ROOT / "target"))
        if not target_dir.is_absolute():
            target_dir = ROOT / target_dir
        profile_dir = "debug" if args.profile == "dev" else "release"
        build_dir = target_dir / target / profile_dir if args.target or args.zig else target_dir / profile_dir
        for name in binaries:
            source = build_dir / name
            if not source.is_file():
                raise RuntimeError(f"expected native binary missing: {source}")
            destination = out / target / name
            data = source.read_bytes()
            libc = glibc_requirement(source, args.max_glibc) if target.endswith("linux-gnu") else None
            atomic_write(destination, data, mode=0o755)
            native_artifacts.append({"name": name, "target": target,
                                     "path": str(destination.relative_to(out)),
                                     "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                     "glibc_required": libc})
    wheel_path: Path | None = None
    if not args.binaries_only:
        if "android" in target:
            parser.error("Android has no PyO3 wheel here; use --binaries-only")
        wheel_out = out / "wheels" / target
        wheel_out.mkdir(parents=True, exist_ok=True)
        before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in wheel_out.glob("*.whl")}
        compatibility_args = wheel_compatibility_args(target, zig=args.zig, maximum_glibc=args.max_glibc)
        run([sys.executable, "-m", "maturin", "build", "--locked", "--profile", args.profile,
             "--manifest-path", "vinculo_python/Cargo.toml", "--interpreter", sys.executable,
             "--out", os.fspath(wheel_out), *target_args, *compatibility_args], env=env)
        wheels = [p for p in wheel_out.glob("*.whl")
                  if p not in before or before[p] != (p.stat().st_mtime_ns, p.stat().st_size)]
        # A reproducible rebuild may preserve the exact wheel timestamp.
        if not wheels:
            wheels = sorted(wheel_out.glob("*.whl"), key=lambda p: p.stat().st_mtime_ns, reverse=True)[:1]
        if len(wheels) != 1:
            raise RuntimeError(f"ambiguous wheel output: {wheels}")
        wheel_path = wheels[0]
        wheel_libraries = []
        if target.endswith("linux-gnu"):
            with zipfile.ZipFile(wheel_path) as archive, tempfile.TemporaryDirectory(prefix="tts-wheel-abi-") as temporary:
                for name in archive.namelist():
                    if name.endswith(".so"):
                        library = Path(temporary) / Path(name).name
                        library.write_bytes(archive.read(name))
                        wheel_libraries.append({"name": name,
                            "glibc_required": glibc_requirement(library, args.max_glibc)})
        data = wheel_path.read_bytes()
        native_artifacts.append({"name": wheel_path.name, "target": target,
                                 "path": str(wheel_path.relative_to(out)),
                                 "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                                 "libraries": wheel_libraries})
        if not args.no_install and not foreign:
            install_extension(wheel_path, env=env, install_wheel=True)
    manifest = {"schema": "tts-native-local-build-v1", "host": host, "target": target,
                "started_utc": started, "finished_utc": datetime.now(timezone.utc).isoformat(),
                "rustc": rustc_version.strip(),
                "profile": args.profile, "python": platform.python_version(),
                "maximum_glibc": args.max_glibc if target.endswith("linux-gnu") else None,
                "zig": args.zig,
                "host_extension_installed": wheel_path is not None and not args.no_install and not foreign,
                "artifacts": native_artifacts}
    atomic_write(out / (target + "-build.json"), json.dumps(manifest, indent=2).encode() + b"\n")
    executables = [artifact for artifact in native_artifacts
                   if artifact["name"] in {"tts-worker", "tts-worldline-native"}]
    if executables:
        package = (ROOT / "crates/tts-worker/Cargo.toml").read_text(encoding="utf-8")
        version_match = re.search(r'^version\s*=\s*"([^"]+)"', package, re.MULTILINE)
        if version_match is None:
            raise RuntimeError("worker package version unavailable")
        install_manifest = {"schema": "tts-native-artifact-v1", "version": version_match.group(1),
                            "target": target, "artifacts": [
                                {"name": artifact["name"], "file": artifact["name"],
                                 "bytes": artifact["bytes"], "sha256": artifact["sha256"]}
                                for artifact in executables]}
        atomic_write(out / target / "manifest.json", json.dumps(install_manifest, indent=2).encode() + b"\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import hashlib
import shlex
import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> bytes:
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if cp.returncode != 0:
        message = cp.stderr.decode("utf-8", "replace").strip() or f"git {' '.join(args)} failed"
        raise SystemExit(message)
    return cp.stdout


def _emit_shell(values: dict[str, str]) -> None:
    for key, value in values.items():
        print(f"{key}={shlex.quote(value)}")


def diff_snapshot(repo: Path, *, cached: bool, base: str | None, target: str | None) -> None:
    args = ["diff"]
    if cached:
        args.append("--cached")
    elif base is not None and target is not None:
        args.extend([base, target])
    elif base is not None:
        args.append(base)
    args.extend(["--raw", "--numstat", "--no-renames", "-z"])
    raw = _git(repo, *args)
    tokens = raw.split(b"\0")
    statuses: list[str] = []
    files: list[str] = []
    numstat: list[str] = []
    i = 0
    seen: set[str] = set()
    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue
        if token.startswith(b":"):
            if i + 1 >= len(tokens):
                raise SystemExit("malformed git diff --raw output")
            meta = token.decode("utf-8", "surrogateescape")
            path = tokens[i + 1].decode("utf-8", "surrogateescape")
            parts = meta.split()
            if len(parts) < 5:
                raise SystemExit("malformed git diff raw record")
            status = parts[4]
            statuses.append(f"{status}\t{path}")
            if path not in seen:
                seen.add(path)
                files.append(path)
            i += 2
            continue
        text = token.decode("utf-8", "surrogateescape")
        if text.count("\t") >= 2:
            numstat.append(text)
        i += 1
    _emit_shell(
        {
            "GIT_DIFF_STATUS_RAW": "\n".join(statuses),
            "GIT_DIFF_FILES_RAW": "\n".join(files),
            "GIT_DIFF_NUMSTAT_RAW": "\n".join(numstat),
            "GIT_DIFF_SNAPSHOT_HASH": hashlib.sha256(raw).hexdigest(),
        }
    )


def status_snapshot(repo: Path) -> None:
    raw = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=no")
    tokens = raw.split(b"\0")
    rows: list[str] = []
    files: list[str] = []
    staged: list[str] = []
    unstaged: list[str] = []
    seen: set[str] = set()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token:
            i += 1
            continue
        text = token.decode("utf-8", "surrogateescape")
        if len(text) < 3:
            raise SystemExit("malformed git status record")
        xy = text[:2]
        path = text[3:] if text[2:3] == " " else text[2:].lstrip()
        source = ""
        if xy[0] in {"R", "C"} and i + 1 < len(tokens) and tokens[i + 1]:
            source = tokens[i + 1].decode("utf-8", "surrogateescape")
            i += 1
        display = f"{xy} {path}"
        if source:
            display += f" <- {source}"
        rows.append(display)
        for item in (path, source):
            if item and item not in seen:
                seen.add(item)
                files.append(item)
        if xy[0] not in {" ", "?", "!"}:
            staged.append(path)
            if source:
                staged.append(source)
        if xy[1] not in {" ", "?", "!"}:
            unstaged.append(path)
            if source:
                unstaged.append(source)
        i += 1
    _emit_shell(
        {
            "GIT_STATUS_RAW": "\n".join(rows),
            "GIT_STATUS_FILES_RAW": "\n".join(dict.fromkeys(files)),
            "GIT_STATUS_STAGED_FILES_RAW": "\n".join(dict.fromkeys(staged)),
            "GIT_STATUS_UNSTAGED_FILES_RAW": "\n".join(dict.fromkeys(unstaged)),
            "GIT_STATUS_FINGERPRINT": hashlib.sha256(raw).hexdigest(),
        }
    )



def refs_snapshot(repo: Path, branch: str) -> None:
    raw = _git(repo, "rev-parse", "HEAD", f"origin/{branch}").decode("utf-8", "replace").splitlines()
    if len(raw) < 2:
        raise SystemExit("git rev-parse did not return local and remote refs")
    current, remote = raw[0].strip(), raw[1].strip()
    subject = _git(repo, "log", "-1", "--pretty=%s", remote).decode("utf-8", "replace").rstrip("\n")
    _emit_shell({
        "GIT_REFS_CURRENT": current,
        "GIT_REFS_REMOTE": remote,
        "GIT_REFS_REMOTE_SUBJECT": subject,
    })

def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p_diff = sub.add_parser("diff")
    p_diff.add_argument("--repo", required=True)
    p_diff.add_argument("--cached", action="store_true")
    p_diff.add_argument("--base")
    p_diff.add_argument("--target")
    p_status = sub.add_parser("status")
    p_status.add_argument("--repo", required=True)
    p_refs = sub.add_parser("refs")
    p_refs.add_argument("--repo", required=True)
    p_refs.add_argument("--branch", required=True)
    args = parser.parse_args()
    repo = Path(args.repo).resolve()
    if args.command == "diff":
        if args.cached and (args.base or args.target):
            parser.error("--cached cannot be combined with --base/--target")
        if args.target and not args.base:
            parser.error("--target requires --base")
        diff_snapshot(repo, cached=args.cached, base=args.base, target=args.target)
    elif args.command == "status":
        status_snapshot(repo)
    else:
        refs_snapshot(repo, args.branch)


if __name__ == "__main__":
    main()

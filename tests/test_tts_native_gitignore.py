"""Stage native source assets in the same isolated worktree used by the updater."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = (
    "crates/tts-engines/data/README.md",
    "crates/tts-engines/data/esper-startup-hook.base64",
    "crates/tts-render/data/CMUDICT-LICENSE.txt",
    "crates/tts-render/data/README.md",
    "crates/tts-render/data/cmudict.txt",
    "crates/tts-render/data/g2p-contracts.json",
    "crates/tts-render/data/phonemizer-contracts.json",
)


@pytest.mark.parametrize("ignore_location", ["tracked", "info-exclude", "global"])
def test_isolated_candidate_stages_source_assets_without_forcing_runtime_data(tmp_path, ignore_location):
    environment = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(directory, *args, check=True):
        return subprocess.run(["git", "-C", str(directory), *args], check=check,
                              capture_output=True, text=True, env=environment)

    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Native source test")
    git(repo, "config", "user.email", "native-source@example.invalid")
    (repo / ".gitignore").write_text("data/\n" if ignore_location == "tracked" else "# base\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "baseline")
    if ignore_location == "info-exclude":
        (repo / ".git/info/exclude").write_text("data/\n")
    elif ignore_location == "global":
        excludes = tmp_path / "global.gitignore"
        excludes.write_text("data/\n")
        git(repo, "config", "core.excludesFile", str(excludes))

    candidate = tmp_path / "isolated-candidate"
    git(repo, "worktree", "add", "--quiet", "--detach", str(candidate), "HEAD")
    for name in ASSETS:
        destination = candidate / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("static source fixture\n")
    rejected = git(candidate, "add", "-A", "--", *ASSETS, check=False)
    assert rejected.returncode != 0 and "ignored" in rejected.stderr

    # The candidate copies its replacement .gitignore before git add.
    (candidate / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
    runtime = candidate / "data/runtime.json"
    runtime.parent.mkdir()
    runtime.write_text('{"private":true}\n')
    scratch = candidate / "crates/tts-render/data/local-cache.json"
    scratch.write_text('{"generated":true}\n')
    git(candidate, "add", "-A", "--", ".gitignore", *ASSETS)
    staged = set(git(candidate, "diff", "--cached", "--name-only").stdout.splitlines())
    assert staged == {".gitignore", *ASSETS}
    for name in ASSETS:
        assert git(candidate, "check-ignore", "--no-index", "--", name, check=False).returncode == 1
    for name in ("data/runtime.json", "crates/tts-render/data/local-cache.json"):
        assert git(candidate, "check-ignore", "--no-index", "--", name, check=False).returncode == 0

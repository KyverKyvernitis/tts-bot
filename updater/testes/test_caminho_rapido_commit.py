from __future__ import annotations

from pathlib import Path

from updater.testes.fonte_core import caminho_fonte_core

ROOT = Path(__file__).resolve().parents[2]
UPDATER = caminho_fonte_core()


def _function_block(source: str, name: str, next_name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index(f"\n{next_name}() {{", start)
    return source[start:end]


def test_candidate_commit_and_head_share_one_sudo_boundary_without_skipping_hooks() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _function_block(source, "candidate_commit_and_head", "prepare_local_candidate_commit_in_worktree")

    assert block.count("\n  sudo -u ubuntu") == 1
    assert 'git -C "$root" commit --quiet' in block
    assert 'git -C "$root" rev-parse HEAD' in block
    # A otimização não pode transformar o commit numa rota que ignore políticas
    # locais do repositório. Hooks e assinatura configurada continuam valendo.
    assert "--no-verify" not in block
    assert "--no-gpg-sign" not in block
    assert "commit.gpgSign=false" not in block


def test_coarse_prepare_time_is_closed_before_real_commit_timer() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _function_block(
        source,
        "prepare_local_candidate_commit_in_worktree",
        "local_candidate_artifact_root_for_commit",
    )

    prepare_at = block.index('mark_update_timing "prepare"')
    commit_start_at = block.index('commit_started_ms="$(update_now_ms)"')
    commit_call_at = block.index("candidate_commit_and_head")
    precise_commit_at = block.index('append_update_timing_ms "commit"')

    assert prepare_at < commit_start_at < commit_call_at < precise_commit_at
    assert 'mark_update_timing "commit"' not in block


def test_commit_state_persistence_has_its_own_timing_label() -> None:
    source = UPDATER.read_text(encoding="utf-8")
    block = _function_block(
        source,
        "prepare_local_candidate_commit_in_worktree",
        "local_candidate_artifact_root_for_commit",
    )

    assert 'log_update_operation_timing_ms "preparation.commit.command_and_head"' in block
    assert 'log_update_operation_timing_ms "preparation.commit"' in block
    assert 'log_update_operation_timing_ms "preparation.commit.state_write"' in block

import os
import time

import pytest

from cogs.musica.runtime_telefone.agente.archive_staging import ArchiveWorkspace, ArchiveWorkspaceBusy


def test_falha_preserva_temporario_e_retomada_limpa_apos_confirmacao(tmp_path):
    key = "a" * 32
    with pytest.raises(RuntimeError, match="upload"):
        with ArchiveWorkspace(key, {"source": "source-1"}, root=tmp_path) as staging:
            (staging.path / "audio.ogg").write_bytes(b"audio")
            staging.save_reference({"guild_id": 1, "message_id": 2})
            raise RuntimeError("upload interrompido")
    with ArchiveWorkspace(key, {"source": "source-1"}, root=tmp_path) as staging:
        assert (staging.path / "audio.ogg").read_bytes() == b"audio"
        assert staging.load_reference()["message_id"] == 2
        staging.complete()
    assert not (tmp_path / key).exists()


def test_fonte_alterada_invalida_audio_e_referencia_temporarios(tmp_path):
    key = "a" * 32
    with ArchiveWorkspace(key, {"source": "old"}, root=tmp_path) as staging:
        (staging.path / "audio.ogg").write_bytes(b"old")
        staging.save_reference({"message_id": 2})
    with ArchiveWorkspace(key, {"source": "new"}, root=tmp_path) as staging:
        assert not (staging.path / "audio.ogg").exists()
        assert staging.load_reference() == {}
        staging.complete()


def test_limpeza_expirada_preserva_upload_ativo(tmp_path):
    active = "a" * 32
    abandoned = "b" * 32
    with ArchiveWorkspace(abandoned, {}, root=tmp_path):
        pass
    old = time.time() - 120
    os.utime(tmp_path / abandoned, (old, old))
    with ArchiveWorkspace(active, {}, root=tmp_path) as staging:
        os.utime(staging.path, (old, old))
        with ArchiveWorkspace("c" * 32, {}, root=tmp_path, ttl_seconds=60) as other:
            assert staging.path.exists()
            assert not (tmp_path / abandoned).exists()
            other.complete()
        staging.complete()


def test_trabalho_duplicado_nao_partilha_mesmos_temporarios(tmp_path):
    key = "a" * 32
    with ArchiveWorkspace(key, {}, root=tmp_path) as staging:
        with pytest.raises(ArchiveWorkspaceBusy):
            with ArchiveWorkspace(key, {}, root=tmp_path):
                pytest.fail("upload duplicado")
        staging.complete()


def test_manutencao_limpa_abandonado_sem_novo_upload_e_preserva_ativo(tmp_path):
    old = time.time() - 120
    key = "0" * 32
    with ArchiveWorkspace(key, {}, root=tmp_path):
        pass
    os.utime(tmp_path / key, (old, old))
    with ArchiveWorkspace("a" * 32, {}, root=tmp_path) as active:
        os.utime(active.path, (old, old))
        assert ArchiveWorkspace.cleanup_expired(root=tmp_path, ttl_seconds=60) == 1
        assert active.path.exists()
        assert not (tmp_path / key).exists()
        active.complete()
    assert ArchiveWorkspace.cleanup_expired(root=tmp_path, ttl_seconds=60) == 0

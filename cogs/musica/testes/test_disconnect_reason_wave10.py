from pathlib import Path


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_vps_never_calls_unknown_disconnect_external_by_default():
    source = (_root() / "cogs/musica/legado/roteador_audio.py").read_text(encoding="utf-8")
    assert '"external_disconnect" if actor is not None else "unknown_disconnect"' in source
    assert 'self._set_idle_reason(state, "worker_unreachable"' in source
    assert 'voice_alone_timeout_disconnect' in source
    assert 'voice_empty_timeout_disconnect' in source


def test_player_has_distinct_disconnect_messages():
    source = (_root() / "cogs/musica/interface/componentes.py").read_text(encoding="utf-8")
    for reason in (
        "music_alone_timeout",
        "music_idle_timeout",
        "voice_idle_empty",
        "voice_connection_lost",
        "worker_unreachable",
        "unknown_disconnect",
        "external_disconnect",
    ):
        assert reason in source
    assert "Motivo: causa não determinada" in source
    assert "não há registro de quem o removeu nem de uma saída automática" in source


def test_worker_snapshot_exposes_disconnect_cause():
    source = (_root() / "cogs/musica/runtime_telefone/agente/estado.py").read_text(encoding="utf-8")
    for field in (
        '"last_disconnect_reason"',
        '"last_disconnect_event"',
        '"last_disconnect_at"',
        '"last_disconnect_human_count"',
    ):
        assert field in source

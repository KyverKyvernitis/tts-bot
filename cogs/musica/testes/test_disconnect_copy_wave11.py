from pathlib import Path


def _source() -> str:
    return (Path(__file__).resolve().parents[1] / "interface" / "componentes.py").read_text(encoding="utf-8")


def test_disconnect_copy_uses_concrete_reasons():
    source = _source()
    expected = (
        "Saí do canal por ficar sozinho",
        "Fiquei 2 minutos sem ninguém no canal de voz",
        "Fila encerrada",
        "nenhuma outra começou nos 2 minutos seguintes",
        "Canal vazio",
        "Esperei 2 segundos para confirmar e saí",
        "Não consegui voltar ao canal",
        "Confirmando estado do player",
        "Removido do canal",
        "Movido para outro canal",
        "Bot saiu do canal",
    )
    for text in expected:
        assert text in source


def test_disconnect_copy_does_not_expose_internal_transport_names_or_guess_actor():
    source = _source()
    forbidden = (
        "Phone Worker inacessível",
        "Reconectando ao Phone Worker",
        "Depois do TTS",
        "por alguém",
        "um moderador",
        "Player interrompido",
        "Player desconectado",
    )
    for text in forbidden:
        assert text not in source


def test_uncertain_disconnect_preserves_state_and_does_not_accuse_someone():
    source = _source()
    assert "Ainda não consegui confirmar se a reprodução continua no canal" in source
    assert "A faixa e a fila continuam preservadas enquanto faço uma nova verificação" in source
    assert "não há registro de quem o removeu nem de uma saída automática" in source
    assert "Motivo: causa não determinada" in source


def test_idle_reasons_have_compact_audit_notes():
    source = _source()
    for text in (
        "Motivo: canal sem usuários · 120 s",
        "Motivo: sem nova música · 120 s",
        "Motivo: canal sem usuários · 2 s",
        "Motivo: conexão de voz perdida",
        "Motivo: remoção manual",
        "Estado: confirmação pendente",
    ):
        assert text in source

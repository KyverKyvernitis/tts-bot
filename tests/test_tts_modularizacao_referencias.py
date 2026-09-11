from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.tts.mensagens.referencias import (
    descricoes_anexos_tts,
    referencia_canal_tts,
    referencia_cargo_tts,
    referencia_link_tts,
    referencia_usuario_tts,
)
from cogs.tts.utils.text import (
    tts_attachment_descriptions,
    tts_channel_reference,
    tts_link_reference,
    tts_role_reference,
    tts_user_reference,
)


def _normalizar(texto: str) -> str:
    return " ".join(str(texto or "").split())


def _pronunciavel(texto: str) -> bool:
    return any(ch.isalnum() for ch in str(texto or ""))


def _nome_falado(texto: str) -> str:
    return _normalizar(texto)


def _dominio(hostname: str) -> str:
    partes = [p for p in str(hostname or "").split(".") if p and p != "www"]
    return partes[-2] if len(partes) >= 2 else (partes[0] if partes else "")


def test_modulo_portugues_preserva_referencias_basicas():
    membro = SimpleNamespace(display_name="Core")
    cargo = SimpleNamespace(name="Moderador")
    canal = SimpleNamespace(name="geral")

    assert referencia_usuario_tts(
        membro,
        resolvedor=lambda item, guild_id=None: (item.display_name, "teste"),
        guild_id=123,
    ) == "Core"
    assert referencia_cargo_tts(
        cargo,
        normalizar_espacos=_normalizar,
        parece_pronunciavel_para_tts=_pronunciavel,
        nome_falado=_nome_falado,
    ) == "cargo Moderador"
    assert referencia_canal_tts(
        canal,
        normalizar_espacos=_normalizar,
        parece_pronunciavel_para_tts=_pronunciavel,
        nome_falado=_nome_falado,
    ) == "canal geral"


def test_modulo_portugues_preserva_links_discord_e_externos():
    canal = SimpleNamespace(name="voz")
    guild = SimpleNamespace(get_channel=lambda channel_id: canal if channel_id == 456 else None)
    padrao = re.compile(r"https://discord\.com/channels/(\d+)/(\d+)")

    kwargs = dict(
        padrao_url_canal_discord=padrao,
        referencia_canal=lambda item: f"canal {item.name}" if item else "canal do discord",
        extrair_dominio_principal=_dominio,
        parece_pronunciavel_para_tts=_pronunciavel,
        nome_falado=_nome_falado,
    )

    assert referencia_link_tts(
        "https://discord.com/channels/123/456",
        guild=guild,
        **kwargs,
    ) == "canal voz"
    assert referencia_link_tts("https://www.youtube.com/watch?v=abc", **kwargs) == "link do youtube"


def test_modulo_portugues_preserva_descricoes_de_anexos():
    anexos = [
        SimpleNamespace(content_type="image/gif", filename="a.gif"),
        SimpleNamespace(content_type="image/png", filename="b.png"),
        SimpleNamespace(content_type="video/mp4", filename="c.mp4"),
        SimpleNamespace(content_type="application/pdf", filename="d.pdf"),
    ]
    esperado = ["Anexo em GIF", "Anexo de imagem", "Anexo de vídeo"]
    assert descricoes_anexos_tts(
        anexos,
        extensoes_imagem=(".png", ".jpg"),
        extensoes_video=(".mp4", ".webm"),
    ) == esperado


def test_fachada_legada_continua_compativel_com_modulo_portugues():
    membro = SimpleNamespace(display_name="Core")
    cargo = SimpleNamespace(name="Moderador")
    canal = SimpleNamespace(name="geral")
    anexo = SimpleNamespace(content_type="image/png", filename="foto.png")
    padrao = re.compile(r"https://discord\.com/channels/(\d+)/(\d+)")

    assert tts_user_reference(
        membro,
        resolver=lambda item, guild_id=None: (item.display_name, "teste"),
        guild_id=321,
    ) == "Core"
    assert tts_role_reference(
        cargo,
        normalize_spaces=_normalizar,
        looks_pronounceable_for_tts=_pronunciavel,
        speech_name=_nome_falado,
    ) == "cargo Moderador"
    assert tts_channel_reference(
        canal,
        normalize_spaces=_normalizar,
        looks_pronounceable_for_tts=_pronunciavel,
        speech_name=_nome_falado,
    ) == "canal geral"
    assert tts_link_reference(
        "https://www.youtube.com/watch?v=abc",
        discord_channel_url_pattern=padrao,
        channel_reference=lambda item: "canal geral",
        extract_primary_domain=_dominio,
        looks_pronounceable_for_tts=_pronunciavel,
        speech_name=_nome_falado,
    ) == "link do youtube"
    assert tts_attachment_descriptions(
        [anexo],
        image_extensions=(".png",),
        video_extensions=(".mp4",),
    ) == ["Anexo de imagem"]

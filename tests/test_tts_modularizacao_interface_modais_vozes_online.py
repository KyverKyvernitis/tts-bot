from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "cogs" / "tts" / "ui.py"
MODULO_PATH = ROOT / "cogs" / "tts" / "interface" / "modais_vozes_online.py"

CLASSES_CANONICAS = {"ModalConfiguracaoEdge", "ModalConfiguracaoGTTS"}
ALIASES_LEGADOS = {
    "EdgeSettingsModal": "ModalConfiguracaoEdge",
    "GTTSSettingsModal": "ModalConfiguracaoGTTS",
}


class TTSInterfaceModaisVozesOnlineEstruturaTests(unittest.TestCase):
    def test_implementacoes_foram_extraidas_com_nomes_em_portugues(self):
        ui_tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        modulo_tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        classes_ui = {node.name for node in ui_tree.body if isinstance(node, ast.ClassDef)}
        classes_modulo = {node.name for node in modulo_tree.body if isinstance(node, ast.ClassDef)}

        self.assertTrue(CLASSES_CANONICAS <= classes_modulo)
        self.assertTrue(set(ALIASES_LEGADOS).isdisjoint(classes_ui))

    def test_ui_preserva_nomes_legados_por_alias_de_importacao(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        encontrados: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module != "interface.modais_vozes_online":
                continue
            for alias in node.names:
                encontrados[str(alias.asname or alias.name)] = alias.name
        self.assertEqual(encontrados, ALIASES_LEGADOS)

    def test_modulo_novo_nao_importa_audio_worker_termux_apk_streaming_ou_rede(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "worker", "termux", "android", "streaming", "routing", "urllib", "http"):
            self.assertNotIn(proibido, joined)


class _ModalBase:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def __init__(self):
        self.children = []

    def add_item(self, item):
        self.children.append(item)

    def clear_items(self):
        self.children.clear()


class _Input:
    def __init__(self, value=""):
        self.value = value
        self.values = []


def _carregar_modulo():
    discord = types.ModuleType("discord")
    discord.Interaction = type("Interaction", (), {})
    discord.Message = type("Message", (), {})
    discord.ui = types.SimpleNamespace(Modal=_ModalBase)

    config = types.ModuleType("config")
    config.EDGE_TTS_VOICE = "pt-BR-FranciscaNeural"

    embed = types.ModuleType("cogs.tts.utils.embed")
    embed.human_language_name = lambda value: f"idioma:{value}"
    embed.human_pitch = lambda value: f"tom:{value}"
    embed.human_rate = lambda value: f"velocidade:{value}"
    embed.human_voice_name = lambda value: f"voz:{value}"

    catalogos = types.ModuleType("cogs.tts.interface.catalogos_de_vozes")
    catalogos.idioma_edge_da_voz = lambda voz, padrao="pt-BR": "-".join(str(voz).split("-")[:2]) if "-" in str(voz) else padrao
    catalogos.opcoes_idiomas_edge = lambda cog, atual="": []
    catalogos.opcoes_vozes_edge_por_idioma = lambda cog, *, idioma, atual="": []
    catalogos.primeira_voz_edge_por_idioma = lambda cog, idioma, atual="": ""
    catalogos.principais_opcoes_idiomas_gtts = lambda cog, atual="": []
    catalogos.voz_edge_corresponde_idioma = lambda voz, idioma: str(voz).startswith(str(idioma) + "-")

    componentes = types.ModuleType("cogs.tts.interface.componentes")

    def adicionar_entrada_texto_modal(modal, nome, *, current="", **kwargs):
        item = _Input(current)
        setattr(modal, nome, item)
        modal.add_item(item)
        return item

    componentes.adicionar_entrada_texto_modal = adicionar_entrada_texto_modal
    componentes.adicionar_item_rotulo_modal = lambda *args, **kwargs: True
    componentes.adicionar_radio_modal = lambda *args, **kwargs: True
    componentes.criar_entrada_texto_modal = lambda **kwargs: _Input(kwargs.get("current", ""))
    componentes.criar_seletor_modal = lambda *args, **kwargs: _Input()
    componentes.opcoes_com_valor_padrao = lambda opcoes, atual: opcoes
    componentes.rotulo_modal_disponivel = lambda: False
    componentes.valor_item = lambda item: str(getattr(item, "value", "") or "")
    componentes.valor_tts_atual = lambda cog, guild_id, user_id, key, default, *, server: getattr(cog, "valores", {}).get(key, default)
    componentes.valor_unico_componente = lambda item, default="": str(getattr(item, "value", default) or default)

    operacoes = types.ModuleType("cogs.tts.interface.operacoes_painel")
    operacoes.salvar_atualizacoes_modal_tts = AsyncMock()

    cogs_pkg = types.ModuleType("cogs")
    cogs_pkg.__path__ = []
    tts_pkg = types.ModuleType("cogs.tts")
    tts_pkg.__path__ = []
    utils_pkg = types.ModuleType("cogs.tts.utils")
    utils_pkg.__path__ = []
    interface_pkg = types.ModuleType("cogs.tts.interface")
    interface_pkg.__path__ = []

    spec = importlib.util.spec_from_file_location("cogs.tts.interface.modais_vozes_online", MODULO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(
        sys.modules,
        {
            "discord": discord,
            "config": config,
            "cogs": cogs_pkg,
            "cogs.tts": tts_pkg,
            "cogs.tts.utils": utils_pkg,
            "cogs.tts.utils.embed": embed,
            "cogs.tts.interface": interface_pkg,
            "cogs.tts.interface.catalogos_de_vozes": catalogos,
            "cogs.tts.interface.componentes": componentes,
            "cogs.tts.interface.operacoes_painel": operacoes,
        },
    ):
        spec.loader.exec_module(module)
    return module, operacoes.salvar_atualizacoes_modal_tts


class TTSInterfaceModaisVozesOnlineComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def test_assinaturas_preservam_contrato_legado(self):
        modulo, _ = _carregar_modulo()
        esperada = [
            "self",
            "cog",
            "panel_message",
            "server",
            "target_user_id",
            "target_user_name",
            "force_text_fallback",
        ]
        for nome in CLASSES_CANONICAS:
            with self.subTest(nome=nome):
                self.assertEqual(list(inspect.signature(getattr(modulo, nome).__init__).parameters), esperada)

    async def test_edge_fallback_preserva_campos_e_salvamento_sem_mudanca(self):
        modulo, salvar = _carregar_modulo()
        cog = types.SimpleNamespace(
            valores={
                "voice": "pt-BR-FranciscaNeural",
                "rate": "+0%",
                "pitch": "+0Hz",
            },
            edge_voice_names={"pt-BR-FranciscaNeural"},
            edge_voice_cache=[],
            _normalize_rate_value=lambda value: value,
            _normalize_pitch_value=lambda value: value,
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        panel = types.SimpleNamespace(guild=types.SimpleNamespace(id=55))
        modal = modulo.ModalConfiguracaoEdge(cog, panel, server=False, target_user_id=77, target_user_name="Alvo", force_text_fallback=True)
        self.assertEqual(modal.current_language, "pt-BR")
        self.assertTrue(all(hasattr(modal, nome) for nome in ("language", "voice", "rate", "pitch")))

        interaction = types.SimpleNamespace(response=types.SimpleNamespace(send_message=AsyncMock()))
        await modal.on_submit(interaction)
        salvar.assert_awaited_once()
        kwargs = salvar.await_args.kwargs
        self.assertEqual(kwargs["updates"], {})
        self.assertEqual(kwargs["success_title"], "Edge atualizado")
        self.assertEqual(kwargs["success_description"], "Nada mudou")
        self.assertEqual(kwargs["target_user_id"], 77)
        self.assertEqual(kwargs["target_user_name"], "Alvo")

    async def test_edge_troca_idioma_reseleciona_voz_compativel(self):
        modulo, salvar = _carregar_modulo()
        cog = types.SimpleNamespace(
            valores={"voice": "pt-BR-FranciscaNeural", "rate": "+0%", "pitch": "+0Hz"},
            edge_voice_names={"pt-BR-FranciscaNeural", "en-US-GuyNeural"},
            edge_voice_cache=[],
            _normalize_rate_value=lambda value: value,
            _normalize_pitch_value=lambda value: value,
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        modal = modulo.ModalConfiguracaoEdge(cog, None, server=True, force_text_fallback=True)
        modal.language.value = "en-US"
        modal.voice.value = "pt-BR-FranciscaNeural"
        modulo.primeira_voz_edge_por_idioma = lambda cog, idioma, atual="": "en-US-GuyNeural"

        interaction = types.SimpleNamespace(response=types.SimpleNamespace(send_message=AsyncMock()))
        await modal.on_submit(interaction)
        salvar.assert_awaited_once()
        self.assertEqual(salvar.await_args.kwargs["updates"], {"voice": "en-US-GuyNeural"})
        self.assertIn("Idioma · idioma:en-US", salvar.await_args.kwargs["success_description"])
        self.assertIn("Voz · voz:en-US-GuyNeural", salvar.await_args.kwargs["success_description"])

    async def test_gtts_idioma_manual_tem_prioridade_e_preserva_destino(self):
        modulo, salvar = _carregar_modulo()
        cog = types.SimpleNamespace(
            valores={"language": "pt-br"},
            _resolve_gtts_language_input=lambda value: (str(value).lower(), "ok"),
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        panel = types.SimpleNamespace(guild=types.SimpleNamespace(id=12))
        modal = modulo.ModalConfiguracaoGTTS(cog, panel, server=False, target_user_id=9, target_user_name="Pessoa", force_text_fallback=True)
        modal.manual_language = _Input("ja")
        interaction = types.SimpleNamespace(response=types.SimpleNamespace(send_message=AsyncMock()))

        await modal.on_submit(interaction)
        salvar.assert_awaited_once()
        kwargs = salvar.await_args.kwargs
        self.assertEqual(kwargs["updates"], {"language": "ja"})
        self.assertEqual(kwargs["success_description"], "Idioma · idioma:ja")
        self.assertEqual(kwargs["target_user_id"], 9)
        self.assertEqual(kwargs["target_user_name"], "Pessoa")

    async def test_gtts_invalido_responde_sem_salvar(self):
        modulo, salvar = _carregar_modulo()
        cog = types.SimpleNamespace(
            valores={"language": "pt-br"},
            _resolve_gtts_language_input=lambda value: (None, None),
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        modal = modulo.ModalConfiguracaoGTTS(cog, None, server=False, force_text_fallback=True)
        modal.language.value = "idioma-invalido"
        response = types.SimpleNamespace(send_message=AsyncMock())
        interaction = types.SimpleNamespace(response=response)

        await modal.on_submit(interaction)
        response.send_message.assert_awaited_once()
        salvar.assert_not_awaited()

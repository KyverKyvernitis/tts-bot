from __future__ import annotations

import ast
import importlib.util
import inspect
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "cogs" / "tts" / "ui.py"
MODULO_PATH = ROOT / "cogs" / "tts" / "interface" / "modais_atts.py"

FUNCOES_CANONICAS = {
    "chave_cache_catalogo_vozes_atts",
    "buscar_catalogo_vozes_atts_sincrono",
    "idioma_atual_modal_atts",
    "carregar_catalogo_vozes_atts_para_modal",
    "enviar_indisponibilidade_atts_minima",
    "enviar_modal_configuracao_atts",
}


class TTSInterfaceModaisATTSEstruturaTests(unittest.TestCase):
    def test_implementacao_real_esta_no_modulo_em_portugues(self):
        modulo = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        ui = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        funcoes = {node.name for node in modulo.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        classes = {node.name for node in modulo.body if isinstance(node, ast.ClassDef)}
        classes_ui = {node.name for node in ui.body if isinstance(node, ast.ClassDef)}
        self.assertTrue(FUNCOES_CANONICAS <= funcoes)
        self.assertIn("ModalConfiguracaoATTS", classes)
        self.assertNotIn("AndroidSettingsModal", classes_ui)

    def test_cache_e_mensagem_de_erro_agora_sao_definidos(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        atribuicoes = {
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
            )
            if isinstance(target, ast.Name)
        }
        self.assertIn("CACHE_CATALOGO_VOZES_ATTS", atribuicoes)
        self.assertIn("MENSAGEM_ERRO_CARREGAMENTO_ATTS", atribuicoes)

    def test_ui_preserva_fachadas_e_alias_do_modal(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        defs = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        esperadas = {
            "_atts_voice_cache_key",
            "_fetch_atts_voice_catalog_sync",
            "_atts_modal_current_language",
            "_load_atts_voice_catalog_for_modal",
            "_send_minimal_atts_unavailable",
            "_send_atts_settings_modal",
        }
        self.assertTrue(esperadas <= set(defs))
        aliases = {
            target.id: node.value.id
            for node in tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        self.assertEqual(aliases.get("AndroidSettingsModal"), "ModalConfiguracaoATTS")

    def test_modulo_novo_nao_importa_audio_streaming_routing_termux_ou_apk(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "streaming", "routing", "termux", "apk"):
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


class _SelectOption:
    def __init__(self, *, label="", value="", description=None, default=False):
        self.label = label
        self.value = value
        self.description = description
        self.default = default


def _normalizar_localidade(valor: object, padrao: str = "pt-BR") -> str:
    bruto = str(valor or padrao or "").strip().replace("_", "-")
    if not bruto:
        return str(padrao or "").strip()
    partes = [parte for parte in bruto.split("-") if parte]
    if len(partes) == 1:
        return partes[0].lower()
    return f"{partes[0].lower()}-{partes[1].upper()}"


def _normalizar_fator(valor: object, padrao: str = "1.0") -> str | None:
    bruto = str(valor or padrao or "1.0").strip().lower().replace("x", "").replace(",", ".")
    try:
        numero = float(bruto)
    except Exception:
        return None
    numero = max(0.5, min(2.0, numero))
    return f"{numero:.2f}".rstrip("0").rstrip(".") or "1"


def _normalizar_fator_personalizado(valor: object) -> str | None:
    bruto = str(valor or "").strip().lower().replace("x", "").replace(",", ".")
    if not bruto:
        return None
    try:
        numero = float(bruto)
    except Exception:
        return None
    if numero < 0.5 or numero > 2.0:
        return None
    return f"{numero:.2f}".rstrip("0").rstrip(".") or "1"


def _carregar_modulo():
    discord = types.ModuleType("discord")
    discord.Interaction = type("Interaction", (), {})
    discord.Message = type("Message", (), {})
    discord.SelectOption = _SelectOption
    discord.ui = types.SimpleNamespace(Modal=_ModalBase)

    config = types.ModuleType("config")
    config.PHONE_WORKER_ENABLED = False
    config.PHONE_WORKER_HOST = ""
    config.PHONE_WORKER_TOKEN = ""
    config.PHONE_WORKER_SCHEME = "http"
    config.PHONE_WORKER_PORT = 8766

    common = types.ModuleType("cogs.tts.common")
    common._shorten = lambda value, limit: str(value)[:limit]

    componentes = types.ModuleType("cogs.tts.interface.componentes")
    componentes.valor_tts_atual = lambda cog, guild_id, user_id, key, default, *, server: getattr(cog, "valores", {}).get(key, default)
    componentes.valor_unico_componente = lambda item, default="": str(getattr(item, "value", default) or default)
    componentes.opcoes_com_valor_padrao = lambda opcoes, atual: opcoes
    componentes.rotulo_modal_disponivel = lambda: False
    componentes.criar_entrada_texto_modal = lambda **kwargs: _Input(kwargs.get("current", ""))

    def adicionar_entrada_texto_modal(modal, nome, *, current="", **kwargs):
        item = _Input(current)
        setattr(modal, nome, item)
        modal.add_item(item)
        return item

    componentes.adicionar_entrada_texto_modal = adicionar_entrada_texto_modal
    componentes.adicionar_item_rotulo_modal = lambda *args, **kwargs: True
    componentes.criar_seletor_modal = lambda *args, **kwargs: _Input()
    componentes.adicionar_radio_modal = lambda *args, **kwargs: True

    valores = types.ModuleType("cogs.tts.interface.valores_atts")
    valores.normalizar_localidade_atts = _normalizar_localidade
    valores.normalizar_fator_atts = _normalizar_fator
    valores.normalizar_fator_personalizado_atts = _normalizar_fator_personalizado
    valores.padrao_radio_modal_atts = lambda atual, predefinidos: (_normalizar_fator(atual) if _normalizar_fator(atual) in predefinidos else "custom")

    def separar(valor, *, taxa_padrao="1.0", tom_padrao="1.0"):
        bruto = str(valor or "").strip()
        if not bruto:
            return taxa_padrao, tom_padrao
        partes = bruto.split("/", 1) if "/" in bruto else bruto.split(None, 1)
        if len(partes) == 1:
            return partes[0].strip(), partes[0].strip()
        return partes[0].strip(), partes[1].strip()

    valores.separar_valores_personalizados_atts = separar

    catalogo = types.ModuleType("cogs.tts.interface.catalogo_atts")
    catalogo.opcoes_idiomas_atts = lambda atual="pt-BR", voices=None: []
    catalogo.pontuar_voz_atts = lambda voice, language: 0
    catalogo.vozes_atts_por_idioma = lambda catalog, language: list(catalog or [])
    catalogo.opcoes_vozes_atts_por_idioma = lambda cog, *, idioma, atual="", catalogo=None, buscar_catalogo=None: []
    catalogo.voz_atts_corresponde_idioma = lambda voice, language, *, buscar_catalogo=None: str(voice).startswith(str(language))
    catalogo.primeira_voz_atts_por_idioma = lambda opcoes: ""
    catalogo.catalogo_atts_pronto_para_idioma = lambda catalog, language: bool(catalog)

    operacoes = types.ModuleType("cogs.tts.interface.operacoes_painel")
    operacoes.salvar_atualizacoes_modal_tts = AsyncMock()

    cogs_pkg = types.ModuleType("cogs")
    cogs_pkg.__path__ = []
    tts_pkg = types.ModuleType("cogs.tts")
    tts_pkg.__path__ = []
    interface_pkg = types.ModuleType("cogs.tts.interface")
    interface_pkg.__path__ = []

    spec = importlib.util.spec_from_file_location("cogs.tts.interface.modais_atts", MODULO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(
        sys.modules,
        {
            "discord": discord,
            "config": config,
            "cogs": cogs_pkg,
            "cogs.tts": tts_pkg,
            "cogs.tts.common": common,
            "cogs.tts.interface": interface_pkg,
            "cogs.tts.interface.componentes": componentes,
            "cogs.tts.interface.valores_atts": valores,
            "cogs.tts.interface.catalogo_atts": catalogo,
            "cogs.tts.interface.operacoes_painel": operacoes,
        },
    ):
        spec.loader.exec_module(module)
    return module, operacoes.salvar_atualizacoes_modal_tts, config


class TTSInterfaceModaisATTSComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def test_assinatura_modal_preserva_contrato_legado(self):
        modulo, _, _ = _carregar_modulo()
        self.assertEqual(
            list(inspect.signature(modulo.ModalConfiguracaoATTS.__init__).parameters),
            ["self", "cog", "panel_message", "server", "target_user_id", "target_user_name", "force_text_fallback", "voice_catalog", "allow_text_fallback"],
        )

    def test_worker_indisponivel_nao_tenta_rede_e_registra_erro(self):
        modulo, _, _ = _carregar_modulo()
        modulo.CACHE_CATALOGO_VOZES_ATTS.clear()
        modulo.CACHE_CATALOGO_VOZES_ATTS.update({"by_locale": {}, "last_error": ""})
        modulo.urllib.request.urlopen = Mock(side_effect=AssertionError("rede não deveria ser usada"))
        self.assertEqual(modulo.buscar_catalogo_vozes_atts_sincrono("pt_BR"), [])
        self.assertEqual(modulo.CACHE_CATALOGO_VOZES_ATTS["last_error"], "worker_unavailable")
        modulo.urllib.request.urlopen.assert_not_called()
        self.assertEqual(modulo.chave_cache_catalogo_vozes_atts("pt_BR"), "pt-br")

    async def test_indisponibilidade_envia_mensagem_definida_em_vez_de_nameerror(self):
        modulo, _, _ = _carregar_modulo()
        resposta = types.SimpleNamespace(is_done=lambda: False, send_message=AsyncMock())
        interaction = types.SimpleNamespace(response=resposta, followup=types.SimpleNamespace(send=AsyncMock()))
        await modulo.enviar_indisponibilidade_atts_minima(interaction)
        resposta.send_message.assert_awaited_once_with(modulo.MENSAGEM_ERRO_CARREGAMENTO_ATTS, ephemeral=True)
        interaction.followup.send.assert_not_awaited()

    async def test_indisponibilidade_usa_followup_quando_resposta_ja_foi_consumida(self):
        modulo, _, _ = _carregar_modulo()
        resposta = types.SimpleNamespace(is_done=lambda: True, send_message=AsyncMock())
        followup = types.SimpleNamespace(send=AsyncMock())
        interaction = types.SimpleNamespace(response=resposta, followup=followup)
        await modulo.enviar_indisponibilidade_atts_minima(interaction)
        followup.send.assert_awaited_once_with(modulo.MENSAGEM_ERRO_CARREGAMENTO_ATTS, ephemeral=True)
        resposta.send_message.assert_not_awaited()

    async def test_modal_fallback_preserva_estado_e_salvamento_sem_mudanca(self):
        modulo, salvar, _ = _carregar_modulo()
        cog = types.SimpleNamespace(
            valores={"android_language": "pt-BR", "android_voice": "", "android_rate": "1.0", "android_pitch": "1.0"},
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        panel = types.SimpleNamespace(guild=types.SimpleNamespace(id=7))
        modal = modulo.ModalConfiguracaoATTS(cog, panel, server=False, target_user_id=9, target_user_name="Pessoa", force_text_fallback=True)
        interaction = types.SimpleNamespace(response=types.SimpleNamespace(send_message=AsyncMock()))
        await modal.on_submit(interaction)
        salvar.assert_awaited_once()
        kwargs = salvar.await_args.kwargs
        self.assertEqual(kwargs["updates"], {})
        self.assertEqual(kwargs["success_title"], "ATTS atualizado")
        self.assertEqual(kwargs["success_description"], "Nada mudou.")
        self.assertEqual(kwargs["target_user_id"], 9)
        self.assertEqual(kwargs["target_user_name"], "Pessoa")


if __name__ == "__main__":
    unittest.main()

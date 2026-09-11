from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / "cogs" / "tts" / "ui.py"
MODULO_PATH = ROOT / "cogs" / "tts" / "interface" / "visoes_base.py"


class TTSInterfaceVisoesBaseEstruturaTests(unittest.TestCase):
    def test_implementacao_canonicamente_em_portugues_foi_extraida(self):
        ui_tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        modulo_tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))

        classes_ui = {n.name for n in ui_tree.body if isinstance(n, ast.ClassDef)}
        classes_modulo = {n.name for n in modulo_tree.body if isinstance(n, ast.ClassDef)}
        funcoes_modulo = {n.name for n in modulo_tree.body if isinstance(n, ast.FunctionDef)}

        self.assertTrue({"VisaoBaseTTS", "VisaoSelecaoSimples"} <= classes_modulo)
        self.assertTrue({"dica_comando_painel_expirado", "mensagem_painel_expirado"} <= funcoes_modulo)
        self.assertNotIn("_BaseTTSView", classes_ui)
        self.assertNotIn("_SimpleSelectView", classes_ui)

    def test_ui_preserva_nomes_legados_por_alias_de_importacao(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        encontrados = {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module != "interface.visoes_base":
                continue
            for alias in node.names:
                encontrados[alias.asname or alias.name] = alias.name

        esperado = {
            "TTS_PANEL_EXPIRE_AFTER_SECONDS": "DURACAO_EXPIRACAO_PAINEL_TTS",
            "TTS_PANEL_DISPATCH_TIMEOUT_SECONDS": "DURACAO_DESPACHO_PAINEL_TTS",
            "TTS_EXPIRED_EMOJI": "EMOJI_PAINEL_TTS_EXPIRADO",
            "_fallback_panel_command_hint": "dica_comando_painel_expirado",
            "_fallback_expired_panel_message": "mensagem_painel_expirado",
            "_BaseTTSView": "VisaoBaseTTS",
            "_SimpleSelectView": "VisaoSelecaoSimples",
        }
        self.assertEqual(encontrados, esperado)

    def test_modulo_novo_nao_depende_de_audio_worker_termux_ou_apk(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "worker", "termux", "android", "streaming", "routing"):
            self.assertNotIn(proibido, joined)


class _View:
    def __init__(self, *, timeout=None, **kwargs):
        self.timeout = timeout
        self.children = []

    def add_item(self, item):
        self.children.append(item)
        item.view = self


class _Select:
    def __init__(self):
        self.view = None


class _Interaction:
    pass


def _carregar_modulo(*, prefixo="_"):
    discord = types.ModuleType("discord")
    discord.Message = type("Message", (), {})
    discord.Interaction = _Interaction
    discord.ui = types.SimpleNamespace(View=_View, Select=_Select)

    config = types.ModuleType("config")
    config.BOT_PREFIX = prefixo
    config.PREFIX = "?"

    spec = importlib.util.spec_from_file_location("_tts_visoes_base_teste", MODULO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(sys.modules, {"discord": discord, "config": config}):
        spec.loader.exec_module(module)
    return module


class TTSInterfaceVisoesBaseComportamentoTests(unittest.IsolatedAsyncioTestCase):
    def test_dicas_e_mensagem_expirada_preservam_contrato(self):
        modulo = _carregar_modulo(prefixo="!")
        casos = {
            "launcher": "`!tts`",
            "user": "`!tts`",
            "server": "`!panel_server`",
            "toggle": "`!toggle_panel`",
            "desconhecido": "`!tts`",
        }
        for tipo, esperado in casos.items():
            with self.subTest(tipo=tipo):
                self.assertEqual(modulo.dica_comando_painel_expirado(tipo), esperado)

        mensagem = modulo.mensagem_painel_expirado("server")
        self.assertIn("<:osaka:1539137127852539944>| Essa interação expirou", mensagem)
        self.assertIn("`!panel_server` novamente para usar esse botão", mensagem)

    async def test_visao_base_preserva_timeout_estado_e_autorizacao(self):
        modulo = _carregar_modulo()
        cog = types.SimpleNamespace(_make_embed=lambda *args, **kwargs: (args, kwargs))
        with patch.object(modulo.time, "monotonic", return_value=100.0):
            view = modulo.VisaoBaseTTS(
                cog,
                123,
                456,
                timeout=30,
                target_user_id=789,
                target_user_name="Pessoa",
            )

        self.assertEqual(view.timeout, 86400.0)
        self.assertEqual(view.owner_id, 123)
        self.assertEqual(view.guild_id, 456)
        self.assertEqual(view.target_user_id, 789)
        self.assertEqual(view.target_user_name, "Pessoa")
        self.assertEqual(view.expires_at_monotonic, 130.0)

        interaction = types.SimpleNamespace(
            user=types.SimpleNamespace(id=123),
            response=types.SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False),
        )
        with patch.object(view, "_is_expired", return_value=False):
            self.assertTrue(await view.interaction_check(interaction))
        interaction.response.send_message.assert_not_awaited()

        interaction.user.id = 999
        with patch.object(view, "_is_expired", return_value=False):
            self.assertFalse(await view.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()

    async def test_visao_base_preserva_fallback_de_expiracao(self):
        modulo = _carregar_modulo(prefixo="$")
        cog = types.SimpleNamespace(
            _build_expired_panel_message=AsyncMock(side_effect=RuntimeError("falha")),
            _make_embed=lambda *args, **kwargs: (args, kwargs),
        )
        view = modulo.VisaoBaseTTS(cog, 1, 2)
        view.panel_kind = "toggle"
        response = types.SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False)
        interaction = types.SimpleNamespace(user=types.SimpleNamespace(id=1), response=response)

        with patch.object(view, "_is_expired", return_value=True):
            self.assertFalse(await view.interaction_check(interaction))

        cog._build_expired_panel_message.assert_awaited_once_with(2, "toggle")
        args, kwargs = response.send_message.await_args
        self.assertIn("`$toggle_panel`", args[0])
        self.assertTrue(kwargs["ephemeral"])

    async def test_visao_selecao_simples_preserva_contexto_e_envio(self):
        modulo = _carregar_modulo()
        cog = types.SimpleNamespace(_make_embed=lambda *args, **kwargs: (args, kwargs))
        seletor = _Select()
        painel_origem = object()
        view = modulo.VisaoSelecaoSimples(
            cog,
            10,
            20,
            "Título",
            "Descrição",
            seletor,
            source_panel_message=painel_origem,
            target_user_id=30,
            target_user_name="Alvo",
        )
        self.assertEqual(seletor.guild_id, 20)
        self.assertEqual(seletor.owner_id, 10)
        self.assertEqual(seletor.target_user_id, 30)
        self.assertEqual(seletor.target_user_name, "Alvo")
        self.assertIs(seletor.view, view)

        resposta_original = object()
        response = types.SimpleNamespace(send_message=AsyncMock(), is_done=lambda: False)
        interaction = types.SimpleNamespace(
            message=object(),
            response=response,
            original_response=AsyncMock(return_value=resposta_original),
        )
        await view.send(interaction)
        response.send_message.assert_awaited_once()
        self.assertIs(view.message, resposta_original)
        self.assertIs(view.source_panel_message, painel_origem)


if __name__ == "__main__":
    unittest.main()

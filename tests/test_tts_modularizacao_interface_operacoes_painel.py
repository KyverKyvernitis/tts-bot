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
MODULO_PATH = ROOT / "cogs" / "tts" / "interface" / "operacoes_painel.py"

FUNCOES_CANONICAS = {
    "salvar_atualizacoes_modal_tts",
    "enviar_modal_configuracao_com_fallback",
    "reiniciar_selecao_lancador_publico",
}

ALIASES_LEGADOS = {
    "TTS_LAUNCHER_DESCRIPTION": "DESCRICAO_LANCADOR_TTS",
    "_save_tts_modal_updates": "salvar_atualizacoes_modal_tts",
    "_send_settings_modal_with_fallback": "enviar_modal_configuracao_com_fallback",
    "_reset_public_launcher_select": "reiniciar_selecao_lancador_publico",
}


class TTSInterfaceOperacoesPainelEstruturaTests(unittest.TestCase):
    def test_implementacoes_foram_extraidas_com_nomes_em_portugues(self):
        ui_tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        modulo_tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        funcoes_ui = {
            node.name
            for node in ui_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        funcoes_modulo = {
            node.name
            for node in modulo_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertTrue(FUNCOES_CANONICAS <= funcoes_modulo)
        self.assertTrue(
            {"_save_tts_modal_updates", "_send_settings_modal_with_fallback", "_reset_public_launcher_select"}.isdisjoint(funcoes_ui)
        )

    def test_ui_preserva_contratos_legados_por_alias(self):
        tree = ast.parse(UI_PATH.read_text(encoding="utf-8"))
        encontrados: dict[str, str] = {}
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or node.module != "interface.operacoes_painel":
                continue
            for alias in node.names:
                encontrados[str(alias.asname or alias.name)] = alias.name
        self.assertEqual(encontrados, ALIASES_LEGADOS)

    def test_modulo_nao_importa_audio_worker_termux_apk_streaming_ou_rede(self):
        tree = ast.parse(MODULO_PATH.read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        joined = "\n".join(imports).lower()
        for proibido in ("audio", "worker", "termux", "android", "apk", "streaming", "urllib", "http"):
            self.assertNotIn(proibido, joined)

    def test_assinaturas_legadas_continuam_compativeis(self):
        modulo = _carregar_modulo()
        self.assertEqual(
            list(inspect.signature(modulo.salvar_atualizacoes_modal_tts).parameters),
            [
                "cog", "interaction", "source_panel_message", "server", "updates",
                "success_title", "success_description", "target_user_id", "target_user_name",
            ],
        )
        self.assertEqual(
            list(inspect.signature(modulo.enviar_modal_configuracao_com_fallback).parameters),
            ["interaction", "guided_factory", "fallback_factory", "context"],
        )
        self.assertEqual(
            list(inspect.signature(modulo.reiniciar_selecao_lancador_publico).parameters),
            ["interaction", "panel"],
        )


class _AllowedMentions:
    @staticmethod
    def none():
        return "none"


def _carregar_modulo():
    discord = types.ModuleType("discord")
    discord.Interaction = type("Interaction", (), {})
    discord.Message = type("Message", (), {})
    discord.AllowedMentions = _AllowedMentions

    cogs_pkg = types.ModuleType("cogs")
    cogs_pkg.__path__ = []
    tts_pkg = types.ModuleType("cogs.tts")
    tts_pkg.__path__ = []
    interface_pkg = types.ModuleType("cogs.tts.interface")
    interface_pkg.__path__ = []

    spec = importlib.util.spec_from_file_location("cogs.tts.interface.operacoes_painel", MODULO_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    with patch.dict(
        sys.modules,
        {
            "discord": discord,
            "cogs": cogs_pkg,
            "cogs.tts": tts_pkg,
            "cogs.tts.interface": interface_pkg,
        },
    ):
        spec.loader.exec_module(module)
    return module


def _interaction(*, guild=True, kick_members=True, user_id=10):
    response = types.SimpleNamespace(
        send_message=AsyncMock(),
        send_modal=AsyncMock(),
        is_done=lambda: False,
    )
    followup = types.SimpleNamespace(send=AsyncMock())
    return types.SimpleNamespace(
        guild=types.SimpleNamespace(id=20) if guild else None,
        user=types.SimpleNamespace(
            id=user_id,
            guild_permissions=types.SimpleNamespace(kick_members=kick_members),
        ),
        response=response,
        followup=followup,
        message=None,
    )


class TTSInterfaceOperacoesPainelComportamentoTests(unittest.IsolatedAsyncioTestCase):
    async def test_salvar_rejeita_contexto_sem_servidor(self):
        modulo = _carregar_modulo()
        interaction = _interaction(guild=False)
        cog = types.SimpleNamespace(_make_embed=Mock(return_value="embed"))
        await modulo.salvar_atualizacoes_modal_tts(
            cog,
            interaction,
            source_panel_message=None,
            server=False,
            updates={"voice": "x"},
            success_title="ok",
            success_description="ok",
        )
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_salvar_rejeita_configuracao_servidor_sem_permissao(self):
        modulo = _carregar_modulo()
        interaction = _interaction(kick_members=False)
        cog = types.SimpleNamespace(_make_embed=Mock(return_value="embed"))
        await modulo.salvar_atualizacoes_modal_tts(
            cog,
            interaction,
            source_panel_message=None,
            server=True,
            updates={"edge_prefix": ","},
            success_title="ok",
            success_description="ok",
        )
        interaction.response.send_message.assert_awaited_once()
        cog._make_embed.assert_called_once()
        self.assertEqual(cog._make_embed.call_args.args[0], "Sem permissão")

    async def test_salvar_launcher_preserva_owner_e_anuncio_servidor(self):
        modulo = _carregar_modulo()
        interaction = _interaction()
        panel_message = object()
        view = types.SimpleNamespace(message=None)
        db = types.SimpleNamespace(set_guild_tts_defaults=Mock(return_value=None))
        cog = types.SimpleNamespace(
            _get_db=lambda: db,
            _make_embed=Mock(return_value="embed"),
            _resolve_public_panel_message=lambda interaction, source: (panel_message, 777),
            _resolve_panel_target_user=lambda interaction, **kwargs: (interaction.user.id, "Pessoa", True),
            _maybe_await=AsyncMock(),
            _public_panel_states={777: {"panel_kind": "launcher", "owner_id": 321}},
            _build_public_tts_launcher_view=Mock(return_value=view),
            _panel_update_after_change=AsyncMock(),
            _announce_panel_change=AsyncMock(),
        )
        await modulo.salvar_atualizacoes_modal_tts(
            cog,
            interaction,
            source_panel_message=panel_message,
            server=True,
            updates={"edge_prefix": ",", "ignore": None},
            success_title="Salvo",
            success_description="Feito",
        )
        cog._build_public_tts_launcher_view.assert_called_once_with(20, owner_id=321, timeout=300)
        self.assertIs(view.message, panel_message)
        cog._panel_update_after_change.assert_awaited_once()
        cog._announce_panel_change.assert_awaited_once()
        self.assertEqual(cog._maybe_await.await_args.args[0], None)
        db.set_guild_tts_defaults.assert_called_once_with(20, edge_prefix=",")

    async def test_salvar_usuario_sem_painel_preserva_destino_e_notice(self):
        modulo = _carregar_modulo()
        interaction = _interaction(user_id=11)
        db = object()
        cog = types.SimpleNamespace(
            _get_db=lambda: db,
            _make_embed=Mock(return_value="embed"),
            _resolve_public_panel_message=lambda interaction, source: (None, 0),
            _resolve_panel_target_user=lambda interaction, **kwargs: (99, "Alvo", False),
            _set_user_tts_and_refresh=AsyncMock(),
            _public_panel_states={},
            _send_tts_notice=AsyncMock(),
        )
        await modulo.salvar_atualizacoes_modal_tts(
            cog,
            interaction,
            source_panel_message=None,
            server=False,
            updates={"language": "pt-br"},
            success_title="Salvo",
            success_description="Feito",
            target_user_id=99,
            target_user_name="Alvo",
        )
        cog._set_user_tts_and_refresh.assert_awaited_once_with(20, 99, language="pt-br")
        cog._send_tts_notice.assert_awaited_once_with(
            interaction,
            title="Salvo",
            description="Feito",
            ok=True,
        )

    async def test_abertura_de_modal_usa_fallback_sem_consumir_contrato(self):
        modulo = _carregar_modulo()
        interaction = _interaction()
        guided = object()
        fallback = object()
        interaction.response.send_modal.side_effect = [RuntimeError("guiado"), None]
        with patch.object(modulo.traceback, "print_exception"):
            await modulo.enviar_modal_configuracao_com_fallback(
                interaction,
                lambda: guided,
                lambda: fallback,
                context="teste",
            )
        self.assertEqual(interaction.response.send_modal.await_count, 2)
        self.assertIs(interaction.response.send_modal.await_args_list[0].args[0], guided)
        self.assertIs(interaction.response.send_modal.await_args_list[1].args[0], fallback)

    async def test_abertura_de_modal_ja_respondida_avisa_no_followup(self):
        modulo = _carregar_modulo()
        interaction = _interaction()
        interaction.response.send_modal.side_effect = RuntimeError("guiado")
        interaction.response.is_done = lambda: True
        with patch.object(modulo.traceback, "print_exception"):
            await modulo.enviar_modal_configuracao_com_fallback(
                interaction,
                lambda: object(),
                lambda: object(),
                context="teste",
            )
        interaction.followup.send.assert_awaited_once_with(
            "Não consegui abrir esse formulário agora.",
            ephemeral=True,
        )
        self.assertEqual(interaction.response.send_modal.await_count, 1)

    async def test_reset_launcher_preserva_owner_e_payload(self):
        modulo = _carregar_modulo()
        message = types.SimpleNamespace(id=55)
        guild = types.SimpleNamespace(id=66)
        view = types.SimpleNamespace(message=None)
        cog = types.SimpleNamespace(
            _public_panel_states={55: {"panel_kind": "launcher", "owner_id": 77}},
            _build_public_tts_launcher_view=Mock(return_value=view),
            _edit_panel_message_payload=AsyncMock(),
            _make_embed=Mock(return_value="embed"),
        )
        interaction = types.SimpleNamespace(message=message, guild=guild)
        panel = types.SimpleNamespace(cog=cog)
        await modulo.reiniciar_selecao_lancador_publico(interaction, panel)
        cog._build_public_tts_launcher_view.assert_called_once_with(66, owner_id=77, timeout=300)
        self.assertIs(view.message, message)
        cog._edit_panel_message_payload.assert_awaited_once_with(message, embed="embed", view=view)

    async def test_reset_ignora_painel_que_nao_e_launcher(self):
        modulo = _carregar_modulo()
        message = types.SimpleNamespace(id=55)
        cog = types.SimpleNamespace(
            _public_panel_states={55: {"panel_kind": "user"}},
            _build_public_tts_launcher_view=Mock(),
        )
        interaction = types.SimpleNamespace(message=message, guild=types.SimpleNamespace(id=66))
        await modulo.reiniciar_selecao_lancador_publico(interaction, types.SimpleNamespace(cog=cog))
        cog._build_public_tts_launcher_view.assert_not_called()


if __name__ == "__main__":
    unittest.main()

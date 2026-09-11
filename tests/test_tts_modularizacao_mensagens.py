from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.tts.mensagens.despacho import (
    ResultadoDespachoMensagem,
    despachar_mensagem_tts,
)
from cogs.tts.mensagens.preparacao import (
    PayloadTTSMensagem,
    preparar_payload_tts_mensagem,
)
from cogs.tts.mensagens.renderizacao import (
    anexar_descricoes_tts,
    renderizar_texto_tts_mensagem,
)
from cogs.tts.mensagens.triagem import (
    DecisaoTriagemMensagem,
    analisar_mensagem_para_tts,
)
from cogs.tts.utils.message_dispatch import (
    MessageDispatchResult,
    dispatch_message_tts,
)
from cogs.tts.utils.message_gate import MessageGateDecision, analyze_message_for_tts
from cogs.tts.utils.message_payload import MessageTTSPayload, build_message_tts_payload
from cogs.tts.utils.message_render import append_tts_descriptions, render_message_tts_text


def test_tipos_legados_apontam_para_tipos_canonicos():
    assert MessageGateDecision is DecisaoTriagemMensagem
    assert MessageTTSPayload is PayloadTTSMensagem
    assert MessageDispatchResult is ResultadoDespachoMensagem


def test_funcoes_legadas_sem_wrapper_desnecessario_apontam_para_canonicas():
    assert analyze_message_for_tts is analisar_mensagem_para_tts
    assert build_message_tts_payload is preparar_payload_tts_mensagem
    assert append_tts_descriptions is anexar_descricoes_tts
    assert render_message_tts_text is renderizar_texto_tts_mensagem


def test_despacho_legado_e_canonico_continuam_disponiveis():
    # O despacho legado é propositalmente um wrapper: ele precisa preservar o
    # monkeypatch de build_message_tts_payload usado pelos testes/consumidores.
    assert callable(dispatch_message_tts)
    assert callable(despachar_mensagem_tts)
    assert dispatch_message_tts is not despachar_mensagem_tts


def test_cog_consume_pipeline_canonico_em_portugues():
    source = (ROOT / "cogs" / "tts" / "cog.py").read_text(encoding="utf-8")
    assert "from .mensagens.triagem import analisar_mensagem_para_tts" in source
    assert "from .mensagens.despacho import despachar_mensagem_tts" in source
    assert "from .mensagens.renderizacao import" in source
    assert ".utils.message_gate" not in source
    assert ".utils.message_dispatch" not in source
    assert ".utils.message_render" not in source


def test_modulos_canonicos_nao_exportam_funcoes_publicas_em_ingles():
    for filename in ("triagem.py", "preparacao.py", "renderizacao.py", "despacho.py"):
        tree = ast.parse((ROOT / "cogs" / "tts" / "mensagens" / filename).read_text(encoding="utf-8"))
        public_functions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_")
        }
        assert not {
            "analyze_message_for_tts",
            "build_message_tts_payload",
            "render_message_tts_text",
            "append_tts_descriptions",
            "dispatch_message_tts",
        } & public_functions

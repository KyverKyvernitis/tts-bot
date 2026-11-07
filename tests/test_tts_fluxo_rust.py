"""Regressões da fila de áudio TTS; não fazem chamadas ao Discord ou rede."""

from __future__ import annotations

import asyncio
import os
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_tts_helpers import tts_audio
from cogs.tts import fluxo_rust


class FluxoRustTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fim = object()
        sem_binario = self._testMethodName not in {
            "test_python_padrao_nao_importa_extensao", "test_fallback_quando_binario_ausente",
            "test_falha_nativa_desativa_proximas_filas",
        }
        if sem_binario:
            arquivo = (Path(fluxo_rust.__file__).resolve().parents[2]
                       / "rust/nucleo_fluxo_tts/vinculo_python/python/tts_fluxo_experimental"
                       / "_nucleo_fluxo_tts.abi3.so")
            if not (sys.platform == "linux" and platform.machine() == "x86_64" and arquivo.is_file()):
                self.skipTest("extensão da etapa 3 só está presente no Linux x86-64")

    def criar(self, *, blocos=2, bytes_bloco=1024):
        return fluxo_rust.FilaRust(
            maximo_blocos=blocos, bytes_por_bloco=bytes_bloco, sentinela=self.fim
        )

    def test_python_padrao_nao_importa_extensao(self):
        with patch.dict(os.environ, {"TTS_FLUXO_RUST": "0"}), patch.object(
            fluxo_rust, "_carregar_nucleo", side_effect=AssertionError("importou Rust")
        ):
            fila, mecanismo = fluxo_rust.criar_fila_streaming(
                maximo_blocos=2, bytes_por_bloco=1024, sentinela=self.fim
            )
        self.assertEqual(mecanismo, "python")
        self.assertIsInstance(fila, asyncio.Queue)
        self.assertEqual(fila.maxsize, 2)

    def test_fallback_quando_binario_ausente(self):
        with patch.dict(os.environ, {"TTS_FLUXO_RUST": "1"}), patch.object(
            fluxo_rust, "_RUST_FALHOU", False
        ), patch.object(
            fluxo_rust, "_carregar_nucleo", side_effect=ImportError("binário ausente")
        ):
            fila, mecanismo = fluxo_rust.criar_fila_streaming(
                maximo_blocos=2, bytes_por_bloco=1024, sentinela=self.fim
            )
            self.assertTrue(fluxo_rust._RUST_FALHOU)
        self.assertEqual(mecanismo, "python")
        self.assertIsInstance(fila, asyncio.Queue)

    def test_ativa_com_configuracao_e_binario_real(self):
        with patch.dict(os.environ, {"TTS_FLUXO_RUST": "1"}):
            fila, mecanismo = fluxo_rust.criar_fila_streaming(
                maximo_blocos=2, bytes_por_bloco=1024, sentinela=self.fim
            )
        self.assertEqual(mecanismo, "rust")
        self.assertIsInstance(fila, fluxo_rust.FilaRust)
        fila.cancelar()

    async def test_falha_nativa_desativa_proximas_filas(self):
        class ModuloFalho:
            class ErroTempoEsgotado(TimeoutError):
                pass
            class ErroFluxoCancelado(RuntimeError):
                pass
            class FluxoDeAudio:
                def __init__(self, *_):
                    self.cancelada = False
                def ler_bloco(self, _):
                    raise RuntimeError("erro simulado do núcleo")
                def cancelar(self):
                    self.cancelada = True

        with patch.object(fluxo_rust, "_RUST_FALHOU", False), patch.dict(
            os.environ, {"TTS_FLUXO_RUST": "1"}
        ), patch.object(fluxo_rust, "_carregar_nucleo", return_value=ModuloFalho):
            fila = fluxo_rust.FilaRust(maximo_blocos=2, bytes_por_bloco=128,
                                      sentinela=self.fim)
            with self.assertRaises(fluxo_rust.FalhaFilaRust):
                fila.get_nowait()
            self.assertTrue(fila._fluxo.cancelada)
            self.assertTrue(fluxo_rust._RUST_FALHOU)
            outra, mecanismo = fluxo_rust.criar_fila_streaming(
                maximo_blocos=2, bytes_por_bloco=128, sentinela=self.fim
            )
            self.assertEqual(mecanismo, "python")
            self.assertIsInstance(outra, asyncio.Queue)

    async def test_sem_dados_get_nowait_equivale_queue_empty(self):
        fila = self.criar()
        with self.assertRaises(asyncio.QueueEmpty):
            fila.get_nowait()
        await fila.put(b"abc")
        self.assertEqual(fila.get_nowait(), b"abc")
        fila.task_done()
        await fila.put(self.fim)
        self.assertIs(fila.get_nowait(), self.fim)
        self.assertIs(fila.get_nowait(), self.fim)

    async def test_concorrencia_preserva_ordem_integridade_e_eof(self):
        fila = self.criar(blocos=3, bytes_bloco=1024)
        blocos = [bytes([indice % 251]) * (64 + indice % 900) for indice in range(200)]
        recebidos = []

        async def produtor():
            for bloco in blocos:
                await fila.put(memoryview(bloco))
            await fila.put(self.fim)

        async def consumidor():
            while True:
                bloco = await fila.get()
                if bloco is self.fim:
                    return
                recebidos.append(bloco)
                fila.task_done()

        await asyncio.wait_for(asyncio.gather(produtor(), consumidor()), 8)
        self.assertEqual(recebidos, blocos)
        estatisticas = fila.estatisticas()
        self.assertEqual(estatisticas["bytes_escritos"], sum(map(len, blocos)))
        self.assertEqual(estatisticas["bytes_lidos"], sum(map(len, blocos)))
        self.assertEqual(estatisticas["bytes_em_fila"], 0)
        self.assertEqual(estatisticas["estado"], "finalizado")
        self.assertLessEqual(estatisticas["maior_ocupacao_bytes"], 3 * 1024)

    async def test_cancelamento_desbloqueia_consumidor(self):
        fila = self.criar()
        tarefa = asyncio.create_task(fila.get())
        await asyncio.sleep(.03)
        tarefa.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(tarefa, 2)
        self.assertEqual(fila.estatisticas()["estado"], "cancelado")

    async def test_cancelamento_desbloqueia_produtor_com_fila_cheia(self):
        fila = self.criar(blocos=1, bytes_bloco=4)
        await fila.put(b"1234")
        tarefa = asyncio.create_task(fila.put(b"5678"))
        await asyncio.sleep(.03)
        self.assertFalse(tarefa.done())
        tarefa.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(tarefa, 2)
        self.assertEqual(fila.estatisticas()["estado"], "cancelado")
        self.assertEqual(fila.estatisticas()["bytes_descartados"], 4)

    async def test_fim_do_fluxo_nao_bloqueia_fila_cheia(self):
        fila = self.criar(blocos=1, bytes_bloco=8)
        await fila.put(b"12345678")
        await asyncio.wait_for(fila.put(self.fim), .25)
        self.assertEqual(fila.get_nowait(), b"12345678")
        self.assertIs(fila.get_nowait(), self.fim)

    async def test_dois_fluxos_isolados(self):
        primeiro, segundo = self.criar(), self.criar()
        await primeiro.put(b"A")
        await segundo.put(b"B")
        primeiro.cancelar()
        self.assertEqual(await segundo.get(), b"B")
        await segundo.put(self.fim)
        self.assertIs(await segundo.get(), self.fim)

    async def test_fifo_real_preserva_bytes_e_finaliza(self):
        """Usa o writer original em uma FIFO real, com módulo Rust."""
        bloco = b"ID3" + os.urandom(8192)
        fila = fluxo_rust.FilaRust(maximo_blocos=2, bytes_por_bloco=16384,
                                  sentinela=tts_audio._TTS_STREAM_END)
        with tempfile.TemporaryDirectory() as diretorio:
            caminho = str(Path(diretorio) / "teste.fifo")
            os.mkfifo(caminho)
            handle = SimpleNamespace(
                fifo_path=caminho, consumer_abandoned=False,
                producer_done=asyncio.Event(), writer_ready=asyncio.Event(),
                queue=fila, producer_task=None, pipe_error=None,
                item=SimpleNamespace(guild_id=1),
            )

            async def leitura():
                def coletar():
                    with open(caminho, "rb", buffering=0) as leitor:
                        return leitor.read()
                return await asyncio.to_thread(coletar)

            consumidor = asyncio.create_task(leitura())
            escritor = asyncio.create_task(
                tts_audio.TTSAudioMixin._edge_stream_pipe_writer(SimpleNamespace(), handle)
            )
            await asyncio.wait_for(handle.writer_ready.wait(), 3)
            await fila.put(bloco)
            handle.producer_done.set()
            await fila.put(tts_audio._TTS_STREAM_END)
            self.assertEqual(await asyncio.wait_for(consumidor, 4), bloco)
            await asyncio.wait_for(escritor, 4)
            self.assertIsNone(handle.pipe_error)

    async def test_signal_stream_end_preserva_semantica(self):
        fila = fluxo_rust.FilaRust(maximo_blocos=2, bytes_por_bloco=1024,
                                  sentinela=tts_audio._TTS_STREAM_END)
        handle = SimpleNamespace(queue=fila, consumer_abandoned=False,
                                 producer_done=asyncio.Event())
        await tts_audio.TTSAudioMixin._signal_stream_end(SimpleNamespace(), handle)
        self.assertTrue(handle.producer_done.is_set())
        self.assertIs(await fila.get(), tts_audio._TTS_STREAM_END)


if __name__ == "__main__":
    unittest.main()

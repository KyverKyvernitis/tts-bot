"""Testes do contrato asyncio sem precisar de Rust compilado."""

import asyncio
import threading
import unittest

from tts_fluxo_experimental import FluxoAssincrono


class SimuladorDeFluxo:
    def __init__(self, capacidade_bytes, maximo_blocos):
        self.capacidade_bytes = capacidade_bytes
        self.maximo_blocos = maximo_blocos
        self.cancelado = False
        self.finalizado = False
        self._blocos = []
        self._cond = threading.Condition()
        self.espera_iniciada = threading.Event()

    def escrever_bloco(self, dados, tempo_maximo_ms=0):
        with self._cond:
            if self.cancelado:
                raise RuntimeError("cancelado")
            if self.finalizado:
                raise RuntimeError("finalizado")
            self._blocos.append(dados)
            self._cond.notify_all()

    def ler_bloco(self, tempo_maximo_ms=0):
        with self._cond:
            self.espera_iniciada.set()
            self._cond.wait_for(
                lambda: self._blocos or self.finalizado or self.cancelado,
                timeout=tempo_maximo_ms / 1000,
            )
            if self.cancelado:
                raise RuntimeError("cancelado")
            if self._blocos:
                return self._blocos.pop(0)
            if self.finalizado:
                return None
            raise TimeoutError("espera encerrada")

    def finalizar(self):
        with self._cond:
            self.finalizado = True
            self._cond.notify_all()

    def cancelar(self):
        with self._cond:
            self.cancelado = True
            self._cond.notify_all()

    def estatisticas(self):
        return {"estado": "cancelado" if self.cancelado else "aberto"}


class TestesAdaptador(unittest.IsolatedAsyncioTestCase):
    async def test_produzir_consumir_sem_bloquear_loop(self):
        fluxo = FluxoAssincrono(_fabrica=SimuladorDeFluxo)
        await fluxo.escrever_bloco(b"abc")
        await asyncio.sleep(0)
        self.assertEqual(await fluxo.ler_bloco(), b"abc")
        fluxo.finalizar()
        self.assertIsNone(await fluxo.ler_bloco())

    async def test_cancelamento_da_corrotina_desbloqueia_thread(self):
        fluxo = FluxoAssincrono(_fabrica=SimuladorDeFluxo)
        tarefa = asyncio.create_task(fluxo.ler_bloco(30_000))
        # Espera que o worker thread tenha iniciado a leitura.
        iniciou = await asyncio.to_thread(fluxo._fluxo.espera_iniciada.wait, 2)
        self.assertTrue(iniciou)
        tarefa.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarefa
        self.assertTrue(fluxo._fluxo.cancelado)

    async def test_cancelamento_de_produtor_propagado(self):
        classe = SimuladorDeFluxo

        class EscritorLento(classe):
            def escrever_bloco(self, dados, tempo_maximo_ms=0):
                self.espera_iniciada.set()
                with self._cond:
                    self._cond.wait_for(lambda: self.cancelado, timeout=5)
                if self.cancelado:
                    raise RuntimeError("cancelado")

        fluxo = FluxoAssincrono(_fabrica=EscritorLento)
        tarefa = asyncio.create_task(fluxo.escrever_bloco(b"dados", 30_000))
        iniciou = await asyncio.to_thread(fluxo._fluxo.espera_iniciada.wait, 2)
        self.assertTrue(iniciou)
        tarefa.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tarefa
        self.assertTrue(fluxo._fluxo.cancelado)

    async def test_sinais_do_fluxo(self):
        fluxo = FluxoAssincrono(_fabrica=SimuladorDeFluxo)
        self.assertEqual(fluxo.estatisticas()["estado"], "aberto")
        fluxo.cancelar()
        self.assertEqual(fluxo.estatisticas()["estado"], "cancelado")


if __name__ == "__main__":
    unittest.main()

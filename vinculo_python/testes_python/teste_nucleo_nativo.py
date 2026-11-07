"""Testes de integração reais, executáveis após maturin develop."""

import threading
import unittest

try:
    from tts_fluxo_experimental._nucleo_fluxo_tts import (
        FluxoDeAudio,
        ErroTempoEsgotado,
        ErroFluxoCancelado,
        ErroFluxoFinalizado,
    )
except ImportError:
    FluxoDeAudio = None


@unittest.skipUnless(FluxoDeAudio is not None, "módulo nativo não compilado")
class TestesNativos(unittest.TestCase):
    def test_ler_e_drenar_ate_eof(self):
        fluxo = FluxoDeAudio(10, 2)
        fluxo.escrever_bloco(b"abc")
        fluxo.escrever_bloco(b"de")
        fluxo.finalizar()
        self.assertEqual(fluxo.ler_bloco(), b"abc")
        self.assertEqual(fluxo.ler_bloco(), b"de")
        self.assertIsNone(fluxo.ler_bloco())
        self.assertEqual(fluxo.estatisticas()["bytes_lidos"], 5)
        with self.assertRaises(ErroFluxoFinalizado):
            fluxo.escrever_bloco(b"f")

    def test_limites_erro_sem_copia_infinita(self):
        fluxo = FluxoDeAudio(2, 1)
        with self.assertRaises(ValueError):
            fluxo.escrever_bloco(b"abc")
        fluxo.escrever_bloco(b"ab")
        with self.assertRaises(ErroTempoEsgotado):
            fluxo.escrever_bloco(b"c", 0)
        self.assertEqual(fluxo.estatisticas()["bytes_em_fila"], 2)
        with self.assertRaises(ValueError):
            fluxo.ler_bloco(3_600_001)

    def test_cancelamento_nao_gera_eof(self):
        fluxo = FluxoDeAudio(8, 2)
        fluxo.escrever_bloco(b"dados")
        fluxo.cancelar()
        fluxo.cancelar()
        with self.assertRaises(ErroFluxoCancelado):
            fluxo.ler_bloco()
        self.assertEqual(fluxo.estatisticas()["bytes_descartados"], 5)

    def test_leitura_bloqueada_nao_prende_gil(self):
        fluxo = FluxoDeAudio(8, 1)
        iniciar = threading.Event()
        resultado = []

        def leitor():
            iniciar.set()
            resultado.append(fluxo.ler_bloco(2_000))

        tarefa = threading.Thread(target=leitor)
        tarefa.start()
        self.assertTrue(iniciar.wait(1))
        # Se ler_bloco retiver o GIL, esta thread não consegue entrar.
        fluxo.escrever_bloco(b"ok", 500)
        tarefa.join(1)
        self.assertFalse(tarefa.is_alive())
        self.assertEqual(resultado, [b"ok"])

    def test_escrita_bloqueada_desperta_ao_consumir(self):
        fluxo = FluxoDeAudio(2, 1)
        fluxo.escrever_bloco(b"ab")
        pronto = threading.Event()
        resultado = []

        def produtor():
            pronto.set()
            fluxo.escrever_bloco(b"c", 2_000)
            resultado.append(True)

        tarefa = threading.Thread(target=produtor)
        tarefa.start()
        self.assertTrue(pronto.wait(1))
        self.assertEqual(fluxo.ler_bloco(500), b"ab")
        tarefa.join(1)
        self.assertFalse(tarefa.is_alive())
        self.assertEqual(fluxo.ler_bloco(), b"c")
        self.assertEqual(resultado, [True])

    def test_cancelar_desperta_leitura(self):
        fluxo = FluxoDeAudio(2, 1)
        pronto = threading.Event()
        erros = []

        def leitor():
            pronto.set()
            try:
                fluxo.ler_bloco(2_000)
            except ErroFluxoCancelado:
                erros.append("cancelado")

        tarefa = threading.Thread(target=leitor)
        tarefa.start()
        self.assertTrue(pronto.wait(1))
        fluxo.cancelar()
        tarefa.join(1)
        self.assertFalse(tarefa.is_alive())
        self.assertEqual(erros, ["cancelado"])

    def test_produtores_concorrentes_contagem(self):
        fluxo = FluxoDeAudio(1_024, 8)
        produtores = []
        erros = []

        def produzir(numero):
            try:
                for _ in range(100):
                    fluxo.escrever_bloco(bytes([numero]) * 32, 2_000)
            except Exception as exc:
                erros.append(exc)

        for numero in range(4):
            tarefa = threading.Thread(target=produzir, args=(numero,))
            tarefa.start()
            produtores.append(tarefa)

        total = 0
        for _ in range(400):
            total += len(fluxo.ler_bloco(2_000))
        for tarefa in produtores:
            tarefa.join(1)
            self.assertFalse(tarefa.is_alive())
        self.assertFalse(erros)
        fluxo.finalizar()
        self.assertIsNone(fluxo.ler_bloco())
        self.assertEqual(total, 12_800)
        self.assertEqual(fluxo.estatisticas()["bytes_em_fila"], 0)

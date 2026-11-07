"""Valida que a extensao carregada na VPS e a compilada para Linux x86-64."""
import sys
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform.startswith('linux'), 'destinado a VPS Linux')
class TestePlataforma(unittest.TestCase):
    def test_extensao_linux_abi3(self):
        import tts_fluxo_experimental._nucleo_fluxo_tts as nativo
        caminho = Path(nativo.__file__).resolve()
        self.assertEqual(caminho.name, '_nucleo_fluxo_tts.abi3.so')
        self.assertEqual(caminho.parent.name, 'tts_fluxo_experimental')

    def test_operacao_basica(self):
        from tts_fluxo_experimental._nucleo_fluxo_tts import FluxoDeAudio
        f = FluxoDeAudio(16, 3)
        f.escrever_bloco(b'ola')
        f.finalizar()
        self.assertEqual(f.ler_bloco(), b'ola')
        self.assertIsNone(f.ler_bloco())

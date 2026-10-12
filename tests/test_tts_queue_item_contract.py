"""Regressao do contrato da fila de TTS ao receber ttv_score.

Isola a dataclass real via AST para nao exigir Discord, rede ou motor de voz.
"""
from __future__ import annotations

import ast
import dataclasses
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _queue_item_class():
    path = ROOT / 'cogs/tts/audio.py'
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    classes = [
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == 'QueueItem'
    ]
    assert len(classes) == 1
    module = ast.Module(body=[
        ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0),
        classes[0],
    ], type_ignores=[])
    namespace = {
        '__name__': __name__,
        'dataclass': dataclasses.dataclass,
        'field': dataclasses.field,
        'time': time,
        'uuid': uuid,
        'DEFAULT_VOICE_ID': 'test-default-voice',
        'DEFAULT_LANGUAGE': 'test-default-language',
        'TTS_TETO_DEFAULT_PITCH_SEMITONES': 0,
    }
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)
    return namespace['QueueItem']


class QueueItemContractTests(unittest.TestCase):
    def setUp(self):
        self.QueueItem = _queue_item_class()
        self.base = dict(
            guild_id=1, channel_id=2, author_id=3, text='olá',
            engine='edge', voice='pt-BR-FranciscaNeural',
            language='pt', rate='+0%', pitch='+0Hz',
        )

    def test_edge_and_gtts_keep_default_score_without_worker(self):
        for engine in ('edge', 'gtts'):
            with self.subTest(engine=engine):
                item = self.QueueItem(**{**self.base, 'engine': engine})
                self.assertIsNone(item.ttv_score)
                self.assertEqual(item.text, 'olá')
                self.assertEqual(item.engine, engine)

    def test_ttv_score_keyword_is_accepted_and_preserved(self):
        for score in ('score de teste', b'dados', {'notes': []}):
            with self.subTest(type=type(score).__name__):
                item = self.QueueItem(**{**self.base, 'engine': 'teto'}, ttv_score=score)
                self.assertIs(item.ttv_score, score)

    def test_score_does_not_change_equality_or_expose_data_in_repr(self):
        left = self.QueueItem(**self.base, ttv_score={'private': 'payload'})
        right = self.QueueItem(**self.base)
        self.assertEqual(left, right)
        self.assertNotIn('private', repr(left))
        descriptor = self.QueueItem.__dataclass_fields__['ttv_score']
        self.assertFalse(descriptor.repr)
        self.assertFalse(descriptor.compare)


if __name__ == '__main__':
    unittest.main()

"""Regression: message preparation and QueueItem must share one constructor contract.

Uses AST to execute the real QueueItem class and the QueueItem(...) expression
from the message preparer. This avoids importing the full Discord/worker runtime
but still exercises the deployed constructor and actual argument expressions.
"""
from __future__ import annotations

import ast
import time
import types
import uuid
import unittest
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "cogs/tts/audio.py"
PREPARER = ROOT / "cogs/tts/mensagens/preparacao.py"


def _get_queue_item():
    tree = ast.parse(AUDIO.read_text(encoding="utf-8"), filename=str(AUDIO))
    declarations = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "QueueItem"]
    assert len(declarations) == 1, "expected exactly one QueueItem definition"
    module = ast.Module(body=declarations, type_ignores=[])
    ns = {
        "__name__": __name__, "dataclass": dataclass, "field": field,
        "time": time, "uuid": uuid, "Optional": Optional, "Any": Any,
        "Callable": Callable, "Awaitable": Awaitable,
        "DEFAULT_VOICE_ID": "kasane-teto", "DEFAULT_LANGUAGE": "pt-BR",
        "TTS_TETO_DEFAULT_PITCH_SEMITONES": 0.0,
    }
    exec(compile(module, str(AUDIO), "exec"), ns)
    return ns["QueueItem"]


def _get_preparation_expression():
    tree = ast.parse(PREPARER.read_text(encoding="utf-8"), filename=str(PREPARER))
    matches = [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "queue_item" for t in n.targets)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name) and n.value.func.id == "QueueItem"
    ]
    assert len(matches) == 1, "expected exactly one QueueItem construction in preparacao.py"
    return compile(ast.Expression(body=matches[0]), str(PREPARER), "eval")


class QueueItemMessageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.item_type = _get_queue_item()
        cls.preparation = _get_preparation_expression()

    def _prepared_item(self, engine: str, *, segments=None, score=None, digest=""):
        preferences = {
            "ttv_voice_id": "kasane-teto", "ttv_language": "pt-BR",
            "ttv_pitch_semitones": "+0.0", "ttv_speech_rate": 1.0,
        }
        resolved = {
            "engine": engine, "voice": "pt-BR-FranciscaNeural",
            "language": "pt-br", "rate": "+0%", "pitch": "+0Hz",
        }
        env = {
            "QueueItem": self.item_type,
            "message": types.SimpleNamespace(
                guild=types.SimpleNamespace(id=10),
                author=types.SimpleNamespace(id=20),
            ),
            "voice_channel": types.SimpleNamespace(id=30),
            "config": types.SimpleNamespace(TTS_PIPER_MODEL_NAME="turbo-default"),
            "text": "Teste de voz", "resolved": resolved,
            "ttv_preferences": preferences,
            "score": score, "segments": segments, "digest": digest,
            "fallback_engine": "gtts", "fallback_voice": "",
            "fallback_language": "pt", "fallback_rate": "+0%",
            "fallback_pitch": "+0Hz",
        }
        return eval(self.preparation, env)

    def test_all_named_message_arguments_are_declared_fields(self):
        tree = ast.parse(PREPARER.read_text(encoding="utf-8"))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "QueueItem"
        ]
        supported = {f.name for f in fields(self.item_type)}
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords if kw.arg is not None}
            self.assertFalse(kwargs - supported, kwargs - supported)
        self.assertTrue({"ttv_score", "ttv_segments", "ttv_performance_hash"} <= supported)

    def test_all_tts_call_sites_use_known_named_fields(self):
        supported = {f.name for f in fields(self.item_type)}
        checked = 0
        for path in (ROOT / "cogs/tts").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                is_queue = (
                    isinstance(node.func, ast.Name) and node.func.id == "QueueItem"
                ) or (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "QueueItem"
                )
                if not is_queue:
                    continue
                explicit = {kw.arg for kw in node.keywords if kw.arg is not None}
                self.assertFalse(
                    explicit - supported,
                    f"{path}:{node.lineno} unknown fields {explicit - supported}",
                )
                checked += 1
        self.assertGreater(checked, 0)

    def test_gtts_and_edge_messages_enter_queue_constructor(self):
        for engine in ("gtts", "edge"):
            with self.subTest(engine=engine):
                item = self._prepared_item(engine)
                self.assertEqual(item.engine, engine)
                self.assertEqual(item.channel_id, 30)
                self.assertIsNone(item.ttv_score)
                self.assertIsNone(item.ttv_segments)
                self.assertEqual(item.ttv_performance_hash, "")

    def test_ttv_metadata_and_dynamic_preferences_are_accepted(self):
        segments = [{"text": "Teste", "hold": False}]
        score = {"notes": [60, 62, 64]}
        item = self._prepared_item("teto", segments=segments, score=score, digest="hash-example")
        self.assertIs(item.ttv_score, score)
        self.assertIs(item.ttv_segments, segments)
        self.assertEqual(item.ttv_performance_hash, "hash-example")
        self.assertEqual(item.ttv_language, "pt-BR")
        self.assertEqual(item.ttv_voice_id, "kasane-teto")
        clone = replace(item, text="outro trecho")
        self.assertEqual(clone.ttv_performance_hash, item.ttv_performance_hash)
        self.assertIs(clone.ttv_segments, segments)

    def test_no_shared_mutable_default_for_segments(self):
        one = self._prepared_item("edge")
        two = self._prepared_item("gtts")
        self.assertIsNone(one.ttv_segments)
        self.assertIsNone(two.ttv_segments)


if __name__ == "__main__":
    unittest.main()

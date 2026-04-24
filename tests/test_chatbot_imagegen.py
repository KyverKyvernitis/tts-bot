from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cogs.chatbot.imagegen import classify_image_prompt, generate_image


class ImagegenRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_classify_image_prompt(self):
        self.assertEqual(classify_image_prompt("gere uma paisagem bonita"), "safe")
        self.assertEqual(classify_image_prompt("gere uma imagem nsfw"), "adult_allowed")
        self.assertEqual(
            classify_image_prompt("gere cena de menor de idade em conteúdo sexual"),
            "blocked",
        )

    async def test_adult_request_in_sfw_channel_is_blocked(self):
        async with aiohttp.ClientSession() as session:
            result = await generate_image(
                session,
                prompt="gere uma imagem nsfw",
                channel_is_nsfw=False,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "channel_not_nsfw")

    async def test_safe_request_missing_gemini_key(self):
        with patch.dict(os.environ, {}, clear=True):
            async with aiohttp.ClientSession() as session:
                result = await generate_image(
                    session,
                    prompt="gere uma montanha nevada",
                    channel_is_nsfw=True,
                )
        self.assertFalse(result.ok)
        self.assertEqual(result.provider, "gemini")
        self.assertEqual(result.reason, "missing_key")

    async def test_adult_request_missing_adult_provider_config(self):
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "x", "ADULT_IMAGEGEN_PROVIDER": "huggingface"},
            clear=True,
        ):
            async with aiohttp.ClientSession() as session:
                result = await generate_image(
                    session,
                    prompt="gere uma imagem erótica artística",
                    channel_is_nsfw=True,
                )
        self.assertFalse(result.ok)
        self.assertEqual(result.provider, "adult_hf")
        self.assertEqual(result.reason, "missing_key")

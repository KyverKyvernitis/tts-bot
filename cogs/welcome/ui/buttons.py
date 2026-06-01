from __future__ import annotations

import asyncio
import base64
import contextlib
import colorsys
import json
import os
from io import BytesIO
from pathlib import Path
import logging
import random
import re
import time
import urllib.error
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from typing import Any

import discord
from discord.ext import commands

try:
    from PIL import Image, ImageSequence
except Exception:  # pragma: no cover - fallback if Pillow is unavailable
    Image = None
    ImageSequence = None

from ..config.defaults import *
from ..core.helpers import *

log = logging.getLogger(__name__)

class _BackButton(discord.ui.Button):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(label="Voltar", emoji="↩️", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        self.panel.go_back()
        self.panel._rebuild()
        await interaction.response.edit_message(view=self.panel)


class _CloseButton(discord.ui.Button):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(label="Fechar", emoji="✖️", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        self.panel.stop()
        with contextlib.suppress(Exception):
            if not interaction.response.is_done():
                await interaction.response.defer()
        targets = [self.panel.message, self.panel.command_message]
        for message in targets:
            if message is None:
                continue
            with contextlib.suppress(discord.HTTPException, discord.NotFound, discord.Forbidden):
                await message.delete()


class _PreviewButton(discord.ui.Button):
    def __init__(self, panel: "WelcomeAdminView"):
        self.panel = panel
        super().__init__(label="Preview", emoji="👁️", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        await self.panel.send_preview(interaction)

__all__ = [
    "_BackButton",
    "_CloseButton",
    "_PreviewButton",
]

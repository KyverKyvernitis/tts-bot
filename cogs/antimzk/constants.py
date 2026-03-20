import re

import discord
from discord import app_commands

from config import GUILD_IDS


_GUILD_OBJECTS = [discord.Object(id=guild_id) for guild_id in GUILD_IDS]
_FOCUS_WORD_RE = re.compile(r"(?<!\w)focus(?!\w)", re.IGNORECASE)
_ROLE_TOGGLE_WORD_RE = re.compile(r"(?<!\w)pica(?!\w)", re.IGNORECASE)
_DJ_TOGGLE_WORD_RE = re.compile(r"(?<!\w)dj(?!\w)", re.IGNORECASE)
_ROLETA_WORD_RE = re.compile(r"(?<!\w)roleta(?!\w)", re.IGNORECASE)
_BUCKSHOT_WORD_RE = re.compile(r"(?<!\w)buckshot(?!\w)", re.IGNORECASE)
_ATIRAR_WORD_RE = re.compile(r"(?<!\w)atirar(?!\w)", re.IGNORECASE)
_RESPONSE_DELETE_AFTER = 20
_ROLE_TOGGLE_DELETE_AFTER = 5
_PICA_DURATION_SECONDS = 2 * 60 * 60
_DJ_DURATION_SECONDS = 6 * 60 * 60


def _guild_scoped():
    return app_commands.guilds(*_GUILD_OBJECTS) if _GUILD_OBJECTS else (lambda f: f)

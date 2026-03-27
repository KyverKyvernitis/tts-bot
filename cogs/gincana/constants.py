import re

import discord
from discord import app_commands

_FOCUS_WORD_RE = re.compile(r"(?<!\w)focus(?!\w)", re.IGNORECASE)
_ROLE_TOGGLE_WORD_RE = re.compile(r"^\s*pica\s*$", re.IGNORECASE)
_DJ_TOGGLE_WORD_RE = re.compile(r"^\s*dj\s*$", re.IGNORECASE)
_ROLETA_WORD_RE = re.compile(r"^\s*roleta\s*$", re.IGNORECASE)
_POKER_WORD_RE = re.compile(r"^\s*poker\s*$", re.IGNORECASE)
_BUCKSHOT_WORD_RE = re.compile(r"^\s*buckshot\s*$", re.IGNORECASE)
_ATIRAR_WORD_RE = re.compile(r"^\s*atirar\s*$", re.IGNORECASE)
_ALVO_WORD_RE = re.compile(r"^\s*alvo\s*$", re.IGNORECASE)
_RESPONSE_DELETE_AFTER = 20
_ROLE_TOGGLE_DELETE_AFTER = 5
_PICA_DURATION_SECONDS = 2 * 60 * 60
_DJ_DURATION_SECONDS = 6 * 60 * 60


def _guild_scoped():
    return lambda f: f


CHIPS_INITIAL = 100
CHIPS_DEFAULT = 100
CHIPS_RESET_HOURS = 12
CHIPS_RESET_SECONDS = CHIPS_RESET_HOURS * 60 * 60
CHIPS_RECHARGE_THRESHOLD = 15
ROLETA_COST = 15
ROLETA_JACKPOT_CHIPS = 100
BUCKSHOT_STAKE = 25
ALVO_STAKE = 10

POKER_BUY_IN = 15

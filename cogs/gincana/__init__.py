"""Pacote legado da cog de jogos.

A implementação ativa foi movida para ``cogs.games``.
Este pacote fica sem ``setup`` para evitar carga duplicada em instalações onde
o updater ainda não remove arquivos antigos do repositório local.
"""

ACTIVE_EXTENSION = "cogs.games"

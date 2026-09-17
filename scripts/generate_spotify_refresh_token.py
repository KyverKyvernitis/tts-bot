#!/usr/bin/env python3
"""Compatibilidade: a ferramenta Spotify agora pertence a cogs.musica."""

from cogs.musica.ferramentas.gerar_token_spotify import main


if __name__ == "__main__":
    raise SystemExit(main())

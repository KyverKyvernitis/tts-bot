#!/usr/bin/env python3
"""Gera SPOTIFY_REFRESH_TOKEN para o player de música.

Uso na VPS:
  cd /home/ubuntu/bot
  source .venv/bin/activate
  python scripts/generate_spotify_refresh_token.py

O script imprime um link de autorização. Abra no navegador/celular, autorize,
e cole de volta a URL final que ficou no navegador. Ele salva/atualiza
SPOTIFY_REFRESH_TOKEN no .env automaticamente.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
from pathlib import Path

SCOPES = "playlist-read-private playlist-read-collaborative user-read-private"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"


def load_env(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        data[key.strip()] = value
    return data


def upsert_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines() if path.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    missing = [key for key in updates if key not in seen]
    if missing and out and out[-1].strip():
        out.append("")
    for key in missing:
        out.append(f"{key}={updates[key]}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str) -> dict:
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    payload = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=payload,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - endpoint fixo Spotify
        return json.loads(resp.read().decode())


def main() -> int:
    root = Path.cwd()
    env_path = root / ".env"
    env = load_env(env_path)
    client_id = os.getenv("SPOTIFY_CLIENT_ID") or env.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET") or env.get("SPOTIFY_CLIENT_SECRET", "")
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI") or env.get("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT_URI)

    if not client_id or not client_secret:
        print("SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET não encontrados no .env.")
        return 1

    state = secrets.token_urlsafe(18)
    params = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "show_dialog": "true",
    })
    auth_url = f"https://accounts.spotify.com/authorize?{params}"

    print("\nAbra este link no navegador logado na conta Spotify que tem acesso à playlist:\n")
    print(auth_url)
    print("\nDepois de autorizar, o navegador vai tentar abrir 127.0.0.1 e talvez mostrar erro. Está tudo bem.")
    print("Copie a URL final completa da barra de endereço e cole aqui.")
    final_url = input("\nURL final: ").strip()
    parsed = urllib.parse.urlparse(final_url)
    qs = urllib.parse.parse_qs(parsed.query)
    code = (qs.get("code") or [""])[0]
    returned_state = (qs.get("state") or [""])[0]
    error = (qs.get("error") or [""])[0]

    if error:
        print(f"Spotify retornou erro: {error}")
        return 1
    if not code:
        print("Não encontrei ?code= na URL final colada.")
        return 1
    if returned_state and returned_state != state:
        print("Aviso: state retornado não bate. Vou continuar mesmo assim, mas confira se você colou a URL certa.")

    try:
        token_data = exchange_code(client_id, client_secret, redirect_uri, code)
    except Exception as exc:
        print(f"Falha ao trocar code por refresh token: {exc!r}")
        print("Confirme se SPOTIFY_REDIRECT_URI no .env é exatamente o mesmo cadastrado no dashboard.")
        return 1

    refresh = str(token_data.get("refresh_token") or "")
    access = str(token_data.get("access_token") or "")
    if not refresh:
        print("Spotify não retornou refresh_token. Tente rodar novamente com show_dialog=true ou remova acesso antigo do app na conta Spotify.")
        return 1

    upsert_env(env_path, {
        "SPOTIFY_REFRESH_TOKEN": refresh,
        "SPOTIFY_REDIRECT_URI": redirect_uri,
        "SPOTIFY_MARKET": env.get("SPOTIFY_MARKET", "BR") or "BR",
    })
    print("\nSPOTIFY_REFRESH_TOKEN salvo no .env com sucesso.")
    print("Access token gerado:", bool(access))
    print("Agora rode: sudo systemctl restart tts-bot.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any


_DURATION_RE = re.compile(r"^(?:(\d{1,2}):)?(\d{1,2}):(\d{2})$")


def _spotify_track_url_from_attrs(attrs) -> str:
    """Extrai um link público de faixa Spotify sem depender de classes CSS."""
    for raw_name, raw_value in attrs or ():
        name = str(raw_name or "").strip().lower()
        if name not in {"href", "data-uri", "data-track-uri", "data-testid-uri", "uri"}:
            continue
        value = html.unescape(str(raw_value or "")).strip()
        if not value:
            continue
        if value.startswith("spotify:track:"):
            item_id = value.rsplit(":", 1)[-1].strip()
            if re.fullmatch(r"[A-Za-z0-9]{16,32}", item_id):
                return f"https://open.spotify.com/track/{item_id}"
        match = re.search(r"(?:https?://open\.spotify\.com)?/track/([A-Za-z0-9]{16,32})", value)
        if match:
            return f"https://open.spotify.com/track/{match.group(1)}"
    return ""

_JSON_SCRIPT_RE = re.compile(
    r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_RE = re.compile(r"<meta\s+([^>]+)>", re.IGNORECASE | re.DOTALL)
_ATTR_RE = re.compile(r"([:\w-]+)\s*=\s*([\"'])(.*?)\2", re.IGNORECASE | re.DOTALL)


@dataclass(slots=True)
class SpotifyEmbedRow:
    title: str
    artist: str
    duration: float | None = None
    webpage_url: str = ""


@dataclass(slots=True)
class SpotifyEmbedDocument:
    title: str = ""
    subtitle: str = ""
    thumbnail: str = ""
    rows: list[SpotifyEmbedRow] | None = None

    def __post_init__(self) -> None:
        if self.rows is None:
            self.rows = []


def _clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value


def parse_duration_label(value: str) -> float | None:
    match = _DURATION_RE.fullmatch(_clean_text(value))
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    if minutes >= 60 and hours:
        return None
    if seconds >= 60:
        return None
    return float(hours * 3600 + minutes * 60 + seconds)


def html_title(content: str) -> str:
    match = _TITLE_RE.search(content or "")
    if not match:
        return ""
    title = _clean_text(re.sub(r"<[^>]+>", " ", match.group(1)))
    return re.sub(r"\s*[|·-]\s*Spotify\s*$", "", title, flags=re.IGNORECASE).strip()


def html_meta(content: str, *keys: str) -> str:
    wanted = {key.lower() for key in keys if key}
    if not wanted:
        return ""
    for match in _META_RE.finditer(content or ""):
        attrs = {name.lower(): html.unescape(value) for name, _, value in _ATTR_RE.findall(match.group(1))}
        marker = (attrs.get("property") or attrs.get("name") or "").lower()
        if marker in wanted:
            return _clean_text(attrs.get("content") or "")
    return ""


def json_script_blobs(content: str) -> list[Any]:
    """Extrai JSON de scripts públicos sem depender de um único id/framework.

    Spotify alterna entre JSON-LD, __NEXT_DATA__ e payloads application/json.
    Aceitamos apenas scripts que parseiem como JSON completo; strings JS livres
    não são executadas nem avaliadas.
    """

    blobs: list[Any] = []
    for match in _JSON_SCRIPT_RE.finditer(content or ""):
        attrs = {name.lower(): value for name, _, value in _ATTR_RE.findall(match.group("attrs") or "")}
        script_type = (attrs.get("type") or "").lower()
        script_id = (attrs.get("id") or "").lower()
        if script_type not in {"application/ld+json", "application/json"} and script_id != "__next_data__":
            continue
        body = html.unescape((match.group("body") or "").strip())
        if not body:
            continue
        try:
            blobs.append(json.loads(body))
        except Exception:
            continue
    return blobs


class _StopSpotifyParse(Exception):
    """Sentinela interna para interromper o HTMLParser ao completar a janela."""


class _SpotifyEmbedHTMLParser(HTMLParser):
    """Parser tolerante do HTML server-rendered do embed.

    A UI pública atualmente expõe a faixa em headings: h3 = título e h4 =
    artista nas coleções; track individual usa headings superiores. O parser
    ignora classes/ids deliberadamente para sobreviver a renomes de CSS.
    """

    def __init__(self, *, limit: int, offset: int = 0) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = max(1, int(limit))
        self.row_offset = max(0, int(offset))
        self._seen_rows = 0
        self._tag_stack: list[str] = []
        self._track_url_stack: list[str] = []
        self._heading_tag = ""
        self._heading_parts: list[str] = []
        self._heading_track_url = ""
        self._pending_title = ""
        self._pending_artist = ""
        self._pending_track_url = ""
        self._top_headings: list[tuple[str, str]] = []
        self._done = False
        self.rows: list[SpotifyEmbedRow] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if self._done:
            return
        tag = tag.lower()
        track_url = _spotify_track_url_from_attrs(attrs)
        self._tag_stack.append(tag)
        self._track_url_stack.append(track_url)
        if self._heading_tag and track_url and not self._heading_track_url:
            # Alguns layouts colocam o <a href=/track/...> dentro do h3.
            self._heading_track_url = track_url
        if tag in {"h1", "h2", "h3", "h4"}:
            self._heading_tag = tag
            self._heading_parts = []
            self._heading_track_url = next((value for value in reversed(self._track_url_stack) if value), "")

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if self._done:
            return
        tag = tag.lower()
        if self._heading_tag == tag:
            parts = list(self._heading_parts)
            # O embed pode renderizar o selo de conteúdo explícito como um
            # fragmento textual separado ("E") dentro do h4. Ele não faz parte
            # do nome do artista; se o juntarmos, vira "E Cavetown" e polui
            # tanto o painel quanto a busca direct-play.
            if tag == "h4" and len(parts) > 1:
                parts = [part for part in parts if part.strip().casefold() not in {"e", "explicit"}]
            value = _clean_text(" ".join(parts))
            if value:
                # Só h1/h2 são necessários para o cabeçalho. Guardar todos os
                # h3/h4 de playlists enormes faria a memória crescer com o
                # tamanho total da página mesmo quando pedimos uma janela curta.
                if tag in {"h1", "h2"}:
                    self._top_headings.append((tag, value))
                if tag == "h3" and len(self.rows) < self.limit:
                    self._flush_pending_without_duration()
                    if self._done:
                        return
                    self._pending_title = value
                    self._pending_artist = ""
                    self._pending_track_url = self._heading_track_url
                elif tag == "h4" and self._pending_title and not self._pending_artist:
                    self._pending_artist = value
            self._heading_tag = ""
            self._heading_parts = []
            self._heading_track_url = ""
        if self._tag_stack:
            # HTML pode ser imperfeito; remove a ocorrência mais interna.
            for index in range(len(self._tag_stack) - 1, -1, -1):
                if self._tag_stack[index] == tag:
                    del self._tag_stack[index:]
                    del self._track_url_stack[index:]
                    break

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._done:
            return
        value = _clean_text(data)
        if not value:
            return
        if self._heading_tag:
            self._heading_parts.append(value)
            return
        if self._pending_title and self._pending_artist and len(self.rows) < self.limit:
            duration = parse_duration_label(value)
            if duration is not None:
                self._append_row(SpotifyEmbedRow(self._pending_title, self._pending_artist, duration, self._pending_track_url))
                self._pending_title = ""
                self._pending_artist = ""
                self._pending_track_url = ""

    def _append_row(self, row: SpotifyEmbedRow) -> None:
        index = self._seen_rows
        self._seen_rows += 1
        if index >= self.row_offset and len(self.rows) < self.limit:
            self.rows.append(row)
        # O caller pede ``limit + 1`` quando precisa detectar continuação.
        # Depois de alcançar offset + janela não há motivo para percorrer o
        # restante de uma playlist potencialmente gigantesca.
        if self._seen_rows >= self.row_offset + self.limit:
            self._done = True
            # ``HTMLParser.feed`` continuaria tokenizando o documento inteiro
            # mesmo com callbacks em no-op. Abortar aqui mantém CPU proporcional
            # à janela solicitada quando o HTML público contém milhares de faixas.
            raise _StopSpotifyParse

    def _flush_pending_without_duration(self) -> None:
        if self._pending_title and self._pending_artist:
            self._append_row(SpotifyEmbedRow(self._pending_title, self._pending_artist, None, self._pending_track_url))
        self._pending_title = ""
        self._pending_artist = ""
        self._pending_track_url = ""

    def finish(self) -> SpotifyEmbedDocument:
        if not self._done:
            self._flush_pending_without_duration()
        h1 = next((value for tag, value in self._top_headings if tag == "h1"), "")
        h2 = next((value for tag, value in self._top_headings if tag == "h2"), "")
        return SpotifyEmbedDocument(title=h1, subtitle=h2, rows=self.rows[: self.limit])


def parse_embed_document(content: str, *, limit: int = 100, offset: int = 0) -> SpotifyEmbedDocument:
    parser = _SpotifyEmbedHTMLParser(limit=limit, offset=offset)
    try:
        parser.feed(content or "")
        parser.close()
    except _StopSpotifyParse:
        # Janela completa: interrupção intencional para não tokenizar o restante.
        pass
    except Exception:
        # HTMLParser é tolerante, mas payload truncado ainda não deve quebrar o
        # resolver; devolvemos o que já foi coletado.
        pass
    document = parser.finish()
    if not document.title:
        document.title = html_title(content)
    document.thumbnail = html_meta(content, "og:image", "twitter:image")
    return document


class _SpotifyEmbedRowCounter(HTMLParser):
    """Conta linhas de faixa sem construir objetos para a playlist inteira."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._heading = ""
        self._parts: list[str] = []
        self._pending_title = False
        self.count = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        tag = str(tag or "").lower()
        if tag in {"h3", "h4"}:
            self._heading = tag
            self._parts = []

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._heading:
            value = _clean_text(data)
            if value:
                self._parts.append(value)

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        tag = str(tag or "").lower()
        if tag != self._heading:
            return
        value = _clean_text(" ".join(self._parts))
        if tag == "h3":
            self._pending_title = bool(value)
        elif tag == "h4" and self._pending_title and value:
            self.count += 1
            self._pending_title = False
        self._heading = ""
        self._parts = []


def count_embed_rows(content: str) -> int:
    """Conta faixas server-rendered em O(n) e memória O(1).

    Serve apenas como fallback para total da playlist quando o HTML não publica
    ``totalCount``. Não cria candidatos, thumbnails ou listas de milhares de
    itens; portanto não desfaz o modelo de playlist virtual.
    """
    parser = _SpotifyEmbedRowCounter()
    try:
        parser.feed(content or "")
        parser.close()
    except Exception:
        pass
    return max(0, int(parser.count))

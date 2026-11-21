from __future__ import annotations

from dataclasses import dataclass
import difflib
import json
from pathlib import Path
import re
from typing import Literal
import unicodedata
from collections.abc import Callable
import discord
from discord import app_commands

from cogs.tts.utils.app_commands import slash_mention


HelpSource = Literal["slash", "prefix"]
_PREFIX_ACCENT = discord.Color(0xE89B87)
_SLASH_ACCENT = discord.Color(0xAA9BE6)
HELP_CONTROL_ID = re.compile(
    r"help:v1:(?P<source>slash|prefix):(?P<owner>[0-9]{1,20}):(?P<music>[01]):"
    r"(?P<action>area|home|search|query|close)"
)
_CATEGORY_MENU_COPY = {
    "voice": ("Voz na call", "Eu leio em voz alta o que você escreve."),
    "music": ("Música", "Me pede uma faixa ou escolhe o que toca depois."),
    "games": ("Jogos e fichas", "Confere suas fichas ou chama alguém pro truco."),
    "chatbot": ("Conversa e imagens", "Me chama pra conversar ou me pede um desenho."),
    "server": ("Servidor", "Cuida dos tickets, das boas-vindas e dos cargos."),
}
# Cada marcador aceita texto provisório, um emoji Unicode ou um <:nome:id> do servidor.
_CATEGORY_MARKERS = {
    "voice": "[voz]",
    "music": "[som]",
    "games": "[jogos]",
    "chatbot": "[chat]",
    "server": "[staff]",
}


@dataclass(frozen=True)
class HelpCategory:
    key: str
    label: str
    description: str
    accent: discord.Color
    search_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class HelpEntry:
    key: str
    category: str | None
    group: str
    description: str
    usage: str
    search_terms: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    prefix_aliases: tuple[str, ...] = ()
    permission: str = "user"
    slash_root: str | None = None
    slash_path: str | None = None
    suffix: str = ""
    detail_note: str = ""
    show_in_category: bool = True


_CATALOG_PATH = Path(__file__).resolve().parents[1] / "shared" / "help_catalog.json"
_ACCENT_FACTORIES: dict[str, Callable[[], discord.Color]] = {
    "green": discord.Color.green,
    "blurple": discord.Color.blurple,
    "orange": discord.Color.orange,
    "fuchsia": discord.Color.fuchsia,
    "gold": discord.Color.gold,
}


def _catalog_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _load_help_catalog() -> tuple[tuple[HelpCategory, ...], tuple[HelpEntry, ...]]:
    raw = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or int(raw.get("version") or 0) != 1:
        raise RuntimeError("catálogo de ajuda incompatível")

    categories: list[HelpCategory] = []
    for item in raw.get("categories") or []:
        if not isinstance(item, dict):
            continue
        accent_name = str(item.get("accent") or "blurple").strip().lower()
        accent_factory = _ACCENT_FACTORIES.get(accent_name, discord.Color.blurple)
        categories.append(HelpCategory(
            key=str(item.get("key") or "").strip(),
            label=str(item.get("label") or "").strip(),
            description=str(item.get("description") or "").strip(),
            accent=accent_factory(),
            search_terms=_catalog_strings(item.get("search_terms")),
        ))

    entries: list[HelpEntry] = []
    for item in raw.get("entries") or []:
        if not isinstance(item, dict):
            continue
        category_raw = item.get("category")
        entries.append(HelpEntry(
            key=str(item.get("key") or "").strip(),
            category=str(category_raw).strip() if category_raw is not None else None,
            group=str(item.get("group") or "").strip(),
            description=str(item.get("description") or "").strip(),
            usage=str(item.get("usage") or "").strip(),
            search_terms=_catalog_strings(item.get("search_terms")),
            aliases=_catalog_strings(item.get("aliases")),
            prefix_aliases=_catalog_strings(item.get("prefix_aliases")),
            permission=str(item.get("permission") or "user").strip(),
            slash_root=str(item.get("slash_root") or "").strip() or None,
            slash_path=str(item.get("slash_path") or "").strip() or None,
            suffix=str(item.get("suffix") or ""),
            detail_note=str(item.get("detail_note") or "").strip(),
            show_in_category=bool(item.get("show_in_category", True)),
        ))

    category_keys = {category.key for category in categories}
    entry_keys = {entry.key for entry in entries}
    if not categories or not entries or len(category_keys) != len(categories) or len(entry_keys) != len(entries):
        raise RuntimeError("catálogo de ajuda inválido")
    if any(entry.category is not None and entry.category not in category_keys for entry in entries):
        raise RuntimeError("catálogo de ajuda possui categoria desconhecida")
    return tuple(categories), tuple(entries)


CATEGORIES, ENTRIES = _load_help_catalog()


_CATEGORY_DIRECT_TERMS: dict[str, tuple[str, ...]] = {
    "voice": ("voz", "voice", "tts", "falar"),
    "music": ("música", "music", "musica", "player"),
    "games": ("jogos", "games", "jogo", "economia"),
    "chatbot": ("chat", "chatbot", "ia"),
    "server": ("servidor", "server", "staff", "admin"),
}

_CATEGORY_BY_KEY = {category.key: category for category in CATEGORIES}
_ENTRY_BY_KEY = {entry.key: entry for entry in ENTRIES}


def _normalize(value: str) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _without_terminal_punctuation(value: str) -> str:
    return re.sub(r"[.!?…]+$", "", str(value or "").rstrip())


def _matching_prefix(raw: str, bot_prefix: str, registered_prefix: str) -> str:
    prefixes = sorted(filter(None, (bot_prefix, registered_prefix)), key=len, reverse=True)
    return next((prefix for prefix in prefixes if raw.startswith(prefix)), "")


def _strip_command_prefix(query: str, bot_prefix: str, registered_prefix: str = "") -> str:
    raw = str(query or "").strip()
    if raw.startswith("/"):
        raw = raw[1:].lstrip()
    prefix = _matching_prefix(raw, bot_prefix, registered_prefix)
    return raw[len(prefix):].lstrip()


def _permission_allowed(
    user: discord.abc.User,
    permission: str,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
) -> bool:
    if permission == "user" or permission in extra_permissions:
        return True
    if not isinstance(user, discord.Member):
        return False
    perms = getattr(user, "guild_permissions", None)
    if perms is None:
        return False

    is_owner = user.id == user.guild.owner_id
    is_admin = bool(getattr(perms, "administrator", False))
    if permission == "administrator":
        return bool(is_owner or is_admin)
    if permission == "manage_guild":
        return bool(is_owner or is_admin or getattr(perms, "manage_guild", False))
    if permission == "manage_messages":
        return bool(is_owner or is_admin or getattr(perms, "manage_messages", False))
    if permission == "say_staff":
        return bool(
            is_owner
            or is_admin
            or getattr(perms, "manage_guild", False)
            or getattr(perms, "manage_messages", False)
            or getattr(perms, "manage_webhooks", False)
        )
    if permission == "manage_roles":
        return bool(is_owner or is_admin or getattr(perms, "manage_roles", False))
    if permission == "kick":
        # Os handlers de TTS checam `kick_members` diretamente, então o help
        # reproduz exatamente essa regra em vez de assumir bypass por admin.
        return bool(is_owner or getattr(perms, "kick_members", False))
    if permission == "kick_or_manage_guild":
        return bool(
            is_owner
            or is_admin
            or getattr(perms, "kick_members", False)
            or getattr(perms, "manage_guild", False)
        )
    if permission == "antibot_staff":
        return bool(
            is_owner
            or is_admin
            or getattr(perms, "manage_guild", False)
            or getattr(perms, "manage_messages", False)
            or getattr(perms, "moderate_members", False)
            or getattr(perms, "kick_members", False)
            or getattr(perms, "ban_members", False)
        )
    if permission == "feedback_staff":
        return bool(
            is_owner
            or is_admin
            or getattr(perms, "kick_members", False)
            or getattr(perms, "manage_guild", False)
            or getattr(perms, "manage_messages", False)
        )
    if permission in {"economy_staff", "ticket_staff"}:
        return False
    return False


def _category_visible(
    category: HelpCategory,
    user: discord.abc.User,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
    hidden_categories: set[str] | frozenset[str] = frozenset(),
) -> bool:
    if category.key in hidden_categories:
        return False
    if category.key != "server":
        return True
    return any(
        entry.category == category.key
        and _permission_allowed(user, entry.permission, extra_permissions)
        for entry in ENTRIES
    )


def _entry_tokens(entry: HelpEntry) -> tuple[str, ...]:
    tokens = [entry.key, *entry.search_terms, *entry.aliases, *entry.prefix_aliases]
    if entry.slash_path:
        tokens.append(entry.slash_path)
    return tuple(token for token in tokens if token)


def _category_menu_copy(category: HelpCategory) -> tuple[str, str]:
    return _CATEGORY_MENU_COPY.get(category.key, (category.label, category.description))


def _category_tokens(category: HelpCategory) -> tuple[str, ...]:
    label, _ = _category_menu_copy(category)
    return (category.key, category.label, label, *category.search_terms)


def _prefix_entry_tokens(entry: HelpEntry) -> tuple[str, ...]:
    if not entry.usage.startswith("{bot_prefix}"):
        return ()

    raw = entry.usage[len("{bot_prefix}"):].strip()
    command_parts: list[str] = []
    for part in raw.split():
        if part.startswith("<") or part.startswith("@") or part.startswith("["):
            break
        command_parts.append(part)
    command = " ".join(command_parts).strip()
    return tuple(token for token in (command, *entry.aliases) if token)


def resolve_help_target(
    subject: str | None,
    *,
    bot_prefix: str,
    registered_prefix: str = "",
    hidden_categories: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, str | None]:
    if subject is None or not str(subject).strip():
        return ("home", None)

    raw = str(subject).strip()
    normalized_special = _normalize(raw)
    if normalized_special in {"home", "inicio", "indice"}:
        return ("home", None)
    if normalized_special.startswith("category:"):
        key = normalized_special.split(":", 1)[1]
        if key in _CATEGORY_BY_KEY and key not in hidden_categories:
            return ("category", key)
        # O seletor de um card antigo pode conter uma área que saiu do catálogo.
        return ("home", None)
    if normalized_special.startswith("entry:"):
        key = normalized_special.split(":", 1)[1]
        entry = _ENTRY_BY_KEY.get(key)
        if entry is not None and entry.category not in hidden_categories:
            return ("entry", key)

    is_slash_command = raw.startswith("/")
    is_prefix_command = bool(_matching_prefix(raw, bot_prefix, registered_prefix))
    normalized = _normalize(_strip_command_prefix(raw, bot_prefix, registered_prefix))

    # Quando o usuário inclui o prefixo, respeita o namespace real do comando.
    # Isso diferencia, por exemplo, `_reset` (TTS) de `/reset` (chatbot).
    if is_slash_command:
        for entry in ENTRIES:
            if entry.category in hidden_categories:
                continue
            if entry.slash_path and normalized == _normalize(entry.slash_path):
                return ("entry", entry.key)
    elif is_prefix_command:
        alias_match = None
        for entry in ENTRIES:
            if entry.category in hidden_categories:
                continue
            if any(normalized == _normalize(token) for token in _prefix_entry_tokens(entry)):
                return ("entry", entry.key)
            if alias_match is None and any(normalized == _normalize(alias) for alias in entry.prefix_aliases):
                alias_match = entry.key
        if alias_match is not None:
            return ("entry", alias_match)

    # Os nomes do índice também funcionam quando começam com o prefixo do bot.
    search_terms = {normalized, normalized_special}
    for category in CATEGORIES:
        if category.key in hidden_categories:
            continue
        direct_terms = _CATEGORY_DIRECT_TERMS.get(category.key, ())
        if any(_normalize(token) in search_terms for token in direct_terms):
            return ("category", category.key)

    for entry in ENTRIES:
        if entry.category in hidden_categories:
            continue
        if any(_normalize(token) in search_terms for token in _entry_tokens(entry)):
            return ("entry", entry.key)

    for category in CATEGORIES:
        if category.key in hidden_categories:
            continue
        if any(_normalize(token) in search_terms for token in _category_tokens(category)):
            return ("category", category.key)

    # Prefix matching só entra para textos com pelo menos 3 caracteres. Evita
    # que aliases curtos como `p`, `q` e `s` levem a resultados arbitrários.
    if len(normalized) >= 3:
        entry_matches: list[HelpEntry] = []
        for entry in ENTRIES:
            if entry.category in hidden_categories:
                continue
            if any(_normalize(token).startswith(normalized) for token in _entry_tokens(entry)):
                entry_matches.append(entry)
        if len(entry_matches) == 1:
            return ("entry", entry_matches[0].key)

    return ("notfound", raw)


def suggest_help_targets(
    subject: str,
    *,
    bot_prefix: str,
    registered_prefix: str = "",
    user: discord.abc.User | None = None,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
    hidden_categories: set[str] | frozenset[str] = frozenset(),
    limit: int = 3,
) -> list[HelpEntry | HelpCategory]:
    normalized = _normalize(_strip_command_prefix(subject, bot_prefix, registered_prefix))
    if not normalized:
        return []

    token_map: dict[str, HelpEntry | HelpCategory] = {}
    for category in CATEGORIES:
        if category.key in hidden_categories:
            continue
        if user is not None and not _category_visible(category, user, extra_permissions, hidden_categories):
            continue
        for token in _category_tokens(category):
            token_map.setdefault(_normalize(token), category)
    for entry in ENTRIES:
        if entry.category in hidden_categories:
            continue
        if user is not None and not _permission_allowed(user, entry.permission, extra_permissions):
            continue
        for token in _entry_tokens(entry):
            token_map.setdefault(_normalize(token), entry)

    matched_tokens = difflib.get_close_matches(normalized, list(token_map), n=max(limit * 3, 6), cutoff=0.78)
    results: list[HelpEntry | HelpCategory] = []
    seen: set[tuple[str, str]] = set()
    for token in matched_tokens:
        target = token_map[token]
        kind = "category" if isinstance(target, HelpCategory) else "entry"
        key = target.key
        marker = (kind, key)
        if marker in seen:
            continue
        seen.add(marker)
        results.append(target)
        if len(results) >= limit:
            break
    return results


def help_autocomplete_choices(
    current: str,
    *,
    user: discord.abc.User,
    prefixes: dict[str, str],
    games_context: dict[str, str] | None = None,
    extra_permissions: set[str] | frozenset[str] = frozenset(),
    hidden_categories: set[str] | frozenset[str] = frozenset(),
    limit: int = 25,
) -> list[app_commands.Choice[str]]:
    bot_prefix = prefixes.get("bot_prefix", "_")
    query = _normalize(_strip_command_prefix(current, bot_prefix, prefixes.get("registered_prefix", "")))
    scored: list[tuple[int, str, str]] = []

    for category in CATEGORIES:
        if not _category_visible(category, user, extra_permissions, hidden_categories):
            continue
        tokens = tuple(_normalize(token) for token in _category_tokens(category))
        if not query:
            score = 0
        elif any(token.startswith(query) for token in tokens):
            score = 0
        elif any(query in token for token in tokens):
            score = 1
        else:
            continue
        label, _ = _category_menu_copy(category)
        scored.append((score, label, f"category:{category.key}"))

    for entry in ENTRIES:
        if entry.category in hidden_categories:
            continue
        if not _permission_allowed(user, entry.permission, extra_permissions):
            continue
        tokens = tuple(_normalize(token) for token in _entry_tokens(entry))
        if not query:
            # Sem texto, evita despejar dezenas de comandos: mostra só as categorias.
            continue
        if any(token == query for token in tokens):
            score = 0
        elif any(token.startswith(query) for token in tokens):
            score = 1
        elif any(query in token for token in tokens):
            score = 2
        else:
            continue
        if not entry.show_in_category and not any(token == query for token in tokens):
            continue
        public_name = _plain_usage(entry, prefixes, games_context)
        name = f"{public_name} — {_without_terminal_punctuation(entry.description)}"[:100]
        scored.append((score, name, f"entry:{entry.key}"))

    scored.sort(key=lambda item: (item[0], item[1].casefold()))
    return [app_commands.Choice(name=name, value=value) for _, name, value in scored[:limit]]


def _entry_bot_prefix(entry: HelpEntry, prefixes: dict[str, str]) -> str:
    bot_prefix = prefixes.get("bot_prefix", "_")
    if entry.category in {"music", "games", "server"}:
        return prefixes.get("registered_prefix", bot_prefix)
    return bot_prefix


def _plain_usage(
    entry: HelpEntry,
    prefixes: dict[str, str],
    games_context: dict[str, str] | None = None,
) -> str:
    bot_prefix = _entry_bot_prefix(entry, prefixes)
    try:
        rendered = entry.usage.format_map({**prefixes, "bot_prefix": bot_prefix})
    except Exception:
        rendered = entry.usage

    if (
        entry.category == "games"
        and entry.slash_path is None
        and "{bot_prefix}" not in entry.usage
        and (games_context or {}).get("mode") == "commands"
    ):
        return f"{bot_prefix}{rendered}"
    return rendered


def _format_usage(
    entry: HelpEntry,
    prefixes: dict[str, str],
    root_ids: dict[str, int],
    games_context: dict[str, str] | None = None,
) -> str:
    if entry.slash_path and entry.slash_root and root_ids.get(entry.slash_root):
        return slash_mention(root_ids, root=entry.slash_root, path=entry.slash_path) + entry.suffix
    return f"`{_plain_usage(entry, prefixes, games_context)}`"


def _aliases_line(
    entry: HelpEntry,
    prefixes: dict[str, str],
    games_context: dict[str, str] | None = None,
) -> str:
    bot_prefix = _entry_bot_prefix(entry, prefixes)
    prefixed_aliases = "{bot_prefix}" in entry.usage or (
        entry.category == "games" and (games_context or {}).get("mode") == "commands"
    )
    alias_prefix = bot_prefix if prefixed_aliases else ""
    names = [f"{alias_prefix}{alias}" for alias in entry.aliases]
    names.extend(f"{bot_prefix}{alias}" for alias in entry.prefix_aliases)
    return ", ".join(f"`{name}`" for name in dict.fromkeys(names))


def _category_entries(
    category: HelpCategory,
    user: discord.abc.User,
    extra_permissions: set[str] | frozenset[str],
) -> list[HelpEntry]:
    return [
        entry
        for entry in ENTRIES
        if entry.category == category.key
        and entry.show_in_category
        and _permission_allowed(user, entry.permission, extra_permissions)
    ]


@dataclass
class _HelpPage:
    header: str
    blocks: list[str]
    accent: discord.Color


def _render_category(
    category: HelpCategory,
    entries: list[HelpEntry],
    *,
    prefixes: dict[str, str],
    root_ids: dict[str, int],
    games_context: dict[str, str],
    music_offline: bool,
) -> _HelpPage:
    label, intro = _category_menu_copy(category)
    if category.key == "voice":
        intro = f"Comigo na call, manda `{prefixes['gtts_prefix']}bom dia` no chat. Eu leio em voz alta."
    elif category.key == "music":
        prefix = _entry_bot_prefix(_ENTRY_BY_KEY["play"], prefixes)
        intro = f"Me pede uma faixa com `{prefix}play Basket Case`. Também aceito link."
        if music_offline:
            intro = "Agora eu tô sem conexão com o player. Os comandos ficam aqui pra quando ele voltar."
    elif category.key == "games":
        balance = _plain_usage(_ENTRY_BY_KEY["ficha"], prefixes, games_context)
        channel = games_context.get("channel_mention")
        where = f" em {channel}" if channel else ""
        intro = f"Vai jogar? Manda `{balance}`{where} pra ver com quantas fichas você tá."
    elif category.key == "chatbot":
        intro = "Me marca no chat e manda um assunto. Se quiser uma imagem, me conta o que desenhar."
    elif category.key == "server":
        intro = "Aqui ficam os ajustes que você pode fazer neste servidor."

    groups: dict[str, list[str]] = {}
    for entry in entries:
        usage = _format_usage(entry, prefixes, root_ids, games_context)
        line = f"{usage} · {_without_terminal_punctuation(entry.description)}"
        aliases = _aliases_line(entry, prefixes, games_context)
        if aliases:
            line += f" ({aliases})"
        groups.setdefault(entry.group, []).append(line)
    blocks = ["\n".join([f"### {group}", *commands]) for group, commands in groups.items()]
    return _HelpPage(f"## {label}\n{intro}", blocks, category.accent)


def _entry_display_title(
    entry: HelpEntry,
    prefixes: dict[str, str],
    games_context: dict[str, str] | None = None,
) -> str:
    if entry.slash_path:
        return f"/{entry.slash_path}"
    plain = _plain_usage(entry, prefixes, games_context)
    if entry.usage.endswith("_prefix}texto"):
        return plain
    return plain.split(" ", 1)[0]


def _render_entry(
    entry: HelpEntry,
    *,
    user: discord.abc.User,
    prefixes: dict[str, str],
    root_ids: dict[str, int],
    games_context: dict[str, str],
    extra_permissions: frozenset[str],
) -> _HelpPage:
    usage = _format_usage(entry, prefixes, root_ids, games_context)
    category = _CATEGORY_BY_KEY.get(entry.category or "")
    title = _entry_display_title(entry, prefixes, games_context)
    blocks = [f"**Como usar**\n{usage}"]

    aliases = _aliases_line(entry, prefixes, games_context)
    if aliases:
        blocks.append(f"**Também funciona com**\n{aliases}")
    if entry.detail_note:
        blocks.append(f"-# {entry.detail_note}")
    elif not _permission_allowed(user, entry.permission, extra_permissions):
        blocks.append("-# Você precisa de permissão pra usar esse comando.")
    accent = category.accent if category else discord.Color.teal()
    return _HelpPage(f"## `{title}`\n{entry.description}", blocks, accent)


def _category_option(category: HelpCategory, *, selected: bool) -> discord.SelectOption:
    label, description = _category_menu_copy(category)
    marker = _CATEGORY_MARKERS.get(category.key, "")
    emoji = None
    if marker.startswith("["):
        description = f"{marker} {description}"
    elif marker:
        emoji = marker
    return discord.SelectOption(
        label=label, description=description[:100], emoji=emoji,
        value=f"category:{category.key}", default=selected,
    )


def help_search_modal(button_id: str) -> discord.ui.Modal:
    modal = discord.ui.Modal(title="Buscar comando", custom_id=button_id.removesuffix(":search") + ":query")
    modal.add_item(discord.ui.Label(
        text="O que você quer procurar?",
        component=discord.ui.TextInput(custom_id="assunto", placeholder="Ex.: join, fichas, voz", max_length=80),
    ))
    # O mesmo listener atende o formulário, mesmo após um reload do cog.
    modal.stop()
    return modal


class HelpCenterView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        owner: discord.abc.User,
        prefixes: dict[str, str],
        root_ids: dict[str, int],
        source: HelpSource = "slash",
        avatar_url: str | None = None,
        games_context: dict[str, str] | None = None,
        extra_permissions: set[str] | frozenset[str] = frozenset(),
        hidden_categories: set[str] | frozenset[str] = frozenset(),
        subject: str | None = None,
        initial_category: str | None = None,
        music_from_activity: bool = False,
    ):
        super().__init__(timeout=None)
        self.owner = owner
        self.source = source
        self.prefixes = {
            "bot_prefix": "_", "gtts_prefix": ".", "edge_prefix": ",",
            "atts_prefix": "%", "teto_prefix": "'", **prefixes,
        }
        self.root_ids = dict(root_ids) if source == "slash" else {}
        self.games_context = dict(games_context or {})
        self.extra_permissions = frozenset(extra_permissions)
        from_activity = not (subject or "").strip() and initial_category is not None
        music_from_activity = music_from_activity or (from_activity and initial_category == "music")
        control_prefix = f"help:v1:{source}:{owner.id}:{int(music_from_activity)}"
        self.hidden_categories = frozenset(hidden_categories)
        if music_from_activity:
            self.hidden_categories -= {"music"}

        categories = [
            category for category in CATEGORIES
            if _category_visible(category, owner, self.extra_permissions, self.hidden_categories)
        ]
        visible_keys = {category.key for category in categories}
        if from_activity and initial_category in visible_keys:
            subject = f"category:{initial_category}"
        self.target_kind, self.target_key = resolve_help_target(
            subject, bot_prefix=self.prefixes["bot_prefix"],
            registered_prefix=self.prefixes.get("registered_prefix", ""),
            hidden_categories=self.hidden_categories,
        )
        if self.target_kind == "category" and self.target_key not in visible_keys:
            self.target_kind, self.target_key = "home", None

        page = self._page(categories, music_offline="music" in hidden_categories)
        header: discord.ui.Item = discord.ui.TextDisplay(page.header)
        if avatar_url and self.target_kind == "home":
            header = discord.ui.Section(header, accessory=discord.ui.Thumbnail(avatar_url, description="Meu avatar"))
        card = discord.ui.Container(header, accent_color=_PREFIX_ACCENT if source == "prefix" else page.accent)
        if page.blocks:
            card.add_item(discord.ui.Separator())
            for block in page.blocks:
                card.add_item(discord.ui.TextDisplay(block))
        if source == "prefix" and self.target_kind != "home":
            card.add_item(discord.ui.TextDisplay(f"-# Pra voltar pelo chat, manda `{self.prefixes['bot_prefix']}help início`."))

        self.add_item(card)
        if categories:
            selected_key = self.target_key if self.target_kind == "category" else None
            options = [_category_option(category, selected=category.key == selected_key) for category in categories]
            self.add_item(discord.ui.ActionRow(discord.ui.Select(
                custom_id=f"{control_prefix}:area", placeholder="Escolha uma área", options=options,
            )))
        actions = discord.ui.ActionRow(
            discord.ui.Button(label="Buscar comando", style=discord.ButtonStyle.primary,
                              custom_id=f"{control_prefix}:search"),
        )
        if self.target_kind != "home":
            actions.add_item(discord.ui.Button(label="Início", custom_id=f"{control_prefix}:home"))
        actions.add_item(discord.ui.Button(label="Fechar", custom_id=f"{control_prefix}:close"))
        self.add_item(actions)
        # O cog recebe os cliques pelos IDs da mensagem, inclusive após reiniciar.
        # O SDK só precisa enviar o layout, sem guardar uma view por mensagem.
        self.stop()

    def _index(self, categories: list[HelpCategory]) -> _HelpPage:
        if self.source == "prefix":
            prefix = self.prefixes["bot_prefix"]
            header = f"## Meus comandos\nQuer me chamar pra call? Manda `{prefix}help join` que eu te mostro como."
            shortcuts = []
            for category in categories:
                query = _CATEGORY_DIRECT_TERMS.get(category.key, (category.key,))[0]
                label, _ = _category_menu_copy(category)
                shortcuts.append(f"`{prefix}help {query}`  ·  **{label}**")
            return _HelpPage(header, ["\n".join(shortcuts)] if shortcuts else [], _PREFIX_ACCENT)

        header = "## Meus comandos\nEsqueceu o comando que me faz cantar? Toca em **Buscar comando** e escreve `cantar`."
        areas = []
        for category in categories:
            label, description = _category_menu_copy(category)
            areas.append(f"**{label}**\n-# {description}")
        return _HelpPage(header, areas, _SLASH_ACCENT)

    def _page(self, categories: list[HelpCategory], *, music_offline: bool) -> _HelpPage:
        if self.target_kind == "home":
            return self._index(categories)
        if self.target_kind == "category":
            category = _CATEGORY_BY_KEY[self.target_key]
            return _render_category(
                category, _category_entries(category, self.owner, self.extra_permissions),
                prefixes=self.prefixes, root_ids=self.root_ids,
                games_context=self.games_context, music_offline=music_offline,
            )
        if self.target_kind == "entry":
            return _render_entry(
                _ENTRY_BY_KEY[self.target_key], user=self.owner, prefixes=self.prefixes,
                root_ids=self.root_ids, games_context=self.games_context,
                extra_permissions=self.extra_permissions,
            )

        subject = str(self.target_key or "")
        suggestions = suggest_help_targets(
            subject, bot_prefix=self.prefixes["bot_prefix"],
            registered_prefix=self.prefixes.get("registered_prefix", ""),
            user=self.owner, extra_permissions=self.extra_permissions, hidden_categories=self.hidden_categories,
        )
        labels = [
            _category_menu_copy(target)[0] if isinstance(target, HelpCategory)
            else _entry_display_title(target, self.prefixes, self.games_context)
            for target in suggestions
        ]
        hint = "Tenta buscar pelo nome de um comando, como `join`, ou escolhe uma área aqui embaixo."
        if labels:
            hint = "Você quis dizer " + " ou ".join(f"`{label}`" for label in labels) + "?"
        term = " ".join(subject.split())[:80].replace("`", "ˋ")
        return _HelpPage(f"## Não achei esse comando\nProcurei por `{term}`.", [hint], _SLASH_ACCENT)

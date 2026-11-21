from __future__ import annotations

from collections import OrderedDict

_MAX_ACTIVITIES = 4096
_WINDOW_MS = 5 * 60 * 1000
# O timestamp em milissegundos ocupa os bits acima dos 22 bits baixos do ID do Discord.
_SNOWFLAKE_TIME_SHIFT = 22
_CATEGORY_BY_MODULE = {
    "antibot": "server",
    "birthday": "server",
    "chatbot": "chatbot",
    "color_roles": "server",
    "feedback": "server",
    "forms": "server",
    "games": "games",
    "musica": "music",
    "role_icons": "server",
    "say": "server",
    "tickets": "server",
    "tts": "voice",
    "welcome": "server",
}


def _record_event(bot, event, category: str | None, priority: int) -> str | None:
    user = getattr(event, "author", None) or getattr(event, "user", None)
    user_id = getattr(user, "id", None)
    event_id = getattr(event, "id", None)
    if bot is None or not user_id or not event_id or getattr(user, "bot", False):
        return None

    guild_id = getattr(event, "guild_id", None) or getattr(getattr(event, "guild", None), "id", 0)
    key = (guild_id, user_id)
    activities = getattr(bot, "_help_activities", None)
    if activities is None:
        activities = bot._help_activities = OrderedDict()

    cutoff_ms = (event_id >> _SNOWFLAKE_TIME_SHIFT) - _WINDOW_MS
    # Limpa registros inativos durante o uso normal, sem um timer para cada pessoa.
    while activities:
        oldest_order, _ = next(iter(activities.values()))
        if (oldest_order[0] >> _SNOWFLAKE_TIME_SHIFT) > cutoff_ms:
            break
        activities.popitem(last=False)

    order = (event_id, priority)
    previous = activities.get(key)
    if previous is not None and order <= previous[0]:
        return None
    activities[key] = (order, category)
    activities.move_to_end(key)
    if len(activities) > _MAX_ACTIVITIES:
        activities.popitem(last=False)

    if category is None and previous is not None:
        (previous_id, _), previous_category = previous
        if previous_id < event_id and (previous_id >> _SNOWFLAKE_TIME_SHIFT) > cutoff_ms:
            return previous_category
    return None


def remember_help_category(bot, event, category: str, *, registered: bool = False) -> None:
    # No mesmo evento, um comando registrado tem precedência sobre um gatilho de texto.
    _record_event(bot, event, category, priority=1 if registered else 0)


def consume_help_category(bot, event) -> str | None:
    # Guarda apenas o ID do help. Um reconhecimento atrasado não pode repor a área consumida.
    return _record_event(bot, event, None, priority=2)


def remember_help_command(bot, event, command) -> None:
    callback = getattr(command, "callback", None)
    package, _, module = getattr(callback, "__module__", "").partition(".")
    if package != "cogs":
        return
    category = _CATEGORY_BY_MODULE.get(module.split(".", 1)[0])
    if category is not None:
        remember_help_category(bot, event, category, registered=True)

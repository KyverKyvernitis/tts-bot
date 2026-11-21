from __future__ import annotations

from collections import OrderedDict

_MAX_ACTIVITIES = 4096
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


def remember_help_category(bot, event, category: str, *, registered: bool = False) -> None:
    user = getattr(event, "author", None) or getattr(event, "user", None)
    user_id = getattr(user, "id", None)
    event_id = getattr(event, "id", None)
    if bot is None or not user_id or not event_id or getattr(user, "bot", False):
        return

    guild_id = getattr(event, "guild_id", None) or getattr(getattr(event, "guild", None), "id", 0)
    key = (guild_id, user_id)
    activities = getattr(bot, "_help_activities", None)
    if activities is None:
        activities = bot._help_activities = OrderedDict()

    # A ordem dos eventos preserva a tentativa mais nova se o reconhecimento atrasar.
    # Na mesma mensagem, o comando registrado tem precedência sobre o gatilho de texto.
    order = (event_id, registered)
    previous = activities.get(key)
    if previous is not None and order <= previous[0]:
        return
    activities[key] = (order, category)
    activities.move_to_end(key)
    if len(activities) > _MAX_ACTIVITIES:
        activities.popitem(last=False)


def last_help_category(bot, guild, user, *, before: int | None = None) -> str | None:
    activities = getattr(bot, "_help_activities", None)
    record = activities.get((getattr(guild, "id", 0), user.id)) if activities else None
    if record is None:
        return None
    (event_id, _), category = record
    return category if before is None or event_id < before else None


def remember_help_command(bot, event, command) -> None:
    callback = getattr(command, "callback", None)
    package, _, module = getattr(callback, "__module__", "").partition(".")
    if package != "cogs":
        return
    category = _CATEGORY_BY_MODULE.get(module.split(".", 1)[0])
    if category is not None:
        remember_help_category(bot, event, category, registered=True)

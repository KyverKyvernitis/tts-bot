from __future__ import annotations

import traceback

import discord

import config


def _encurtar(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def valor_tts_atual(cog: "TTSVoice", guild_id: int, user_id: int, key: str, default: str = "", *, server: bool = False) -> str:
    db = cog._get_db()
    try:
        if server and db is not None and hasattr(db, "get_guild_tts_defaults"):
            data = db.get_guild_tts_defaults(guild_id) or {}
            return str((data or {}).get(key) or default or "")
        if db is not None and hasattr(db, "resolve_tts"):
            data = db.resolve_tts(guild_id, user_id) or {}
            return str((data or {}).get(key) or default or "")
    except Exception:
        pass
    return str(default or "")


def valores_selecionados(item) -> list[str]:
    try:
        values = []
        for value in (getattr(item, "values", None) or []):
            if value is None:
                continue
            if hasattr(value, "id") and not isinstance(value, str):
                value = str(getattr(value, "id", "") or "")
            value = str(value or "").strip()
            if value:
                values.append(value)
        return values
    except Exception:
        return []


def cargos_selecionados(item) -> list[discord.Role]:
    roles: list[discord.Role] = []
    try:
        for value in (getattr(item, "values", None) or []):
            if isinstance(value, discord.Role):
                roles.append(value)
    except Exception:
        pass
    return roles


def primeiro_cargo_selecionado(item) -> discord.Role | None:
    roles = cargos_selecionados(item)
    return roles[0] if roles else None


def valor_item(item, default: str = "") -> str:
    try:
        value = getattr(item, "value", None)
        if value is None:
            return str(default or "")
        return str(value or "").strip()
    except Exception:
        return str(default or "")


def componentes_experimentais_modal_ativos() -> bool:
    # Selects/radio/checkbox dentro de modal ainda variam bastante entre
    # versões da lib/cliente. Mantemos desligado por padrão para não quebrar
    # a interação do painel; o painel continua usando selects na mensagem e
    # modais seguros com TextInput.
    return bool(getattr(config, "TTS_EXPERIMENTAL_MODAL_COMPONENTS", False))


def tentar_adicionar_grupo_radio(modal, attr_name: str, *, label: str, options: list[tuple[str, str, str]], default_value: str) -> bool:
    if not componentes_experimentais_modal_ativos():
        return False
    group_cls = getattr(discord.ui, "RadioGroup", None)
    if group_cls is None:
        return False
    try:
        group = group_cls(custom_id=attr_name, required=True, options=[])
        for opt_label, value, description in options:
            group.add_option(
                label=opt_label,
                value=value,
                description=description or None,
                default=(str(value) == str(default_value)),
            )
        modal.add_item(group)
        setattr(modal, attr_name, group)
        return True
    except Exception as e:
        print(f"[tts_modal] RadioGroup desativado/falhou: {e!r}")
        return False


def tentar_adicionar_grupo_checkbox(modal, attr_name: str, *, options: list[tuple[str, str, str, bool]], min_values: int = 0, max_values: int | None = None) -> bool:
    if not componentes_experimentais_modal_ativos():
        return False
    group_cls = getattr(discord.ui, "CheckboxGroup", None)
    if group_cls is None:
        return False
    try:
        group = group_cls(custom_id=attr_name, required=False, min_values=min_values, max_values=max_values or len(options), options=[])
        for opt_label, value, description, selected in options:
            group.add_option(
                label=opt_label,
                value=value,
                description=description or None,
                default=bool(selected),
            )
        modal.add_item(group)
        setattr(modal, attr_name, group)
        return True
    except Exception as e:
        print(f"[tts_modal] CheckboxGroup desativado/falhou: {e!r}")
        return False


def criar_seletor_opcional(*, placeholder: str, options: list[discord.SelectOption]):
    kwargs = dict(placeholder=placeholder, min_values=0, max_values=1, options=options[:25])
    try:
        return discord.ui.Select(required=False, **kwargs)
    except TypeError:
        return discord.ui.Select(**kwargs)


def valor_unico_componente(item, default: str = "") -> str:
    values = valores_selecionados(item)
    if values:
        return str(values[0] or "").strip()
    return valor_item(item, default)


def opcoes_com_valor_padrao(options: list[discord.SelectOption], current: str) -> list[discord.SelectOption]:
    current = str(current or "").strip()
    seen: set[str] = set()
    fixed: list[discord.SelectOption] = []
    matched_current = False

    for option in options or []:
        value = str(getattr(option, "value", "") or "").strip()
        if not value or value in seen:
            continue
        try:
            option.default = bool(current and value == current)
            matched_current = matched_current or bool(option.default)
        except Exception:
            pass
        fixed.append(option)
        seen.add(value)
        if len(fixed) >= 25:
            break

    if current and not matched_current:
        fixed.insert(0, discord.SelectOption(label=_encurtar(current, 100), description="Valor atual", value=current, default=True))

    return fixed[:25]


def rotulo_modal_disponivel() -> bool:
    return bool(hasattr(discord.ui, "Label"))


def criar_entrada_texto_modal(*, label: str | None, placeholder: str, current: str = "", max_length: int = 80, required: bool = False):
    kwargs = {
        "placeholder": placeholder,
        "required": required,
        "max_length": max_length,
    }
    # Components V2 proíbe label duplicado: quando o TextInput é filho de um
    # discord.ui.Label, somente o contêiner pode fornecer o texto do rótulo.
    if label is not None:
        kwargs["label"] = label
    item = discord.ui.TextInput(**kwargs)
    try:
        item.default = str(current or "")[:max_length]
    except Exception:
        try:
            item.value = str(current or "")[:max_length]
        except Exception:
            pass
    return item


def adicionar_entrada_texto_modal(modal, attr_name: str, *, label: str, placeholder: str, current: str = "", max_length: int = 80, required: bool = False) -> None:
    item = criar_entrada_texto_modal(label=label, placeholder=placeholder, current=current, max_length=max_length, required=required)
    modal.add_item(item)
    setattr(modal, attr_name, item)


def adicionar_item_rotulo_modal(modal, attr_name: str, *, text: str, description: str = "", component=None) -> bool:
    label_cls = getattr(discord.ui, "Label", None)
    if label_cls is None or component is None:
        return False
    try:
        modal.add_item(label_cls(text=str(text or "")[:45], description=(str(description or "")[:100] or None), component=component))
        setattr(modal, attr_name, component)
        return True
    except Exception as e:
        print(f"[tts_modal] Label desativado/falhou: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)
        return False


def criar_seletor_modal(custom_id: str, *, placeholder: str, options: list[discord.SelectOption], required: bool = True):
    kwargs = dict(custom_id=custom_id, placeholder=str(placeholder or "")[:150], min_values=1 if required else 0, max_values=1, options=(options or [])[:25])
    try:
        return discord.ui.Select(required=required, **kwargs)
    except TypeError:
        return discord.ui.Select(**kwargs)


def valores_padrao_seletor_cargo(default_role: discord.Role | None) -> list[object]:
    if default_role is None:
        return []
    default_value_cls = getattr(discord, "SelectDefaultValue", None)
    if default_value_cls is None:
        return []
    from_role = getattr(default_value_cls, "from_role", None)
    if callable(from_role):
        try:
            return [from_role(default_role)]
        except Exception as e:
            print(f"[tts_modal] SelectDefaultValue.from_role falhou: {e!r}")
    try:
        value_type = getattr(getattr(discord, "SelectDefaultValueType", None), "role", None)
        if value_type is not None:
            return [default_value_cls(id=int(getattr(default_role, "id", 0) or 0), type=value_type)]
    except Exception as e:
        print(f"[tts_modal] SelectDefaultValue manual falhou: {e!r}")
    return []


def criar_seletor_cargo_modal(custom_id: str, *, placeholder: str, required: bool = False, default_role: discord.Role | None = None):
    role_select_cls = getattr(discord.ui, "RoleSelect", None)
    if role_select_cls is None:
        return None
    default_values = valores_padrao_seletor_cargo(default_role)
    kwargs = dict(
        custom_id=custom_id,
        placeholder=str(placeholder or "")[:150],
        min_values=1 if required else 0,
        max_values=1,
    )
    if default_values:
        kwargs["default_values"] = default_values
    try:
        return role_select_cls(required=required, **kwargs)
    except TypeError:
        kwargs.pop("default_values", None)
        try:
            select = role_select_cls(required=required, **kwargs)
        except TypeError:
            select = role_select_cls(**kwargs)
        if default_values:
            try:
                select.default_values = default_values
            except Exception as e:
                print(f"[tts_modal] RoleSelect default_values indisponível: {e!r}")
        return select


def valores_radio_correspondem(left: object, right: object) -> bool:
    a = str(left or "").strip().replace("+", "")
    b = str(right or "").strip().replace("+", "")
    if a == b:
        return True
    try:
        na = float(a.lower().replace("hz", "").replace("%", ""))
        nb = float(b.lower().replace("hz", "").replace("%", ""))
        return abs(na - nb) < 0.001
    except Exception:
        return False


def criar_radio_modal(custom_id: str, *, options: list[tuple[str, str, str]], default_value: str):
    group_cls = getattr(discord.ui, "RadioGroup", None)
    if group_cls is None:
        return None
    try:
        group = group_cls(custom_id=custom_id, required=True, options=[])
        default_seen = any(valores_radio_correspondem(value, default_value) for _, value, _ in options)
        for label, value, description in options:
            is_default = valores_radio_correspondem(value, default_value) if default_seen else label.casefold() == "normal"
            group.add_option(
                label=label[:100],
                value=str(value)[:100],
                description=(description or None),
                default=is_default,
            )
        return group
    except Exception as e:
        print(f"[tts_modal] RadioGroup desativado/falhou: {e!r}")
        return None


def adicionar_radio_modal(modal, attr_name: str, *, text: str, description: str, options: list[tuple[str, str, str]], current: str) -> bool:
    group = criar_radio_modal(attr_name, options=options, default_value=str(current or ""))
    if group is None:
        return False
    return adicionar_item_rotulo_modal(modal, attr_name, text=text, description=description, component=group)


def criar_grupo_checkbox_modal(custom_id: str, *, options: list[tuple[str, str, str, bool]], min_values: int = 0, max_values: int | None = None):
    group_cls = getattr(discord.ui, "CheckboxGroup", None)
    if group_cls is None:
        return None
    try:
        group = group_cls(custom_id=custom_id, required=False, min_values=min_values, max_values=max_values or len(options), options=[])
        for label, value, description, default in options:
            group.add_option(label=label[:100], value=str(value)[:100], description=(description or None), default=bool(default))
        return group
    except Exception as e:
        print(f"[tts_modal] CheckboxGroup desativado/falhou: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)
        return None

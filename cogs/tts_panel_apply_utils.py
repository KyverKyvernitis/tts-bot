import discord

from .tts_voice_common import validate_mode

async def _apply_server_prefix_from_modal(
    cog,
    interaction: discord.Interaction,
    *,
    prefix_kind: str,
    prefix: str,
    panel_message: discord.Message,
):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar os prefixos do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    cleaned = (prefix or "").strip()
    if not cleaned:
        await interaction.response.send_message(
            embed=cog._make_embed("Prefixo inválido", "O prefixo não pode ficar vazio.", ok=False),
            ephemeral=True,
        )
        return

    cleaned = cleaned[:8]

    if prefix_kind == "bot":
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, bot_prefix=cleaned))
        desc = f"O prefixo do bot do servidor agora é `{cleaned}`"
        history_entry = cog._server_history_text(interaction, "o prefixo dos comandos", cog._quote_value(cleaned))
        title = "Prefixo do bot atualizado"
    elif prefix_kind == "edge":
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, edge_prefix=cleaned))
        desc = f"O prefixo do modo Edge do servidor agora é `{cleaned}`"
        history_entry = cog._server_history_text(interaction, "o prefixo do modo Edge", cog._quote_value(cleaned))
        title = "Prefixo do modo Edge atualizado"
    elif prefix_kind == "gcloud":
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, gcloud_prefix=cleaned))
        desc = f"O prefixo do Google Cloud do servidor agora é `{cleaned}`"
        history_entry = cog._server_history_text(interaction, "o prefixo do Google Cloud", cog._quote_value(cleaned))
        title = "Prefixo do Google Cloud atualizado"
    else:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, gtts_prefix=cleaned, tts_prefix=cleaned))
        desc = f"O prefixo do modo gTTS do servidor agora é `{cleaned}`"
        history_entry = cog._server_history_text(interaction, "o prefixo do modo gTTS", cog._quote_value(cleaned))
        title = "Prefixo do modo gTTS atualizado"

    await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
    cog._append_public_panel_history(getattr(panel_message, "id", None), history_entry)
    last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
    embed = await cog._build_settings_embed(
        interaction.guild.id,
        interaction.user.id,
        server=True,
        panel_kind="server",
        last_changes=last_changes,
        message_id=getattr(panel_message, "id", None),
    )
    view = cog._build_panel_view(0 if getattr(panel_message, "id", None) in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=True)
    view.message = panel_message
    edited = False
    try:
        if getattr(interaction, "message", None) is not None and getattr(interaction.message, "id", None) == getattr(panel_message, "id", None):
            await interaction.response.edit_message(embed=embed, view=view)
            edited = True
        else:
            await panel_message.edit(embed=embed, view=view)
            edited = True
    except discord.NotFound:
        print("[tts_panel] painel antigo não existe mais; seguindo sem editar")
    except Exception as e:
        print(f"[tts_panel] falha ao editar painel: {e!r}")

    if edited:
        await interaction.followup.send(
            embed=cog._make_embed(title, desc, ok=True),
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            embed=cog._make_embed(title, desc, ok=True),
            ephemeral=True,
        )
    await cog._announce_panel_change(
        interaction,
        title=title,
        description=desc,
        target_message=panel_message,
    )

async def _apply_mode_from_panel(cog, interaction: discord.Interaction, mode: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    value = validate_mode(mode)
    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, engine=value))
        desc = f"O modo padrão do servidor agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS, Edge e Google Cloud continuam escolhendo o motor por mensagem."
        history_entry = cog._server_history_text(interaction, "o modo padrão do servidor", value)
        await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
    else:
        history_entry = cog._user_history_text(interaction, "o próprio modo" if effective_user_id == interaction.user.id else "o modo", value, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, engine=value, history_entry=history_entry)
        desc = f"O modo de TTS de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"O seu modo de TTS agora é `{value}`. Esse ajuste só afeta comandos antigos e compatibilidade; os prefixos gTTS, Edge e Google Cloud continuam escolhendo o motor por mensagem."
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id if not server else interaction.user.id,
        server=server,
        panel_kind="server" if server else "user",
        last_changes=last_changes,
        message_id=message_id,
        target_user_name=effective_user_name if not server else None,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Modo atualizado",
        description=desc,
        target_message=panel_message,
    )
    if server:
        await cog._announce_panel_change(interaction, title="Modo atualizado", description=desc)

async def _apply_voice_from_panel(cog, interaction: discord.Interaction, voice: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    if voice not in cog.edge_voice_names and voice not in cog.edge_voice_cache:
        await interaction.response.send_message(
            embed=cog._make_embed("Voz inválida", "Essa voz não foi encontrada na lista do Edge.", ok=False),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, voice=voice))
        desc = f"A voz padrão do servidor agora é `{voice}`."
        history_entry = cog._server_history_text(interaction, "a voz padrão do servidor", voice)
        await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
    else:
        history_entry = cog._user_history_text(interaction, "a própria voz" if effective_user_id == interaction.user.id else "a voz", voice, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, voice=voice, history_entry=history_entry)
        desc = f"A voz do Edge de {effective_user_name} agora é `{voice}`." if effective_user_id != interaction.user.id else f"A sua voz do Edge agora é `{voice}`."
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id if not server else interaction.user.id,
        server=server,
        panel_kind="server" if server else "user",
        last_changes=last_changes,
        message_id=message_id,
        target_user_name=effective_user_name if not server else None,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Configuração de TTS atualizada",
        description=desc,
        target_message=panel_message,
    )
    if server:
        await cog._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)

async def _apply_language_from_panel(cog, interaction: discord.Interaction, language: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, language=language))
        desc = f"O idioma padrão do servidor agora é `{language}`."
        history_entry = cog._server_history_text(interaction, "o idioma padrão do servidor", language)
        await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
    else:
        history_entry = cog._user_history_text(interaction, "o próprio idioma" if effective_user_id == interaction.user.id else "o idioma", language, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, language=language, history_entry=history_entry)
        desc = f"O idioma do gtts de {effective_user_name} agora é `{language}`." if effective_user_id != interaction.user.id else f"O seu idioma do gtts agora é `{language}`."
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id if not server else interaction.user.id,
        server=server,
        panel_kind="server" if server else "user",
        last_changes=last_changes,
        message_id=message_id,
        target_user_name=effective_user_name if not server else None,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Configuração de TTS atualizada",
        description=desc,
        target_message=panel_message,
    )
    if server:
        await cog._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)

async def _apply_speed_from_panel(cog, interaction: discord.Interaction, speed: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, rate=speed))
        desc = f"A velocidade padrão do servidor agora é `{speed}`."
        history_entry = cog._server_history_text(interaction, "a velocidade padrão do servidor", speed)
        await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
    else:
        history_entry = cog._user_history_text(interaction, "a própria velocidade" if effective_user_id == interaction.user.id else "a velocidade", speed, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, rate=speed, history_entry=history_entry)
        desc = f"A velocidade do Edge de {effective_user_name} agora é `{speed}`." if effective_user_id != interaction.user.id else f"A sua velocidade do Edge agora é `{speed}`."
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id if not server else interaction.user.id,
        server=server,
        panel_kind="server" if server else "user",
        last_changes=last_changes,
        message_id=message_id,
        target_user_name=effective_user_name if not server else None,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Configuração de TTS atualizada",
        description=desc,
        target_message=panel_message,
    )
    if server:
        await cog._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)

async def _apply_pitch_from_panel(cog, interaction: discord.Interaction, pitch: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, pitch=pitch))
        desc = f"O tom padrão do servidor agora é `{pitch}`."
        history_entry = cog._server_history_text(interaction, "o tom padrão do servidor", pitch)
        await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("server_last_changes", []) or [])
    else:
        history_entry = cog._user_history_text(interaction, "o próprio tom" if effective_user_id == interaction.user.id else "o tom", pitch, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, pitch=pitch, history_entry=history_entry)
        desc = f"O tom do Edge de {effective_user_name} agora é `{pitch}`." if effective_user_id != interaction.user.id else f"O seu tom do Edge agora é `{pitch}`."
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])

    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id if not server else interaction.user.id,
        server=server,
        panel_kind="server" if server else "user",
        last_changes=last_changes,
        message_id=message_id,
        target_user_name=effective_user_name if not server else None,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Configuração de TTS atualizada",
        description=desc,
        target_message=panel_message,
    )
    if server:
        await cog._announce_panel_change(interaction, title="Configuração de TTS atualizada", description=desc)

async def _apply_gcloud_language_from_modal(cog, interaction: discord.Interaction, language: str, *, server: bool, panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    await cog._apply_gcloud_language_from_panel(interaction, language, server=server, source_panel_message=panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

async def _apply_gcloud_voice_from_modal(cog, interaction: discord.Interaction, voice_name: str, *, server: bool, panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    await cog._apply_gcloud_voice_from_panel(interaction, voice_name, server=server, source_panel_message=panel_message, target_user_id=target_user_id, target_user_name=target_user_name)

async def _apply_gcloud_language_from_panel(cog, interaction: discord.Interaction, language: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await cog._respond(interaction, embed=cog._make_embed('Sem permissão', 'Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.', ok=False), ephemeral=True)
        return
    db = cog._get_db()
    if db is None:
        await cog._respond(interaction, embed=cog._make_embed('Banco indisponível', 'Não consegui acessar o banco de dados agora.', ok=False), ephemeral=True)
        return
    value, error = cog._validate_gcloud_language_input(language)
    if error or value is None:
        await cog._respond(interaction, embed=cog._make_embed('Idioma inválido', error or 'Idioma inválido.', ok=False), ephemeral=True)
        return
    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)

    catalog = await cog._load_gcloud_voices()
    current_voice = cog._get_current_gcloud_voice(interaction.guild.id, effective_user_id, server=server)
    updates: dict[str, str] = {'gcloud_language': value}
    adjusted_voice = ''
    if catalog and not cog._gcloud_voice_matches_language(current_voice, value):
        adjusted_voice = cog._pick_first_gcloud_voice_for_language(catalog, value)
        if adjusted_voice:
            updates['gcloud_voice'] = adjusted_voice

    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, **updates))
        desc = f"O idioma do Google Cloud do servidor agora é `{value}`."
        history_entry = cog._server_history_text(interaction, 'o idioma do Google Cloud do servidor', value)
        if adjusted_voice:
            desc += f" A voz do Google foi ajustada para `{adjusted_voice}` para combinar com o idioma."
            history_entry = cog._server_history_text(interaction, 'o idioma do Google Cloud do servidor', f'{value} (voz ajustada para {adjusted_voice})')
        await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get('server_last_changes', []) or [])
    else:
        history_entry = cog._user_history_text(interaction, 'o próprio idioma do Google' if effective_user_id == interaction.user.id else 'o idioma do Google', value, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
        if adjusted_voice:
            history_entry = cog._user_history_text(interaction, 'o próprio idioma do Google' if effective_user_id == interaction.user.id else 'o idioma do Google', f'{value} (voz ajustada para {adjusted_voice})', message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, history_entry=history_entry, **updates)
        desc = f"O idioma do Google Cloud de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"O seu idioma do Google Cloud agora é `{value}`."
        if adjusted_voice:
            desc += f" A voz do Google foi ajustada para `{adjusted_voice}` para combinar com o idioma."
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get('user_last_changes', []) or [])
    embed = await cog._build_settings_embed(interaction.guild.id, effective_user_id if not server else interaction.user.id, server=server, panel_kind='server' if server else 'user', last_changes=last_changes, message_id=message_id, target_user_name=effective_user_name if not server else None, viewer_user_id=interaction.user.id)
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(interaction, embed=embed, view=view, title='Configuração de TTS atualizada', description=desc, target_message=panel_message)
    if server:
        await cog._announce_panel_change(interaction, title='Configuração de TTS atualizada', description=desc)

async def _apply_gcloud_voice_from_panel(cog, interaction: discord.Interaction, voice_name: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await cog._respond(interaction, embed=cog._make_embed('Sem permissão', 'Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.', ok=False), ephemeral=True)
        return
    db = cog._get_db()
    if db is None:
        await cog._respond(interaction, embed=cog._make_embed('Banco indisponível', 'Não consegui acessar o banco de dados agora.', ok=False), ephemeral=True)
        return
    value, error = cog._validate_gcloud_voice_input(voice_name)
    if error or value is None:
        await cog._respond(interaction, embed=cog._make_embed('Voz inválida', error or 'Voz inválida.', ok=False), ephemeral=True)
        return
    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, gcloud_voice=value))
        desc = f"A voz do Google Cloud do servidor agora é `{value}`."
        history_entry = cog._server_history_text(interaction, 'a voz do Google Cloud do servidor', value)
        await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get('server_last_changes', []) or [])
    else:
        history_entry = cog._user_history_text(interaction, 'a própria voz do Google' if effective_user_id == interaction.user.id else 'a voz do Google', value, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, gcloud_voice=value, history_entry=history_entry)
        desc = f"A voz do Google Cloud de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"A sua voz do Google Cloud agora é `{value}`."
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get('user_last_changes', []) or [])
    embed = await cog._build_settings_embed(interaction.guild.id, effective_user_id if not server else interaction.user.id, server=server, panel_kind='server' if server else 'user', last_changes=last_changes, message_id=message_id, target_user_name=effective_user_name if not server else None, viewer_user_id=interaction.user.id)
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(interaction, embed=embed, view=view, title='Configuração de TTS atualizada', description=desc, target_message=panel_message)
    if server:
        await cog._announce_panel_change(interaction, title='Configuração de TTS atualizada', description=desc)

async def _apply_gcloud_speed_from_panel(cog, interaction: discord.Interaction, speed: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await cog._respond(interaction, embed=cog._make_embed('Sem permissão', 'Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.', ok=False), ephemeral=True)
        return
    db = cog._get_db()
    if db is None:
        await cog._respond(interaction, embed=cog._make_embed('Banco indisponível', 'Não consegui acessar o banco de dados agora.', ok=False), ephemeral=True)
        return
    value = cog._normalize_gcloud_rate_value(speed)
    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, gcloud_rate=value))
        desc = f"A velocidade do Google Cloud do servidor agora é `{value}`."
        history_entry = cog._server_history_text(interaction, 'a velocidade do Google Cloud do servidor', value)
        await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get('server_last_changes', []) or [])
    else:
        history_entry = cog._user_history_text(interaction, 'a própria velocidade do Google' if effective_user_id == interaction.user.id else 'a velocidade do Google', value, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, gcloud_rate=value, history_entry=history_entry)
        desc = f"A velocidade do Google Cloud de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"A sua velocidade do Google Cloud agora é `{value}`."
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get('user_last_changes', []) or [])
    embed = await cog._build_settings_embed(interaction.guild.id, effective_user_id if not server else interaction.user.id, server=server, panel_kind='server' if server else 'user', last_changes=last_changes, message_id=message_id, target_user_name=effective_user_name if not server else None, viewer_user_id=interaction.user.id)
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(interaction, embed=embed, view=view, title='Configuração de TTS atualizada', description=desc, target_message=panel_message)
    if server:
        await cog._announce_panel_change(interaction, title='Configuração de TTS atualizada', description=desc)

async def _apply_gcloud_pitch_from_panel(cog, interaction: discord.Interaction, pitch: str, *, server: bool, source_panel_message: discord.Message | None = None, target_user_id: int | None = None, target_user_name: str | None = None):
    if server and not interaction.user.guild_permissions.kick_members:
        await cog._respond(interaction, embed=cog._make_embed('Sem permissão', 'Você precisa da permissão `Expulsar Membros` para alterar as configurações do servidor por esse painel.', ok=False), ephemeral=True)
        return
    db = cog._get_db()
    if db is None:
        await cog._respond(interaction, embed=cog._make_embed('Banco indisponível', 'Não consegui acessar o banco de dados agora.', ok=False), ephemeral=True)
        return
    value = cog._normalize_gcloud_pitch_value(pitch)
    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(interaction, server=server, message_id=message_id, target_user_id=target_user_id, target_user_name=target_user_name)
    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, gcloud_pitch=value))
        desc = f"O tom do Google Cloud do servidor agora é `{value}`."
        history_entry = cog._server_history_text(interaction, 'o tom do Google Cloud do servidor', value)
        await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get('server_last_changes', []) or [])
    else:
        history_entry = cog._user_history_text(interaction, 'o próprio tom do Google' if effective_user_id == interaction.user.id else 'o tom do Google', value, message_id=message_id, target_user_id=effective_user_id, target_user_name=effective_user_name)
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, gcloud_pitch=value, history_entry=history_entry)
        desc = f"O tom do Google Cloud de {effective_user_name} agora é `{value}`." if effective_user_id != interaction.user.id else f"O seu tom do Google Cloud agora é `{value}`."
        cog._append_public_panel_history(message_id, history_entry)
        last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get('user_last_changes', []) or [])
    embed = await cog._build_settings_embed(interaction.guild.id, effective_user_id if not server else interaction.user.id, server=server, panel_kind='server' if server else 'user', last_changes=last_changes, message_id=message_id, target_user_name=effective_user_name if not server else None, viewer_user_id=interaction.user.id)
    view_target_user_id = None if server or is_public_user_panel else effective_user_id
    view_target_user_name = None if server or is_public_user_panel else effective_user_name
    view = cog._build_panel_view(0 if message_id in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=server, target_user_id=view_target_user_id, target_user_name=view_target_user_name)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(interaction, embed=embed, view=view, title='Configuração de TTS atualizada', description=desc, target_message=panel_message)
    if server:
        await cog._announce_panel_change(interaction, title='Configuração de TTS atualizada', description=desc)

async def _apply_spoken_name_from_modal(
    cog,
    interaction: discord.Interaction,
    spoken_name: str,
    *,
    panel_message: discord.Message | None = None,
    target_user_id: int | None = None,
    target_user_name: str | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Comando indisponível", "Esse ajuste só pode ser usado dentro de um servidor.", ok=False),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(
        interaction,
        server=False,
        message_id=message_id,
        target_user_id=target_user_id,
        target_user_name=target_user_name,
    )

    validated_name, validation_error = cog._validate_spoken_name_input(spoken_name)
    if validation_error:
        await interaction.response.send_message(
            embed=cog._make_embed("Apelido inválido", validation_error, ok=False),
            ephemeral=True,
        )
        return

    if validated_name:
        history_entry = cog._user_history_text(
            interaction,
            "o apelido falado" if effective_user_id != interaction.user.id else "o próprio apelido falado",
            f"`{validated_name}`",
            message_id=message_id,
            target_user_id=effective_user_id,
            target_user_name=effective_user_name,
        )
        await cog._set_user_tts_and_refresh(
            interaction.guild.id,
            effective_user_id,
            speaker_name=validated_name,
            history_entry=history_entry,
        )
        desc = f"O apelido falado de {effective_user_name} agora é `{validated_name}`." if effective_user_id != interaction.user.id else f"O seu apelido falado agora é `{validated_name}`."
    else:
        if effective_user_id == interaction.user.id:
            history_entry = cog._encode_public_owner_history(
                effective_user_id,
                cog._panel_actor_name(interaction),
                "removeu o próprio apelido falado personalizado",
            )
            desc = "O seu apelido falado voltou para o modo automático."
        else:
            history_entry = f"{cog._panel_actor_name(interaction)} removeu o apelido falado personalizado de {effective_user_name}"
            desc = f"O apelido falado de {effective_user_name} voltou para o modo automático."
        await cog._set_user_tts_and_refresh(
            interaction.guild.id,
            effective_user_id,
            speaker_name="",
            history_entry=history_entry,
        )

    cog._append_public_panel_history(message_id, history_entry)
    last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, effective_user_id))).get("user_last_changes", []) or [])
    embed = await cog._build_settings_embed(
        interaction.guild.id,
        effective_user_id,
        server=False,
        panel_kind="user",
        last_changes=last_changes,
        message_id=message_id,
        target_user_name=effective_user_name,
        viewer_user_id=interaction.user.id,
    )
    view_target_user_id = None if is_public_user_panel else effective_user_id
    view_target_user_name = None if is_public_user_panel else effective_user_name
    view = cog._build_panel_view(
        0 if message_id in cog._public_panel_states else interaction.user.id,
        interaction.guild.id,
        server=False,
        target_user_id=view_target_user_id,
        target_user_name=view_target_user_name,
    )
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Apelido falado atualizado",
        description=desc,
        target_message=panel_message,
    )

async def _apply_announce_author_from_panel(cog, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para usar esse comando.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message = source_panel_message or getattr(interaction, "message", None)
    await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, announce_author=bool(enabled)))
    desc = "Autor antes da frase ativado." if enabled else "Autor antes da frase desativado."
    history_entry = cog._server_history_text(interaction, "ativou o Autor antes da frase" if enabled else "desativou o Autor antes da frase")
    await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, server_last_change=history_entry))
    cog._append_public_panel_history(getattr(panel_message, "id", None), history_entry)
    panel_history = await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))
    last_changes = list((panel_history or {}).get("server_last_changes", []) or [])
    embed = await cog._build_settings_embed(
        interaction.guild.id,
        interaction.user.id,
        server=True,
        panel_kind="server",
        last_changes=last_changes,
        message_id=getattr(panel_message, "id", None),
        viewer_user_id=interaction.user.id,
    )
    view = cog._build_panel_view(0 if getattr(panel_message, "id", None) in cog._public_panel_states else interaction.user.id, interaction.guild.id, server=True)
    if panel_message is not None:
        view.message = panel_message
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Modo de TTS atualizado",
        description=desc,
        target_message=panel_message,
    )
    await cog._announce_panel_change(interaction, title="Modo de TTS atualizado", description=desc)

async def _apply_only_target_from_panel(cog, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para usar esse comando.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message = source_panel_message or getattr(interaction, "message", None)
    await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, only_target_user=bool(enabled)))
    desc = "Modo Cuca ativado." if enabled else "Modo Cuca desativado."
    history_entry = cog._toggle_history_text(interaction, "ativou o Modo Cuca" if enabled else "desativou o Modo Cuca")
    await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, toggle_last_change=history_entry))
    cog._append_public_panel_history(getattr(getattr(interaction, "message", None), "id", None), history_entry)
    last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("toggle_last_changes", []) or [])
    embed = await cog._build_toggle_embed(interaction.guild.id, interaction.user.id, last_changes=last_changes, message_id=getattr(getattr(interaction, "message", None), "id", None))
    view = cog._build_toggle_view(0 if getattr(getattr(interaction, "message", None), "id", None) in cog._public_panel_states else interaction.user.id, interaction.guild.id)
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Modo de TTS atualizado",
        description=desc,
        target_message=panel_message,
    )
    await cog._announce_panel_change(interaction, title="Modo de TTS atualizado", description=desc)

async def _apply_block_voice_bot_from_panel(cog, interaction: discord.Interaction, enabled: bool, source_panel_message: discord.Message | None = None):
    if not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message(
            embed=cog._make_embed(
                "Sem permissão",
                "Você precisa da permissão `Expulsar Membros` para usar esse painel.",
                ok=False,
            ),
            ephemeral=True,
        )
        return

    db = cog._get_db()
    if db is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Banco indisponível", "Não consegui acessar o banco de dados agora.", ok=False),
            ephemeral=True,
        )
        return

    panel_message = source_panel_message or getattr(interaction, "message", None)
    await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, block_voice_bot=bool(enabled)))
    desc = f"Bloqueio por outro bot {'ativado' if enabled else 'desativado'}."
    history_entry = cog._toggle_history_text(interaction, "ativou o Bloqueio por outro bot" if enabled else "desativou o Bloqueio por outro bot")
    await cog._maybe_await(db.set_guild_panel_last_change(interaction.guild.id, toggle_last_change=history_entry))
    cog._append_public_panel_history(getattr(getattr(interaction, "message", None), "id", None), history_entry)
    last_changes = list((await cog._maybe_await(db.get_panel_history(interaction.guild.id, interaction.user.id))).get("toggle_last_changes", []) or [])
    embed = await cog._build_toggle_embed(interaction.guild.id, interaction.user.id, last_changes=last_changes, message_id=getattr(getattr(interaction, "message", None), "id", None))
    view = cog._build_toggle_view(0 if getattr(getattr(interaction, "message", None), "id", None) in cog._public_panel_states else interaction.user.id, interaction.guild.id)
    await cog._panel_update_after_change(
        interaction,
        embed=embed,
        view=view,
        title="Modo de TTS atualizado",
        description=desc,
        target_message=panel_message,
    )
    await cog._announce_panel_change(interaction, title="Modo de TTS atualizado", description=desc)

    if enabled:
        await cog._disconnect_if_blocked(interaction.guild)

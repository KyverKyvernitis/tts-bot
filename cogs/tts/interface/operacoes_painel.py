from __future__ import annotations

import traceback

import discord


DESCRICAO_LANCADOR_TTS = (
    "Tem dois modos de texto para voz, cada um com um prefixo diferente. "
    "Escolha qual quer configurar"
)


async def salvar_atualizacoes_modal_tts(
    cog: "TTSVoice",
    interaction: discord.Interaction,
    *,
    source_panel_message: discord.Message | None,
    server: bool,
    updates: dict[str, object],
    success_title: str,
    success_description: str,
    target_user_id: int | None = None,
    target_user_name: str | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=cog._make_embed("Comando indisponível", "Esse ajuste só pode ser usado dentro de um servidor.", ok=False),
            ephemeral=True,
        )
        return
    if server and not getattr(getattr(interaction.user, "guild_permissions", None), "kick_members", False):
        await interaction.response.send_message(
            embed=cog._make_embed("Sem permissão", "Você precisa da permissão `Expulsar Membros` para alterar o TTS do servidor.", ok=False),
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

    clean_updates = {k: v for k, v in (updates or {}).items() if v is not None}
    if not clean_updates:
        await interaction.response.send_message(
            embed=cog._make_embed("Nada mudou", "Nenhum ajuste foi alterado.", ok=True),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    panel_message, message_id = cog._resolve_public_panel_message(interaction, source_panel_message)
    effective_user_id, effective_user_name, is_public_user_panel = cog._resolve_panel_target_user(
        interaction,
        server=server,
        message_id=message_id,
        target_user_id=target_user_id,
        target_user_name=target_user_name,
    )

    if server:
        await cog._maybe_await(db.set_guild_tts_defaults(interaction.guild.id, **clean_updates))
        panel_kind = "server"
    else:
        await cog._set_user_tts_and_refresh(interaction.guild.id, effective_user_id, **clean_updates)
        panel_kind = "user"

    state = cog._public_panel_states.get(message_id or 0, {}) if message_id else {}
    if panel_message is not None and state.get("panel_kind") == "launcher":
        view = cog._build_public_tts_launcher_view(
            interaction.guild.id,
            owner_id=int(state.get("owner_id", 0) or 0),
            timeout=300,
        )
        view.message = panel_message
        await cog._panel_update_after_change(
            interaction,
            embed=cog._make_embed("TTS", DESCRICAO_LANCADOR_TTS, ok=True),
            view=view,
            title=success_title,
            description=success_description,
            target_message=panel_message,
        )
        if server:
            await cog._announce_panel_change(interaction, title=success_title, description=success_description, target_message=panel_message)
        return

    should_edit_panel = bool(panel_message is not None)
    if should_edit_panel:
        embed = await cog._build_settings_embed(
            interaction.guild.id,
            effective_user_id if not server else interaction.user.id,
            server=server,
            panel_kind=panel_kind,
            target_user_name=effective_user_name if not server else None,
            viewer_user_id=interaction.user.id,
        )
        view_target_user_id = None if server or is_public_user_panel else effective_user_id
        view_target_user_name = None if server or is_public_user_panel else effective_user_name
        view = cog._build_panel_view(
            0 if message_id in cog._public_panel_states else interaction.user.id,
            interaction.guild.id,
            server=server,
            target_user_id=view_target_user_id,
            target_user_name=view_target_user_name,
        )
        if panel_message is not None:
            view.message = panel_message
        await cog._panel_update_after_change(
            interaction,
            embed=embed,
            view=view,
            title=success_title,
            description=success_description,
            target_message=panel_message,
        )
    else:
        await cog._send_tts_notice(
            interaction,
            title=success_title,
            description=success_description,
            ok=True,
        )

    if server:
        await cog._announce_panel_change(interaction, title=success_title, description=success_description, target_message=panel_message)


async def enviar_modal_configuracao_com_fallback(interaction: discord.Interaction, guided_factory, fallback_factory, *, context: str) -> None:
    try:
        await interaction.response.send_modal(guided_factory())
        return
    except Exception as e:
        print(f"[tts_modal] modal guiado falhou em {context}: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)
        if interaction.response.is_done():
            try:
                await interaction.followup.send("Não consegui abrir esse formulário agora.", ephemeral=True)
            except Exception:
                pass
            return
    try:
        await interaction.response.send_modal(fallback_factory())
    except Exception as e:
        print(f"[tts_modal] fallback também falhou em {context}: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)
        try:
            await interaction.response.send_message("Não consegui abrir esse formulário agora.", ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send("Não consegui abrir esse formulário agora.", ephemeral=True)
            except Exception:
                pass


async def reiniciar_selecao_lancador_publico(interaction: discord.Interaction, panel) -> None:
    """Re-renderiza o launcher público para limpar a opção marcada no select.

    No mobile do Discord, depois de escolher uma opção, o select pode ficar preso
    visualmente no último valor. Como abrir modal consome a resposta da interação,
    esse reset precisa ser feito com message.edit normal, sem usar interaction.response.
    """
    message = getattr(interaction, "message", None)
    guild = getattr(interaction, "guild", None)
    cog = getattr(panel, "cog", None)
    if message is None or guild is None or cog is None:
        return
    message_id = int(getattr(message, "id", 0) or 0)
    state = getattr(cog, "_public_panel_states", {}).get(message_id, {}) if message_id else {}
    if state.get("panel_kind") != "launcher":
        return

    try:
        view = cog._build_public_tts_launcher_view(
            guild.id,
            owner_id=int(state.get("owner_id", 0) or 0),
            timeout=300,
        )
        view.message = message
        await cog._edit_panel_message_payload(
            message,
            embed=cog._make_embed("TTS", DESCRICAO_LANCADOR_TTS, ok=True),
            view=view,
        )
    except Exception as e:
        print(f"[tts_panel] falha ao limpar select do launcher: {e!r}")
        traceback.print_exception(type(e), e, e.__traceback__)

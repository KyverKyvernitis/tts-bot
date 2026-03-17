import discord


def make_embed(title: str, description: str, *, ok: bool = True) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.green() if ok else discord.Color.red())


def build_expired_panel_embed(*, slash_mention: str, prefix_hint: str) -> discord.Embed:
    return make_embed("Esse painel expirou", f"Use o comando de barra {slash_mention} ou prefixo {prefix_hint} para abrir outro painel.", ok=False)


def build_toggle_embed(*, auto_leave_enabled: bool, only_target_enabled: bool, block_voice_bot_enabled: bool, history_text: str = "") -> discord.Embed:
    embed = discord.Embed(title="Painel de toggles do TTS", description="Use os botões abaixo para ligar ou desligar os modos especiais do TTS.", color=discord.Color.blurple())
    embed.add_field(name="Bloqueio por outro bot", value="`Ativado`" if bool(block_voice_bot_enabled) else "`Desativado`", inline=True)
    embed.add_field(name="Modo Cuca", value="`Ativado`" if bool(only_target_enabled) else "`Desativado`", inline=True)
    embed.add_field(name="Auto leave", value="`Ativado`" if bool(auto_leave_enabled) else "`Desativado`", inline=True)
    if history_text:
        embed.add_field(name="Últimas alterações", value=history_text, inline=False)
    return embed


def status_badge(value: bool, *, on: str = "Ativo", off: str = "Inativo") -> str:
    return f"🟢 {on}" if bool(value) else f"⚫ {off}"


def status_source_badge(source: str) -> str:
    source = str(source or "Servidor")
    return f"👤 {source}" if source == "Usuário" else f"🏠 {source}"


def status_engine_label(engine: str) -> str:
    value = str(engine or "gtts").lower()
    if value == "edge":
        return "🗣️ Edge"
    if value == "gcloud":
        return "☁️ Google Cloud"
    return "🌐 gTTS"


def status_voice_channel_text(guild: discord.Guild | None, target_user_id: int) -> str:
    if guild is None:
        return "Não disponível"
    member = guild.get_member(int(target_user_id or 0))
    voice_state = getattr(member, "voice", None)
    channel = getattr(voice_state, "channel", None)
    if channel is None:
        return "Fora de call"
    return getattr(channel, "mention", None) or f"`{getattr(channel, 'name', 'Desconhecido')}`"


def spoken_name_status_text(*, active_name: str, active_source: str, custom_name: str = "") -> tuple[str, str]:
    if custom_name:
        return f"`{active_name}` (personalizado)", active_source
    if active_source == "apelido do servidor":
        return f"`{active_name}` (apelido do servidor)", active_source
    if active_source == "nome de usuário":
        return f"`{active_name}` (nome de usuário)", active_source
    return f"`{active_name}` (padrão)", active_source


def build_status_embed(*, member: discord.abc.User | None, target_name: str, user_id: int, viewer_user_id: int, public: bool, is_connected: bool, is_playing: bool, queue_size: int, resolved: dict, user_settings: dict, user_channel: str, bot_channel: str, spoken_name_text: str, history_text: str, google_language_default: str, google_voice_default: str, google_rate_default: str, google_pitch_default: str) -> discord.Embed:
    if public:
        title = f"📡 Status de TTS de {target_name}"
        description = f"Resumo público das configurações atuais de TTS de {target_name}."
    elif int(user_id or 0) != int(viewer_user_id or user_id or 0):
        title = f"📡 Status de TTS de {target_name}"
        description = f"Resumo das configurações atuais de TTS de {target_name}."
    else:
        title = "📡 Status do TTS"
        description = "Resumo das suas configurações atuais de TTS neste servidor."
    color = discord.Color.green() if is_playing else (discord.Color.blurple() if is_connected else discord.Color.orange())
    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    if member is not None:
        avatar = getattr(getattr(member, "display_avatar", None), "url", None)
        if avatar:
            embed.set_thumbnail(url=avatar)
    queue_label = f"{queue_size} item" + ("" if queue_size == 1 else "s")
    summary_bits = [status_badge(is_connected, on="Conectado", off="Desconectado"), status_badge(is_playing, on="Falando", off="Em espera"), f"📚 Fila: `{queue_label}`", f"🎙️ Engine: `{resolved.get('engine', 'gtts')}`"]
    embed.add_field(name="Resumo rápido", value=" • ".join(summary_bits), inline=False)
    customized_keys = [label for key, label in (("engine", "engine"), ("voice", "voz do Edge"), ("language", "idioma do gTTS"), ("rate", "velocidade do Edge"), ("pitch", "tom do Edge"), ("gcloud_voice", "voz do Google"), ("gcloud_language", "idioma do Google"), ("gcloud_rate", "velocidade do Google"), ("gcloud_pitch", "tom do Google"), ("speaker_name", "apelido falado")) if str((user_settings or {}).get(key, "") or "").strip()]
    source_line = "**Origem:** usando padrões do servidor" if not customized_keys else "**Personalizado:** " + ", ".join(customized_keys)
    embed.add_field(name="🎛️ Configuração ativa", value=(f"**Engine:** {status_engine_label(str(resolved.get('engine', 'gtts')))}\n" f"**gTTS idioma:** `{resolved.get('gtts_language', resolved.get('language', 'Não definido'))}`\n" f"**Edge voz:** `{resolved.get('edge_voice', resolved.get('voice', 'Não definido'))}`\n" f"**Edge velocidade:** `{resolved.get('edge_rate', resolved.get('rate', '+0%'))}`\n" f"**Edge tom:** `{resolved.get('edge_pitch', resolved.get('pitch', '+0Hz'))}`\n" f"**Google idioma:** `{resolved.get('gcloud_language', google_language_default)}`\n" f"**Google voz:** `{resolved.get('gcloud_voice', google_voice_default)}`\n" f"**Google velocidade:** `{resolved.get('gcloud_rate', google_rate_default)}`\n" f"**Google tom:** `{resolved.get('gcloud_pitch', google_pitch_default)}`\n" f"**Apelido falado:** {spoken_name_text}\n" f"{source_line}"), inline=False)
    embed.add_field(name="🛰️ Estado atual", value=(f"**Você:** {user_channel}\n" f"**Bot:** {bot_channel}\n" f"**Conexão:** {status_badge(is_connected, on='Conectado', off='Desconectado')}\n" f"**Reprodução:** {status_badge(is_playing, on='Falando agora', off='Parado')}\n" f"**Fila:** `{queue_label}`"), inline=False)
    if history_text:
        embed.add_field(name="🕘 Últimas alterações", value=history_text, inline=False)
    footer_text = "Sincronizado com o histórico do seu tts menu." if not public and int(user_id or 0) == int(viewer_user_id or 0) else "Sincronizado com o histórico do tts menu."
    embed.set_footer(text=footer_text)
    return embed


def build_settings_embed(*, title: str, description: str, resolved: dict, guild_defaults: dict, history_text: str, server: bool, panel_kind: str, spoken_name_text: str | None, google_language_default: str, google_voice_default: str, google_rate_default: str, google_pitch_default: str, google_prefix_default: str) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
    embed.add_field(name="Voz do Edge", value=f"`{resolved.get('edge_voice', resolved.get('voice', 'Não definido'))}`", inline=True)
    embed.add_field(name="Idioma do gTTS", value=f"`{resolved.get('gtts_language', resolved.get('language', 'Não definido'))}`", inline=True)
    embed.add_field(name="Velocidade do Edge", value=f"`{resolved.get('edge_rate', resolved.get('rate', '+0%'))}`", inline=True)
    embed.add_field(name="Tom do Edge", value=f"`{resolved.get('edge_pitch', resolved.get('pitch', '+0Hz'))}`", inline=True)
    embed.add_field(name="Idioma do Google", value=f"`{resolved.get('gcloud_language', google_language_default)}`", inline=True)
    embed.add_field(name="Voz do Google", value=f"`{resolved.get('gcloud_voice', google_voice_default)}`", inline=True)
    embed.add_field(name="Velocidade do Google", value=f"`{resolved.get('gcloud_rate', google_rate_default)}`", inline=True)
    embed.add_field(name="Tom do Google", value=f"`{resolved.get('gcloud_pitch', google_pitch_default)}`", inline=True)
    if not server and spoken_name_text is not None:
        embed.add_field(name="Apelido falado", value=spoken_name_text, inline=True)
    if server:
        embed.add_field(name="Prefixo do bot", value=f"`{guild_defaults.get('bot_prefix', '_')}`", inline=True)
        embed.add_field(name="Prefixo do modo gTTS", value=f"`{guild_defaults.get('gtts_prefix', guild_defaults.get('tts_prefix', '.'))}`", inline=True)
        embed.add_field(name="Prefixo do modo Edge", value=f"`{guild_defaults.get('edge_prefix', ',')}`", inline=True)
        google_prefix = guild_defaults.get('gcloud_prefix', google_prefix_default)
        embed.add_field(name="Prefixo do Google", value=f"`{google_prefix}`", inline=True)
        embed.add_field(name="Autor antes da frase", value="`Ativado`" if bool(guild_defaults.get('announce_author', False)) else "`Desativado`", inline=True)
    if history_text:
        embed.add_field(name="Últimas alterações", value=history_text, inline=False)
    embed.set_footer(text="Os ajustes de gTTS, Edge e Google Cloud ficam salvos no banco." if server or panel_kind == "toggle" else "As alterações desse painel ficam salvas para o usuário correspondente.")
    return embed

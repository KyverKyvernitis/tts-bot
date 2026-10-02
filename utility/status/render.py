from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord

from .data import StatusSnapshot, engine_label, first_present, integer, number


ROWS_PER_PAGE = 5
PAGE_TEXT_BUDGET = 2700
TTS_SECTIONS = {"summary": "Resumo", "engines": "Engines", "voice": "Voz e conexão"}
_ROUTE_REASONS = {
    "disabled_or_unconfigured": "worker desativado ou não configurado",
    "worker_base_unavailable": "endereço do worker indisponível",
    "worker_offline_or_not_ready": "worker offline ou ainda não pronto",
    "tts_agent_not_ready": "TTS Agent ainda não está pronto",
    "health_error": "verificação do worker falhou",
    "health_error_degraded": "verificação instável; rota anterior preservada",
    "health_ok": "worker saudável", "adaptive_disabled": "roteamento adaptativo desativado",
    "always_worker_engine": "engine dedicada ao worker",
    "gtts_short_text_vps_fastpath": "texto curto processado na VPS",
    "worker_ready": "worker pronto", "worker_busy": "worker ocupado",
    "recent_first_audio": "comparação de latência até o primeiro áudio",
    "real_request_sample": "amostra real para medir o worker",
    "local_until_measured": "VPS até obter medições comparáveis",
    "not_checked": "ainda sem verificação",
    "worker_stream_unavailable": "stream do worker indisponível",
    "worker_fallback": "worker falhou; síntese concluída na VPS",
    "piper_legacy_worker": "Piper legado executado no worker",
    "piper_fallback": "Piper indisponível; engine alternativa usada",
    "edge_circuit_open": "Edge em pausa; gTTS usado",
    "edge_fallback": "Edge falhou; gTTS usado",
    "stream_fallback": "stream interrompido; áudio gerado por arquivo",
    "synth_ok": "geração concluída",
}
_VOICE_STATES = {
    "voice_handoff_received_waiting_transfer": "handoff recebido; aguardando posse da voz",
    "voice_transfer_staged_waiting_vps_release": "transferência preparada; dono ainda VPS",
    "voice_ownership_granted_waiting_connection": "posse liberada; aguardando conexão",
    "voice_handoff_registered_dry_run": "handoff recebido; conexão direta não iniciada",
    "shared_voice_session_registered": "sessão compartilhada registrada",
    "voice_connection_dry_run_ready": "conexão de teste pronta",
    "not_ready": "preparando",
}


@dataclass(frozen=True, slots=True)
class Card:
    text: str
    thumbnail_url: str | None = None


@dataclass(frozen=True, slots=True)
class PanelPage:
    header: str
    cards: tuple[Card, ...]
    footer: str
    accent: discord.Color


def safe_text(value: Any, limit: int = 160) -> str:
    text = str(value if value is not None else "").replace("\n", " ").replace("\r", " ").strip()
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text)) or "—"


def fmt_int(value: Any) -> str:
    parsed = integer(value)
    if parsed is None:
        return "indisponível"
    if parsed >= 10 ** 15:
        try:
            return f"{parsed:.2e}"
        except OverflowError:
            return "valor fora da faixa"
    return f"{parsed:,}".replace(",", ".")


def fmt_ms(value: Any) -> str:
    parsed = number(value)
    if parsed is None:
        return "sem amostras"
    return f"{parsed:.0f} ms" if parsed >= 10 else f"{parsed:.2f} ms"


def fmt_bytes(value: Any) -> str:
    parsed = number(value)
    if parsed is None:
        return "indisponível"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if parsed < 1024 or unit == "TB":
            return f"{int(parsed)} B" if unit == "B" else f"{parsed:.1f} {unit}"
        parsed /= 1024
    return "indisponível"


def fmt_bool(value: Any) -> str:
    return "sim" if value is True else "não" if value is False else "indisponível"


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).strftime("%H:%M:%S UTC")


def reason_label(value: Any) -> str:
    reason = str(value or "")
    if reason.startswith("vps_faster:"):
        return "VPS apresentou menor latência"
    return safe_text(_ROUTE_REASONS.get(reason, reason or "sem decisão registrada"), 140)


def _chunks(cards: list[Card]) -> list[tuple[Card, ...]]:
    pages: list[tuple[Card, ...]] = []
    current: list[Card] = []
    length = 0
    for card in cards:
        # Cards maiores são divididos em linhas, nunca descartados.
        blocks: list[str] = []
        block = ""
        for line in card.text.splitlines():
            # Valores inesperados não podem criar uma linha que exceda o limite.
            pieces = [line[index:index + PAGE_TEXT_BUDGET] for index in range(0, len(line), PAGE_TEXT_BUDGET)] or [""]
            for piece in pieces:
                if block and len(block) + len(piece) + 1 > PAGE_TEXT_BUDGET:
                    blocks.append(block)
                    block = ""
                block = f"{block}\n{piece}" if block else piece
        if block:
            blocks.append(block)
        for text in blocks:
            if current and (len(current) >= ROWS_PER_PAGE or length + len(text) > PAGE_TEXT_BUDGET):
                pages.append(tuple(current))
                current = []
                length = 0
            current.append(Card(text, card.thumbnail_url))
            length += len(text)
    if current:
        pages.append(tuple(current))
    return pages or [(Card("Nenhum dado disponível."),)]


def server_pages(snapshot: StatusSnapshot) -> tuple[PanelPage, ...]:
    rows = snapshot.servers
    if not snapshot.servers_available:
        header = "## 🌐 Servidores\nDados de servidores indisponíveis."
    else:
        total_members = sum(row.members or 0 for row in rows)
        approximate = any(row.approximate_members or row.members is None for row in rows)
        members_label = "membros somados (aproximado)" if approximate else "membros somados"
        total_synths = sum(row.synths or 0 for row in rows)
        all_stats_available = snapshot.stats_available and all(row.stats_available for row in rows)
        synths_label = f"**{fmt_int(total_synths)}** sínteses · histórico persistido" if all_stats_available else "Total de sínteses históricas indisponível"
        header = f"## 🌐 Servidores\n**{fmt_int(len(rows))}** servidores · **{fmt_int(total_members)}** {members_label}\n{synths_label}"
    names: dict[str, int] = {}
    for row in rows:
        names[row.name.casefold()] = names.get(row.name.casefold(), 0) + 1
    cards: list[Card] = []
    for index, row in enumerate(rows, 1):
        suffix = f" · ID {row.guild_id}" if names[row.name.casefold()] > 1 else ""
        title = f"**{index}. {safe_text(row.name, 64)}**{suffix}"
        members = fmt_int(row.members) + (" (aprox.)" if row.approximate_members else "")
        lines = [title, f"{members} membros · {fmt_int(row.synths)} sínteses"]
        if row.engines:
            lines.append(" · ".join(f"{engine_label(key)} **{fmt_int(amount)}**" for key, amount in row.engines))
        cards.append(Card("\n".join(lines), row.icon_url))
    if not cards:
        cards.append(Card("Nenhum servidor encontrado no cache do bot." if snapshot.servers_available else "Tente atualizar o painel em alguns instantes."))
    chunks = _chunks(cards)
    return tuple(PanelPage(header, cards, f"Página {index + 1}/{len(chunks)} · coletado às {timestamp(snapshot.collected_at)}", discord.Color.blurple()) for index, cards in enumerate(chunks))


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _route_card(metrics: dict[str, Any]) -> Card:
    agent = _mapping(metrics.get("tts_agent"))
    enabled, ok = agent.get("enabled"), agent.get("ok")
    if enabled is False:
        status = "⚪ Worker desativado"
    elif ok is True:
        status = "🟢 Worker disponível"
    elif enabled is True and ok is False:
        status = "🟠 Worker indisponível · fallback na VPS"
    else:
        status = "⚪ Disponibilidade do worker desconhecida"
    age = number(agent.get("last_check_age_seconds"))
    if age is not None:
        status += f" · health há {age:.0f} s" + (" (desatualizado)" if age > 60 else "")
    else:
        status += " · sem verificação registrada"
    route_names = {"worker": "Worker", "vps": "VPS", "local": "VPS"}
    decision = route_names.get(str(agent.get("last_route_decision", "")), "sem decisão registrada")
    effective = route_names.get(str(agent.get("last_effective_route", "")), "sem síntese registrada")
    lines = ["### 🧭 Rota e disponibilidade", status,
             f"Última decisão: **{decision}** · {reason_label(agent.get('last_route_reason'))}",
             f"Última síntese concluída: **{effective}** · {reason_label(agent.get('last_effective_reason'))}"]
    if agent.get("worker_id"):
        lines.append(f"Worker: {safe_text(agent['worker_id'], 60)}" + (f" · versão {safe_text(agent['worker_version'], 30)}" if agent.get("worker_version") else ""))
    health_reason = agent.get("reason")
    if health_reason:
        lines.append(f"Estado de health/rota: {reason_label(health_reason)}")
    cooldown = number(agent.get("cooldown_remaining_seconds"))
    if cooldown and cooldown > 0:
        lines.append(f"Nova tentativa em {cooldown:.1f} s")
    if agent.get("last_error"):
        lines.append(f"Último erro: {safe_text(agent['last_error'], 160)}")
    return Card("\n".join(lines))


def _summary_cards(snapshot: StatusSnapshot) -> list[Card]:
    metrics = snapshot.tts
    agent = _mapping(metrics.get("tts_agent"))
    cards = [_route_card(metrics)]
    route_totals: dict[str, int] = {}
    valid_routes = _mapping(metrics.get("engines_by_route"))
    for route in ("worker", "vps"):
        source = valid_routes.get(route)
        if isinstance(source, dict) and not source:
            route_totals[route] = 0
    for metric in snapshot.engines:
        if metric.count is not None:
            route_totals[metric.route] = route_totals.get(metric.route, 0) + metric.count
    for route in tuple(route_totals):
        routed = valid_routes.get(route)
        invalid_source = isinstance(routed, dict) and any(not isinstance(value, dict) or integer(value.get("synth_count")) is None for value in routed.values())
        if invalid_source or any(metric.route == route and metric.count is None for metric in snapshot.engines):
            del route_totals[route]
    totals = " · ".join(f"{label}: **{fmt_int(route_totals.get(route))}**" for route, label in (("worker", "Worker"), ("vps", "VPS")))
    if "unknown" in route_totals:
        totals += f"\nRota não identificada (métricas anteriores): **{fmt_int(route_totals['unknown'])}**"
    failures = [item.failures for item in snapshot.engines if item.failures is not None]
    failures_available = bool(failures) and len(failures) == len(snapshot.engines)
    empty_engines_available = not snapshot.engines and (isinstance(metrics.get("engines"), dict) or all(isinstance(valid_routes.get(route), dict) for route in ("worker", "vps")))
    failure_total = sum(failures) if failures_available or empty_engines_available else None
    labels = list(dict.fromkeys(engine_label(item.key) for item in snapshot.engines))
    cards.append(Card("### 🔊 Sínteses desde o reinício\n" + totals + f"\nFalhas de geração: {fmt_int(failure_total)}" + ("\nEngines: " + " · ".join(safe_text(label, 64) for label in labels[:8]) if labels else "\nSem métricas de engines.")))
    cards.append(Card(
        "### ⏳ Filas e tempos\n"
        f"VPS agora: **{fmt_int(metrics.get('queued_items_current'))}** itens · {fmt_int(metrics.get('guild_states_current'))} servidores com estado ativo\n"
        f"Worker agora: **{fmt_int(agent.get('queue_active'))}/{fmt_int(agent.get('queue_limit'))}**\n"
        f"Acumulado: {fmt_int(metrics.get('queue_enqueued'))} enfileiradas · {fmt_int(metrics.get('queue_deduplicated'))} deduplicadas · {fmt_int(metrics.get('queue_dropped'))} descartadas\n"
        f"Média de espera: {fmt_ms(metrics.get('avg_queue_wait_ms'))} · despacho: {fmt_ms(metrics.get('avg_dispatch_ms'))}"
    ))
    hits, misses = integer(metrics.get("cache_hits")), integer(metrics.get("cache_misses"))
    rate = f"{100 * hits / (hits + misses):.1f}% de acerto" if hits is not None and misses is not None and hits + misses > 0 else "sem consultas" if hits == misses == 0 else "acerto indisponível"
    storage = snapshot.storage
    cards.append(Card(
        "### 📦 Cache e armazenamento\n"
        f"VPS: {fmt_int(hits)} hits · {fmt_int(misses)} misses · {fmt_int(metrics.get('cache_stores'))} gravações · {rate}\n"
        f"Worker: {fmt_int(metrics.get('worker_cache_lookup_hits'))} hits · {fmt_int(metrics.get('worker_cache_lookup_misses'))} misses\n"
        f"Consultas worker: {fmt_int(metrics.get('worker_cache_lookup_skipped'))} ignoradas · {fmt_int(metrics.get('worker_cache_lookup_errors'))} erros\n"
        f"Temporários: **{fmt_bytes(storage.total_bytes)}** · runtime/cache/credenciais {fmt_int(storage.runtime_files)}/{fmt_int(storage.cache_files)}/{fmt_int(storage.credential_files)}\n"
        f"-# Armazenamento coletado às {timestamp(storage.collected_at)}"
    ))
    worker_lines = ["### 📱 Atividade do TTS Agent"]
    advertised = agent.get("available_engines")
    if isinstance(advertised, (list, tuple)):
        worker_lines.append("Engines anunciadas: " + (" · ".join(safe_text(engine_label(value), 64) for value in advertised[:8]) or "nenhuma"))
    worker_lines.extend([
        f"Health: {fmt_int(metrics.get('tts_agent_health_ok'))} ok · {fmt_int(metrics.get('tts_agent_health_fail'))} falhas",
        f"Sínteses: {fmt_int(metrics.get('tts_agent_synth_attempts'))} tentativas · {fmt_int(metrics.get('tts_agent_synth_ok'))} concluídas · {fmt_int(metrics.get('tts_agent_synth_failed'))} falhas",
        f"Retries por ocupação: {fmt_int(metrics.get('tts_agent_busy_retries'))}",
        f"Decisões: {fmt_int(metrics.get('tts_agent_route_worker_samples'))} worker · {fmt_int(metrics.get('tts_agent_route_vps_samples'))} VPS",
    ])
    requested = first_present(agent, "last_requested_engine", metrics, "tts_agent_last_requested_engine")
    selected = first_present(agent, "last_selected_engine", metrics, "tts_agent_last_selected_engine")
    if requested or selected:
        cache = first_present(agent, "last_cache_hit", metrics, "tts_agent_last_cache_hit")
        cache_label = "hit" if cache is True else "miss" if cache is False else "indisponível"
        worker_lines.extend([
            f"Última resposta TTS: {safe_text(engine_label(requested), 64)} solicitado → {safe_text(engine_label(selected), 64)} usado",
            f"{safe_text(first_present(agent, 'last_audio_format', metrics, 'tts_agent_last_audio_format'), 30)} · {fmt_bytes(first_present(agent, 'last_audio_bytes', metrics, 'tts_agent_last_audio_bytes'))} · cache {cache_label} · {fmt_ms(first_present(agent, 'last_synth_ms', metrics, 'tts_agent_last_synth_ms'))}",
        ])
    if metrics.get("tts_agent_last_failure_reason"):
        worker_lines.append("Última falha: " + safe_text(metrics["tts_agent_last_failure_reason"], 120))
    cards.append(Card("\n".join(worker_lines)))
    return cards


def _engine_cards(snapshot: StatusSnapshot) -> list[Card]:
    cards: list[Card] = []
    for item in snapshot.engines:
        route = {"worker": "Worker", "vps": "VPS", "unknown": "rota não identificada"}[item.route]
        marker = "🔴" if item.consecutive_failures else "🟢" if item.consecutive_failures == 0 and item.count else "⚪"
        lines = [f"{marker} **{safe_text(engine_label(item.key), 64)}** · {route}",
                 f"{fmt_int(item.count)} sínteses · {fmt_int(item.failures)} falhas · média {fmt_ms(item.average_ms)}",
                 f"Última duração: {fmt_ms(item.last_ms)} · {fmt_int(item.consecutive_failures)} falhas seguidas"]
        if item.cache_hits is not None or item.cache_misses is not None:
            lines.append(f"Cache: {fmt_int(item.cache_hits)} hits · {fmt_int(item.cache_misses)} misses")
        if item.last_error:
            lines.append("Último erro: " + safe_text(item.last_error, 160))
        cards.append(Card("\n".join(lines)))
    return cards or [Card("Nenhuma métrica de engine registrada desde o reinício.")]


def _voice_cards(snapshot: StatusSnapshot) -> list[Card]:
    metrics = snapshot.tts
    agent = _mapping(metrics.get("tts_agent"))
    voice = _mapping(agent.get("voice_agent")) or _mapping(metrics.get("worker_voice_agent"))
    state = str(voice.get("state") or "sem resposta de health")
    direct = voice.get("direct_tts_ready")
    availability = any(voice.get(key) is True for key in ("shared_session_ready", "ok", "available"))
    marker = "🟢" if direct is True else "🟡" if availability else "⚪" if direct is None else "🔴"
    lines = ["### 🎛️ Worker Voice Agent", f"{marker} **{safe_text(_VOICE_STATES.get(state, state), 160)}**",
             f"Áudio direto pronto: {fmt_bool(direct)}",
             f"Recursos: música {fmt_bool(voice.get('music_ready'))} · TTS {fmt_bool(voice.get('tts_ready'))} · ducking {fmt_bool(voice.get('ducking_ready'))}",
             f"Atividade: {fmt_int(voice.get('session_count'))} sessões · {fmt_int(voice.get('handoff_count'))} handoffs · {fmt_int(voice.get('transfer_count'))} transferências · {fmt_int(voice.get('connection_count'))} conexões"]
    missing = voice.get("missing")
    if isinstance(missing, (list, tuple)) and missing:
        lines.append("Pendências: " + " · ".join(safe_text(item, 80) for item in missing[:8]))
    cards = [Card("\n".join(lines))]
    specs = [
        ("Sessões", "worker_voice_session_reports", "worker_voice_session_skipped"),
        ("Handoffs", "worker_voice_session_handoff", "worker_voice_session_handoff_skipped"),
        ("Preparos", "worker_voice_session_transfer_prepare", "worker_voice_session_transfer_prepare_skipped"),
        ("Transferências", "worker_voice_session_transfer_begin", None),
        ("TTS direto", "worker_voice_session_direct_tts", "worker_voice_session_direct_tts_skipped"),
        ("Probes", "worker_voice_session_connection_probe", "worker_voice_session_connection_probe_skipped"),
    ]
    counters = ["### 📊 Eventos desde o reinício"]
    for label, prefix, skipped in specs:
        line = f"{label}: {fmt_int(metrics.get(prefix + '_ok'))} ok · {fmt_int(metrics.get(prefix + '_failed'))} falhas"
        if skipped:
            line += f" · {fmt_int(metrics.get(skipped))} ignorados"
        counters.append(line)
    cards.append(Card("\n".join(counters)))
    session = _mapping(voice.get("last_session"))
    if session:
        cards.append(Card(f"### Última sessão\nGuild {safe_text(session.get('guild_id'), 30)} · canal {safe_text(session.get('channel_id'), 30)} · TTL {fmt_int(session.get('ttl_seconds'))} s"))
    handoff = _mapping(voice.get("last_handoff"))
    if handoff:
        cards.append(Card(f"### Último handoff\nDono: {safe_text(handoff.get('voice_owner', handoff.get('transport_owner')), 40)}\nSessão presente: {fmt_bool(handoff.get('session_id_present'))} · endpoint presente: {fmt_bool(handoff.get('endpoint_present'))} · token presente: {fmt_bool(handoff.get('voice_token_present'))}"))
    transfer = _mapping(voice.get("last_transfer"))
    if transfer:
        cards.append(Card(f"### Última transferência\n{safe_text(transfer.get('voice_owner', transfer.get('current_owner')), 40)} → {safe_text(transfer.get('requested_owner'), 40)}\nEstado: {safe_text(transfer.get('state'), 100)}"))
    connection = _mapping(voice.get("last_connection"))
    if connection:
        lines = ["### Última conexão", f"Estado: {safe_text(connection.get('state'), 80)} · etapa: {safe_text(connection.get('stage'), 80)}", f"WS pronto: {fmt_bool(connection.get('ready_received'))} · UDP ok: {fmt_bool(connection.get('udp_probe_ok'))}"]
        if connection.get("error"):
            lines.append("Erro: " + safe_text(connection["error"], 160))
        cards.append(Card("\n".join(lines)))
    return cards


def tts_pages(snapshot: StatusSnapshot, section: str = "summary") -> tuple[PanelPage, ...]:
    section = section if section in TTS_SECTIONS else "summary"
    header = f"## 🔊 TTS · {TTS_SECTIONS[section]}\n-# Métricas de geração desde o reinício do bot."
    if not snapshot.tts_available:
        cards = [Card("Métricas TTS indisponíveis. Atualize quando o módulo de voz estiver pronto.")]
    else:
        cards = _summary_cards(snapshot) if section == "summary" else _engine_cards(snapshot) if section == "engines" else _voice_cards(snapshot)
    chunks = _chunks(cards)
    return tuple(PanelPage(header, cards, f"Página {index + 1}/{len(chunks)} · coletado às {timestamp(snapshot.collected_at)}", discord.Color.dark_teal()) for index, cards in enumerate(chunks))


def panel_pages(snapshot: StatusSnapshot, tab: str, section: str = "summary") -> tuple[PanelPage, ...]:
    return tts_pages(snapshot, section) if tab == "tts" else server_pages(snapshot)

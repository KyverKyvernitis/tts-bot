#!/usr/bin/env python3
"""Consulta e compara a saída do Music Agent instalado, sem reiniciar serviços.

Usa somente a biblioteca padrão. Lê configurações já existentes como dados;
não importa o worker/servidor, não cria tokens e não grava arquivos de env.
O modo Opus direto é salvo por servidor. Perfil e proteção duram até reiniciar.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request


PROFILES = ("balanced", "quality_192", "quality_256")
MAX_ENV_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
TIMEOUT_SECONDS = 10
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class HelperError(RuntimeError):
    """Erro local cuja mensagem já foi preparada para exibição segura."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        # Authorization nunca deve acompanhar um redirecionamento do serviço.
        return None


def decode_env_value(value: str) -> str:
    """Espelha a leitura literal do worker, sem interpolar shell ou variáveis."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1]:
        if value[0] == '"':
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                try:
                    return json.loads(value.replace("\\$", "$").replace("\\`", "`"))
                except (ValueError, TypeError):
                    pass
            return value[1:-1]
        if value[0] == "'":
            return value[1:-1]
    return value


def read_existing_env(path: Path, values: dict[str, str]) -> None:
    try:
        with path.expanduser().open("rb") as source:
            raw = source.read(MAX_ENV_BYTES + 1)
    except FileNotFoundError:
        return
    except OSError:
        raise HelperError("Não consegui ler o ambiente já instalado; confira os caminhos e as permissões.") from None
    if len(raw) > MAX_ENV_BYTES:
        raise HelperError("O arquivo de ambiente excede o limite de leitura do auxiliar.")
    for line in raw.decode("utf-8", errors="replace").splitlines():
        clean = line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        if clean.startswith("export "):
            clean = clean[len("export "):].strip()
        name, literal = clean.split("=", 1)
        name = name.strip()
        if ENV_NAME.fullmatch(name):
            values.setdefault(name, decode_env_value(literal))


def environment(environ=None, *, home: Path | None = None) -> dict[str, str]:
    values = dict(os.environ if environ is None else environ)
    home = home or Path.home()
    read_existing_env(Path(values.get("PHONE_WORKER_ENV") or home / ".phone-worker.env"), values)
    worker_dir = Path(values.get("PHONE_WORKER_DIR") or home / "phone-worker").expanduser()
    music_env = Path(values.get("MUSIC_AGENT_ENV") or worker_dir / "secrets/music-agent.env")
    read_existing_env(music_env, values)
    return values


def positive_guild(value: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("informe o ID numérico do servidor Discord") from None
    if not 0 < result < 2**64:
        raise argparse.ArgumentTypeError("o ID do servidor precisa ser positivo e caber em 64 bits")
    return result


def loss_fraction(value: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("a perda deve ser uma fração entre 0 e 1, por exemplo 0.05") from None
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise argparse.ArgumentTypeError("a perda deve ser uma fração finita entre 0 e 1")
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    actions = result.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("status", "consulta perfil solicitado, encoder aplicado e capacidades"),
        ("profile", "seleciona um perfil de saída para este servidor"),
        ("protection", "ajusta FEC e perda esperada para uma comparação controlada"),
        ("direct", "salva Opus direto automático ou desligado para este servidor"),
        ("reset", "remove os ajustes de teste deste servidor e restaura os defaults instalados"),
    ):
        action = actions.add_parser(name, help=help_text)
        action.add_argument("--guild", required=True, type=positive_guild, metavar="ID")
        if name == "profile":
            action.add_argument("--profile", required=True, choices=PROFILES)
        elif name == "protection":
            action.add_argument("--fec", required=True, choices=("on", "off"))
            action.add_argument("--loss", required=True, type=loss_fraction,
                                help="fração esperada de perda; 0.05 significa 5%%")
        elif name == "direct":
            action.add_argument("--mode", required=True, choices=("auto", "off"))
    return result


def command_body(args: argparse.Namespace) -> dict:
    body = {"action": "audio_output", "guild_id": args.guild}
    if args.action == "profile":
        body["settings"] = {"profile": args.profile}
    elif args.action == "protection":
        body["settings"] = {"fec": args.fec == "on", "expected_packet_loss": args.loss}
    elif args.action == "direct":
        body["settings"] = {"opus_direct": args.mode}
    elif args.action == "reset":
        body["reset"] = True
    return body


def endpoint(values: dict[str, str]) -> str:
    host = str(values.get("MUSIC_AGENT_HOST") or "127.0.0.1").strip()
    host = {"0.0.0.0": "127.0.0.1", "localhost": "127.0.0.1", "::": "::1"}.get(host, host)
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        except ValueError:
            raise HelperError("Este auxiliar aceita somente o endereço local do Music Agent.") from None
    try:
        port = int(values.get("MUSIC_AGENT_PORT") or "8780")
    except (TypeError, ValueError):
        raise HelperError("MUSIC_AGENT_PORT precisa ser uma porta numérica válida.") from None
    if not 1 <= port <= 65535:
        raise HelperError("MUSIC_AGENT_PORT precisa estar entre 1 e 65535.")
    address = "[" + host + "]" if ":" in host else host
    return f"http://{address}:{port}/command"


def request_command(body: dict, values: dict[str, str], *, opener=None) -> dict:
    token = str(values.get("MUSIC_AGENT_TOKEN") or "").strip()
    if not token:
        raise HelperError("MUSIC_AGENT_TOKEN ausente no ambiente existente; nenhum token foi criado.")
    target = endpoint(values)
    request = urllib.request.Request(
        target, data=json.dumps(body, allow_nan=False).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    local = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with local.open(request, timeout=TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise HelperError(f"HTTP {int(status)}: confira a versão 0.3.90 ou mais nova e o estado do agente.")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 401:
            message = "autenticação recusada; confira o token já instalado."
        elif error.code == 400:
            message = "o agente recusou o comando; confira command_error no music_agent.log."
        else:
            message = "confira o agente atualizado e o serviço local."
        raise HelperError(f"HTTP {error.code}: {message}") from None
    except (urllib.error.URLError, OSError, TimeoutError):
        # Não incluir a exceção: algumas bibliotecas imprimem request, URL ou header.
        raise HelperError("O Music Agent não respondeu pela conexão local; consulte music_agent.log.") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HelperError("A resposta local excedeu o limite de tamanho do auxiliar.")
    try:
        def reject_constant(_):
            raise ValueError
        result = json.loads(raw, parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        raise HelperError("O Music Agent devolveu uma resposta JSON inválida.") from None
    if not isinstance(result, dict):
        raise HelperError("O Music Agent devolveu uma resposta incompatível.")
    returned_guild = result.get("guild_id")
    if returned_guild is not None and returned_guild != body["guild_id"]:
        raise HelperError("O Music Agent respondeu para um servidor diferente do solicitado.")
    return result


# A resposta de /command pode conter outros dados do agente em versões futuras.
# Apenas esta árvore de metadados de qualidade pode chegar ao terminal.
QUALITY_KEYS = frozenset({
    "settings", "revision", "requested", "applied", "profile", "bitrate_kbps",
    "channel_bitrate_kbps", "copy_bitrate_kbps", "target_bitrate_kbps", "source_abr_kbps", "fec",
    "expected_packet_loss", "application", "bandwidth", "signal_type", "active",
    "transport", "status", "state", "reason", "changed", "queued", "supported",
    "limited_by_channel", "limited_by_config", "channel_id", "playback_token",
    "encoder", "encoder_version", "opus_version", "discord_version", "pcm",
    "profiles", "balanced", "quality_192", "quality_256", "default_profile",
    "live_updates", "per_guild", "persistent", "reset_on_restart", "passthrough",
    "eligible", "reasons", "enabled", "fields", "min", "max", "default", "choices",
    "ok", "mode", "pending", "requested_kbps", "applied_kbps", "volume_percent",
    "supported_fields", "unsupported_fields", "settings_revision", "source_is_opus",
    "runtime", "discord_py_version", "libopus_version", "encoder_controls", "bitrate",
    "complexity", "vbr", "metrics", "audio_frame_count", "audio_bytes", "source_stall_count",
    "partial_frame_count", "source_deadline_overruns", "mix_limited_frames", "profile_target_kbps",
    "sample_rate_hz", "channels", "frame_duration_ms", "loss_is_measured", "pcm_mixer_preserved",
    "passthrough_default", "unsupported_controls", "error_type", "applied_at", "encoder_kind",
    "encoder_supported", "unsupported", "capabilities", "requested_profile", "fec_applied",
    "expected_packet_loss_applied",
    "opus_direct", "opus_direct_modes", "opus_direct_default", "output_mode",
    "persistence", "ready", "stored", "opus_direct_persistent", "opus_direct_segmented_archive",
    "copied_packets", "encoded_packets", "latched", "packet_duration_ms", "source_bitrate_kbps",
    "preskip_samples", "trimmed_tail_samples", "packet_validation_required", "candidate",
    "validated_packets", "fallback_encoder", "copy_active", "copy_supported", "required_packet_duration_ms",
    "source", "copied_bytes", "fallback_latched", "copy_packet_limit_bytes", "copy_fec",
    "sample_rate", "input_sample_rate", "pre_skip", "output_gain", "mapping_family",
    "input_sample_rate_hz", "pre_skip_samples", "channel_mapping", "output_gain_q8",
    "end_granule_samples", "ogg_pages", "audio_packets", "trim_mode", "tail_padding_samples",
    "raw_duration_samples", "decoded_samples",
    "archive_reader_continuous", "archive_segment_index", "archive_segments_total",
    "archive_segments_completed", "archive_preroll_packets_skipped", "archive_preroll_samples_skipped",
    "preparation", "cache_enabled", "prepared_opus", "prepared_bitrate_kbps", "prepared_source_codec",
    "prepared_cache_hit", "opus_direct_preparation", "opus_direct_origin", "opus_direct_bitrate_kbps",
    "preparation_required", "cache", "max_bytes", "max_item_bytes", "hits", "misses", "active_writers",
    "prepared_requested_fec", "prepared_expected_packet_loss",
    "processed_active", "processed_bitrate_kbps", "opus_transport", "opus_processed_active",
    "opus_processed_bitrate_kbps", "opus_output_processing", "opus_processed_effects",
    "opus_processed_volume_min", "opus_processed_volume_max", "original_packet_copy_with_effects",
})


def metadata_only(value, values: dict[str, str], *, depth=0):
    if depth > 6:
        return None
    if isinstance(value, dict):
        return {name: metadata_only(item, values, depth=depth + 1)
                for name, item in value.items() if name in QUALITY_KEYS}
    if isinstance(value, list):
        return [metadata_only(item, values, depth=depth + 1) for item in value[:32]]
    if isinstance(value, str):
        text = value
        for name, secret in values.items():
            if re.search(r"TOKEN|PASSWORD|SECRET|CREDENTIAL|(?:^|_)KEY$", name, re.I) and secret:
                text = text.replace(str(secret), "[redacted]")
        if "://" in text or re.search(r"authorization|bearer\s|cookie", text, re.I):
            return "[redacted]"
        return text[:160]
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def public_result(result: dict, values: dict[str, str]) -> dict:
    output = {}
    for name in ("ok", "version", "guild_id", "audio_output", "sync", "capabilities"):
        if name in result:
            output[name] = metadata_only(result[name], values)
    return output


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    values = environment()
    result = request_command(command_body(args), values)
    print(json.dumps(public_result(result, values), ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if result.get("ok") is True else 1


def cli(argv=None) -> int:
    try:
        return main(argv)
    except HelperError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Falha local ({type(error).__name__}); confira o serviço e a configuração instalada.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())

#!/usr/bin/env bash
set -Eeuo pipefail

cd /home/ubuntu/bot

# Garante diretórios runtime antes de carregar o cog TTS. O cleanup externo só
# remove arquivos, mas esta guarda evita falha total se a pasta foi apagada.
mkdir -p "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/runtime" \
         "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/cache" \
         "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/credentials" 2>/dev/null || true
chmod 700 "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}" \
          "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/runtime" \
          "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/cache" \
          "${TTS_TEMP_DIR:-/home/ubuntu/bot/tmp_audio}/credentials" 2>/dev/null || true

# Dependências Python podem ser promovidas como releases versionados pelo
# updater. Não use `source activate`: venvs copiados/versionados podem conter
# caminhos antigos nos scripts de ativação. Executar o interpretador diretamente
# mantém sys.prefix correto e permite rollback por simples troca do symlink.
PYTHON_RUNTIME_BASE="${TTS_BOT_PYTHON_RUNTIME_ROOT:-${DISCORD_AUTO_UPDATE_STAGING_DIR:-/home/ubuntu/bot-update-staging}/candidates/python-runtimes}"
PYTHON_RUNTIME_CURRENT="${TTS_BOT_PYTHON_RUNTIME_CURRENT_LINK:-$PYTHON_RUNTIME_BASE/current}"
if [[ -x "$PYTHON_RUNTIME_CURRENT/venv/bin/python" ]]; then
  PYTHON_BIN="$PYTHON_RUNTIME_CURRENT/venv/bin/python"
elif [[ -x /home/ubuntu/bot/.venv/bin/python ]]; then
  PYTHON_BIN=/home/ubuntu/bot/.venv/bin/python
else
  PYTHON_BIN="$(command -v python3)"
fi
PYTHON_ENV="$(dirname "$(dirname "$PYTHON_BIN")")"
export VIRTUAL_ENV="$PYTHON_ENV"
export PATH="$PYTHON_ENV/bin:$PATH"

set -a
source /home/ubuntu/bot/.env
set +a

# A VPS não inicia nem aguarda Lavalink local. Música pesada pertence ao phone worker/Music Agent.

# Mantém prints legados de diagnóstico em ordem cronológica no journald. Sem
# unbuffered, várias linhas de voz podiam aparecer juntas minutos depois.
exec "$PYTHON_BIN" -u bot.py

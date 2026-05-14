#!/data/data/com.termux/files/usr/bin/bash
# Instalador local para rodar dentro desta pasta no Termux.
set -u

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

cp "$SRC_DIR/start-phone-lavalink.sh" "$HOME/start-phone-lavalink.sh"
cp "$SRC_DIR/watch-phone-lavalink.sh" "$HOME/watch-phone-lavalink.sh"
chmod +x "$HOME/start-phone-lavalink.sh" "$HOME/watch-phone-lavalink.sh"

if [[ ! -f "$HOME/.phone-lavalink.env" ]]; then
  cp "$SRC_DIR/phone-lavalink.env.example" "$HOME/.phone-lavalink.env"
  chmod 600 "$HOME/.phone-lavalink.env"
  echo "Criei ~/.phone-lavalink.env. Edite a senha antes de iniciar."
else
  echo "Mantive ~/.phone-lavalink.env existente."
fi

mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-lavalink-watch.sh" <<'BOOT'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock 2>/dev/null || true
sleep 20
if ! tmux has-session -t lavalink-watch 2>/dev/null; then
  tmux new-session -d -s lavalink-watch '~/watch-phone-lavalink.sh'
fi
BOOT
chmod +x "$HOME/.termux/boot/start-lavalink-watch.sh"

echo "Instalado. Próximos passos:"
echo "1) nano ~/.phone-lavalink.env"
echo "2) termux-wake-lock"
echo "3) tmux new-session -d -s lavalink-watch '~/watch-phone-lavalink.sh'"

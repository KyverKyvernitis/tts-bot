# Lavalink auxiliar no celular

Arquivos para manter o Lavalink do celular vivo como extensão da VPS.

## Instalação no Termux

No Termux normal, dentro desta pasta, rode:

```bash
./install.sh
nano ~/.phone-lavalink.env
```

O Lavalink deve ficar dentro do Debian/proot em `/root/lavalink` por padrão.
Se usar outro caminho, ajuste `PHONE_LAVALINK_PROOT_DIR`.

Inicie o watchdog local:

```bash
termux-wake-lock
tmux new-session -d -s lavalink-watch '~/watch-phone-lavalink.sh'
```

Para ver logs do Lavalink:

```bash
tmux attach -t lavalink-debian
```

## Opcional: Termux:Boot

Com o app Termux:Boot instalado, crie:

```bash
mkdir -p ~/.termux/boot
cat > ~/.termux/boot/start-lavalink-watch.sh <<'SH'
#!/data/data/com.termux/files/usr/bin/bash
termux-wake-lock
sleep 20
if ! tmux has-session -t lavalink-watch 2>/dev/null; then
  tmux new-session -d -s lavalink-watch '~/watch-phone-lavalink.sh'
fi
SH
chmod +x ~/.termux/boot/start-lavalink-watch.sh
```

Desative economia de bateria para Termux, Termux:Boot e Tailscale.

#!/usr/bin/env bash
set -Eeuo pipefail

BACKUP_DIR="/home/ubuntu/backups"
DATE="$(date '+%Y-%m-%d_%H-%M-%S')"
HOSTNAME="$(hostname)"
ARCHIVE="$BACKUP_DIR/${HOSTNAME}_configs_$DATE.tar.gz"
TMP_DIR="/tmp/tts-bot-backup-$DATE"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR"

mkdir -p "$TMP_DIR/home/ubuntu/bot"
mkdir -p "$TMP_DIR/etc/systemd/system"
mkdir -p "$TMP_DIR/etc/fail2ban"
mkdir -p "$TMP_DIR/etc/ssh"
mkdir -p "$TMP_DIR/etc/ufw"
mkdir -p "$TMP_DIR/etc/sysctl.d"

cp -a /home/ubuntu/bot/.env "$TMP_DIR/home/ubuntu/bot/" 2>/dev/null || true
cp -a /home/ubuntu/bot/webserver.py "$TMP_DIR/home/ubuntu/bot/" 2>/dev/null || true

find /home/ubuntu/bot -maxdepth 1 -type f -name '*.sh' -exec cp -a {} "$TMP_DIR/home/ubuntu/bot/" \; 2>/dev/null || true

cp -a /etc/systemd/system/tts-bot.service "$TMP_DIR/etc/systemd/system/" 2>/dev/null || true
cp -a /etc/fail2ban/jail.local "$TMP_DIR/etc/fail2ban/" 2>/dev/null || true
cp -a /etc/ssh/sshd_config "$TMP_DIR/etc/ssh/" 2>/dev/null || true
cp -a /etc/ssh/sshd_config.d "$TMP_DIR/etc/ssh/" 2>/dev/null || true
cp -a /etc/ufw "$TMP_DIR/etc/" 2>/dev/null || true
cp -a /etc/sysctl.d/99-swap.conf "$TMP_DIR/etc/sysctl.d/" 2>/dev/null || true

tar -czf "$ARCHIVE" -C "$TMP_DIR" .
chmod 600 "$ARCHIVE"

find "$BACKUP_DIR" -maxdepth 1 -type f -name '*_configs_*.tar.gz' -mtime +14 -delete

rm -rf "$TMP_DIR"

echo "Backup criado em: $ARCHIVE"

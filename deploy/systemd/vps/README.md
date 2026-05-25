# VPS systemd source of truth

These files are the repository-managed templates for units that used to live only
inside `/etc/systemd/system` on the Oracle VPS.

Use:

```bash
sudo /home/ubuntu/bot/scripts/install-vps-systemd-units.sh
```

The installer is idempotent and creates backups before changing live files. It:

- syncs the active VPS unit templates from this directory to `/etc/systemd/system`;
- keeps VPS-local `lavalink.service` disabled/masked because Lavalink belongs on
  the phone worker / Music Agent side;
- installs `tts-bot-alert@.service` and the updater timer;
- installs phone worker watcher units but keeps them inactive unless explicitly
  enabled by environment flags;
- normalizes emergency crontab lines without re-enabling healthcheck/resource-check.

Never commit local secrets. Units may reference `/home/ubuntu/bot/.env`, but the
actual `.env` file stays local to the VPS.

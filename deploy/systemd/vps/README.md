# VPS systemd source of truth

These files are the repository-managed templates for units that used to live only
inside `/etc/systemd/system` on the Oracle VPS.

Use:

```bash
sudo /home/ubuntu/bot/scripts/install-vps-systemd-units.sh
```

To compare the live VPS state with the repository templates without changing anything:

```bash
sudo /home/ubuntu/bot/scripts/install-vps-systemd-units.sh --audit
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


## Source of truth

Treat this directory as the clean, reviewable source of truth for VPS units.
`/etc/systemd/system` is the installed output generated from these templates. Do
not commit live backups, generated status files, logs, `.env` values, tokens, or
other local state from the VPS.

The installer intentionally keeps `healthcheck.sh` and `resource-check.sh` paused
when their emergency `TEMP_DISABLED_*` markers are present. It may normalize
malformed cron lines, but it must not silently re-enable those jobs.

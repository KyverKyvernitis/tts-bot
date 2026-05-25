# Legacy VPS-local Lavalink drop-ins

This directory intentionally contains no active drop-in templates.

Old live files such as `/etc/systemd/system/lavalink.service.d/10-quality.conf`,
`30-lavasrc-env.conf`, or `override.conf` belonged to the old VPS-local Lavalink
runtime. They should remain disabled on the VPS because audio Lavalink now belongs
to the phone worker / Music Agent side.

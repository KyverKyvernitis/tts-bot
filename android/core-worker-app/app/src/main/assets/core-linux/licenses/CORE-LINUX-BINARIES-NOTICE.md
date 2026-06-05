# Core Linux embedded binaries notice

This APK stage bundles audited aarch64 artifacts imported from Termux packages for private/internal Core Worker testing. Runtime downloads are disabled.

Bundled components:

- PRoot 5.1.107.76 — GPL-2.0 — source: https://github.com/termux/proot/archive/v5.1.107.76.zip
- BusyBox 1.37.0-r3 — GPL-2.0 — source: https://busybox.net/downloads/busybox-1.37.0.tar.bz2
- talloc 2.4.3 — GPL-3.0 — source: https://www.samba.org/ftp/talloc/talloc-2.4.3.tar.gz
- libandroid-selinux 14.0.0.11-1 — Android/SELinux platform package — source via Termux package recipe
- PCRE2 10.47 — BSD-3-Clause — source via PCRE2 upstream/Termux package recipe

Public redistribution needs a full corresponding-source bundle, license texts, Termux build recipes/configs, and patch notes matching the exact binary hashes in `embedded-binaries-manifest.json`.

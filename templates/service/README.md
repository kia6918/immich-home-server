# Host supervision

`scripts/lifecycle.py` renders systemd and launchd definitions with the target's actual
Python/runtime/state paths. No user, hostname or home path is embedded here. Both run
the same storage-aware guard; they never schedule Immich version upgrades. Container
restart policies remain disabled so Docker cannot bypass the guard at host startup.

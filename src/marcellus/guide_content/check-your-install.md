---
title: Check your install
section: getting-started
order: 7
---

`marcellus doctor` (or `fsc doctor`, the same program) checks that Marcellus
can reach everything it needs and prints one line per check. For anything that
is not right it adds a line starting `fix:` that says what to change. It only
reads: it never writes a file, sends a push or changes a setting.

Run it where Marcellus runs, as the same user as the service if you can:

```
fsc doctor                       # systemd or a virtualenv
docker exec marcellus fsc doctor # Docker
```

It reads the same config file the server does (`MARCELLUS_CONFIG`, then
`/etc/marcellus/sidecar.yml`, then `./config/sidecar.yml`). To check a
different file, add `--config /path/to/sidecar.yml`.

## What it prints

```
 ✓ config               /opt/marcellus/config/sidecar.yml loaded
 ✓ frigate-api          Frigate 0.18.2 at http://frigate.example:5000
 ✗ frigate-signin       https://frigate.example:8971: tls untrusted
                        fix: Frigate at https://frigate.example:8971 presented a TLS certificate Marcellus does not trust ... set `frigate.verify_tls: false` ...
 ✓ frigate-urls-differ  the two Frigate URLs are different origins
 ✓ frigate-db           /opt/frigate/database/frigate.db opens read-only; Frigate tables present
 ! recordings           /mnt/frigate holds no recordings yet
                        fix: expected on a brand-new Frigate: wait for it to record, then rerun doctor
 ✓ scrub-cache          /data/scrub writable; 212.4 GiB free
 ✓ ffmpeg               ffmpeg 6.1.1 runs
 - mqtt                 push is off (push.enabled: false)
 - push-relay           push is off (push.enabled: false)
 ✓ service              systemd unit marcellus is active; /healthz on 127.0.0.1:5001 is ok

Fix the failures above. 8 passed, 1 warning, 1 failed, 2 skipped.
```

The mark at the start of each line says how it went: `✓` passed, `!` is a
warning (worth a look, not broken), `✗` failed, `-` was skipped because it
does not apply (for example the push checks when push is off). The command
exits with status 0 when nothing failed (warnings are fine) and 1 when
something did, so a script can rely on it.

## The checks

| Check | What it looks at | When it is not a pass |
|---|---|---|
| `config` | The config file exists, parses and validates. | A failure names the key at fault. A warning means no file was found (defaults in use), or a key was misspelled and is being ignored. |
| `frigate-api` | `frigate.base_url` answers `/api/version`; the version is shown. | Frigate is down, the URL or port is wrong, or a certificate or `http`/`https` mismatch (see the next check). |
| `frigate-signin` | `frigate.proxy_base_url` answers, and whether Frigate has sign-in on or off. | An untrusted certificate (Frigate's own is self-signed) or `http://` pointed at an HTTPS port gets the same explanation and fix the log and [Troubleshooting](/guide/troubleshooting) give. A warning means the origin accepts anyone although Frigate has sign-in on. |
| `frigate-urls-differ` | The two Frigate URLs are different origins. | Identical URLs make the sidecar's session check a no-op. |
| `frigate-db` | `frigate.db_path` exists, opens read-only and has Frigate's tables. | Wrong path, a file that is not Frigate's database, or no read permission. A permission failure names the file's owner and group, and the fix is the exact `usermod -aG <group> <user>` command. If you run doctor as a different user than the service, it says so and checks the service user's access. |
| `recordings` | `frigate.recordings_path` exists, is readable, holds recent files, and matches where Frigate says its recordings are. | A path that does not line up with Frigate's own recording paths fails, and the fix explains how `recordings_path` replaces `media_path`. A brand-new Frigate with no recordings yet is a warning, not a failure. |
| `scrub-cache` | The cache directory is writable and has free space above `scrub.min_free_bytes`. | Low space pauses scrub generation. Skipped when scrub is off. |
| `ffmpeg` | `ffmpeg` and `ffprobe` are installed and run. | Needed for scrub; a warning when scrub is off. |
| `mqtt` | With push on: connects to the broker with your settings, then waits (up to `--mqtt-wait`, 20 s) for any message on Frigate's topics. | A refused login or unreachable broker fails. Connected but silent is a warning: check Frigate's own `mqtt:` settings. Skipped when push is off. |
| `push-relay` | With push on and the relay transport: the relay answers. Nothing is sent. | The machine cannot reach the relay. Skipped when push is off or the transport is `mock`. |
| `service` | The systemd unit or Docker container is running, and `/healthz` answers on `sidecar.bind_host:bind_port`. | A stopped unit or container, or a degraded `/healthz` (its failing parts are named). Skipped when no unit or container is found and nothing answers. |

Each check has its own time limit and one failing never stops the rest.

## Options

- `--json` prints a list of `{"id", "status", "detail", "fix"}` objects
  instead of the table, where `status` is `pass`, `warn`, `fail` or
  `skipped`. Scripts and the installer use it.
- `--timeout SECONDS` sets how long each check may take (default 5).
- `--mqtt-wait SECONDS` sets how long the `mqtt` check waits for a message
  from Frigate (default 20).
- `--config PATH` checks a specific config file.
- `--no-color` turns colours off.

Passwords, keys and tokens are never printed, so the output is safe to paste
into a bug report or a forum post.

For how each setting works, see [First run](/guide/first-run). For a problem
doctor does not explain, see [Troubleshooting](/guide/troubleshooting).

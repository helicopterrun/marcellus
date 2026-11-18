---
title: Set up push
section: notifications
order: 6
---

Push alerts need two things Marcellus cannot do for you: a message broker
(MQTT) that Frigate publishes to, and a Frigate login for the app. This page
walks through both. Marcellus must already be running; see
[Docker or systemd?](/guide/choose-install) if it is not.

## How it fits together

Frigate publishes what it sees to an MQTT broker. Marcellus subscribes to that
broker (the topics `frigate/reviews` and `frigate/available`), decides whether
each item deserves an alert, and sends it to your phone through the push relay.
No broker, no events, no alerts. Push is off until you turn it on.

## The quick way: let `fsc init` do it

Run `fsc init --force` (Docker:
`docker compose run --rm marcellus init --force -o /config/sidecar.yml`; systemd:
`sudo /opt/marcellus/venv/bin/fsc init --force -o /etc/marcellus/sidecar.yml`).
`init` reads Frigate's `mqtt:` section, then asks **Set up push alerts now?**
Answer `y` and it:

- shows the broker it found (address, port, user) and asks you to confirm it,
  or to type another;
- writes `push.enabled: true` and the broker's host, port and user into
  `sidecar.yml`;
- asks for the broker password with hidden input and writes it **only** to the
  environment file, as `MARCELLUS_PUSH__MQTT_PASSWORD`, never into
  `sidecar.yml` and never to the screen. If a password is already stored there
  it is kept unless you say yes to replacing it;
- connects to the broker once, with the same check `fsc doctor` runs, and tells
  you the result.

Where the password goes depends on how it runs:

- **systemd:** `/etc/marcellus-push.env`, created root-owned with mode `0600`.
  Run `init` as root. As anyone else it stores nothing and prints the exact
  commands to run instead (they read the password silently, so they hold no
  secret).
- **Docker:** `/opt/marcellus/.env` on the host. `init` runs inside the
  container, which cannot see that file, so it prints the host commands (the
  same silent-read form) and the `docker compose up -d` to apply them. If you
  mount the file into the container, `--env-file` writes it directly.

If MQTT is off in Frigate, `init` says push cannot work until it is switched on
in Frigate's own config (that is Frigate's setting; `init` never edits it) and
skips push. If Frigate talks to the broker over TLS, it warns you: Marcellus
connects in plain text, so use the broker's plain listener (usually 1883).

For scripts: `fsc init --force --non-interactive --enable-push
--mqtt-password-file /path/to/file` (or set `MARCELLUS_INIT_MQTT_PASSWORD` in
the environment). `--mqtt-host`, `--mqtt-port` and `--mqtt-user` override what
was found. Without `--enable-push` a non-interactive run leaves push alone, and
with it but no password for a broker that needs one, it skips push and says so.

Then restart Marcellus (`docker compose up -d` or `systemctl restart marcellus`)
and carry on with section 4, Frigate sign-in, below. Sections 1 to 3 are the
same thing done by hand.

## Doing it by hand

### 1. Frigate and MQTT

1. Make sure Frigate is configured to publish to an MQTT broker. This is part
   of Frigate's own configuration, covered in Frigate's documentation.
2. Note the broker's address, port (Marcellus assumes `1883`) and, if it needs
   one, the username and password.
3. Marcellus connects to the broker in plain text, with no TLS option. Use the
   broker's ordinary (non-TLS) listener.

### 2. Tell Marcellus about the broker

Edit `sidecar.yml` (`/opt/marcellus/config/sidecar.yml` for Docker,
`/etc/marcellus/sidecar.yml` for systemd) and add a `push:` section:

```yaml
push:
  enabled: true
  mqtt_host: 192.168.1.10      # your broker's address
  mqtt_port: 1883
  mqtt_username: marcellus     # leave out if the broker needs no login
```

Leave `mqtt_username` out for a broker without a login. The default host is
`localhost`, which only works if the broker runs on the same machine as
Marcellus (and Marcellus is not in a bridged-network container).

**Keep the password out of that file.** Set it as an environment variable
instead:

- **Docker:** add `MARCELLUS_PUSH__MQTT_PASSWORD=your-password` to
  `/opt/marcellus/.env`. The compose file passes `.env` into the container.
- **systemd:** put the same line in `/etc/marcellus-push.env`, owned by root
  with mode `0600`. The unit reads this file if it exists.

Then restart: `docker compose up -d` (Docker) or
`systemctl restart marcellus` (systemd).

### 3. Check the connection

Open [`/healthz`](/healthz). The `mqtt` line should say `connected`. If it
says `disconnected`, check the host, port, username and password, and that
Marcellus can reach the broker from where it runs. With Docker's default host
networking the container shares the machine's network. If you switched to
bridged networking, `mqtt_host` must resolve from inside the container.

## 4. Frigate sign-in

Marcellus never stores a Frigate password. Its pages and the phone app sign in
through Frigate itself, so the settings must point at the right Frigate ports:

- `frigate.base_url`: Frigate's plain port, usually `5000`. Marcellus uses it
  for its own background calls.
- `frigate.proxy_base_url`: Frigate's signed-in port, usually `8971`. Marcellus
  checks your login against it. These two addresses must be different.
  Frigate serves this port over HTTPS with a self-signed certificate by
  default, so use `https://` and set `frigate.verify_tls: false`, or turn TLS
  off in Frigate and use `http://` (see "Frigate's HTTPS sign-in port" in
  [First run](/guide/first-run)).

If Frigate has sign-in turned on (the normal case), leave
`sidecar.require_frigate_auth` at its default of `true`, then open
`http://<host>:5001/login` and sign in with a Frigate account. If your Frigate
has sign-in turned off, there is nothing to set: Marcellus asks Frigate and
lets the app in without a login. See [First run](/guide/first-run) for the
details and what that means for who can reach Marcellus.

## 5. Connect your phone

Install the Elsinore app and complete its onboarding. The phone then appears under
[Settings, Push](/settings#push), where its **Test** button sends a real alert.
[Push notifications](/guide/push-notifications) explains the alert levels
and the push relay.

If nothing arrives, start at `/healthz`; no `mqtt` connection means no alerts.
[Troubleshooting](/guide/troubleshooting) lists the other usual causes.

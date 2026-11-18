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

## 1. Frigate and MQTT

1. Make sure Frigate is configured to publish to an MQTT broker. This is part
   of Frigate's own configuration, covered in Frigate's documentation.
2. Note the broker's address, port (Marcellus assumes `1883`) and, if it needs
   one, the username and password.
3. Marcellus connects to the broker in plain text, with no TLS option. Use the
   broker's ordinary (non-TLS) listener.

## 2. Tell Marcellus about the broker

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

## 3. Check the connection

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

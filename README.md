# Marcellus

A companion sidecar for [Frigate NVR](https://github.com/blakeblackshear/frigate): scrub
cache, push notifications, triage and analysis pages, face pipeline.

It adds:

- **Triage UI** for labeling tracked-object events as true-positive / false-positive
  / skip, with snapshots, video clips, and zone overlays. Labels go into a
  sidecar SQLite DB; Frigate's own DB is opened read-only.
- **Read-only analysis** over Frigate's database (score histograms, motion rate,
  FPS budget vs detector capacity, zone hit counts, annotation offset
  diagnostics, etc.), available as a CLI and as JSON HTTP endpoints.
- **Scrub-cache** (`/v1/scrub`): a uniform-cadence sprite-sheet cache generated
  from real recording segments, so clients get a predictable, gap-free timeline.
- **Reverse proxy** to Frigate's authenticated origin, so a client can hold a
  single base URL.
- **Push notifications** (`/v1/push`): subscribes to Frigate's `frigate/reviews`
  MQTT topic and routes each event through the **attention ladder**, deciding
  whether it becomes an APNs card, a Live Activity, or just a logged decision.
  Off by default.

Runs as a single Docker container next to Frigate, bind-mounting Frigate's
`config.yml` and `frigate.db` read-only and writing its own SQLite DB.

Actively developed and running in production alongside one Frigate 0.17
deployment, with the Elsinore iOS app as its main client. It is still a
single-deployment project at heart, so expect portability rough edges.

## Quick install

```sh
curl -fsSL https://raw.githubusercontent.com/helicopterrun/marcellus/main/install.sh | sudo bash
```

Uses Docker when available, otherwise a venv plus systemd unit, and prompts for
your Frigate URLs and paths. Re-run it to upgrade. Requirements, manual Docker
Compose and systemd installs, and upgrade notes are in the
[deployment guide](https://helicopterrun.github.io/marcellus/guide/deployment/).

## Documentation

- [Documentation site](https://helicopterrun.github.io/marcellus/)
- [User guide](https://helicopterrun.github.io/marcellus/guide/welcome/): setup,
  authentication, configuration, CLI, and every page and feature. The same
  guide ships inside the app.
- [Reference](https://helicopterrun.github.io/marcellus/reference/scrub-cache-and-proxy-spec/):
  design specs for the scrub cache and proxy API, push notifications, the
  attention ladder, and encounters. These are design notes and may lag the code.

## License

MIT, see [LICENSE](LICENSE).

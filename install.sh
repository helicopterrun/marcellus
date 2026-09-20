#!/usr/bin/env bash
# marcellus installer.
#
#   curl -fsSL https://raw.githubusercontent.com/helicopterrun/marcellus/main/install.sh | bash
#
# Docker present  -> compose deployment in $INSTALL_DIR (image from ghcr.io)
# No Docker       -> bare-metal venv + systemd unit
#
# Idempotent: re-running upgrades (pulls the new image / pip upgrade) and
# restarts, leaving your .env / sidecar.yml untouched.
set -euo pipefail

REPO="helicopterrun/marcellus"
RAW="https://raw.githubusercontent.com/$REPO/main"
INSTALL_DIR="${INSTALL_DIR:-/opt/marcellus}"
# Release train, not :latest — reruns of this script should not silently
# cross a minor version.
IMAGE="ghcr.io/$REPO:0.3"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo) -- installs into $INSTALL_DIR"

fetch() { # fetch <relpath> <dest> -- never clobber an existing user-edited file
  if [ -e "$2" ]; then
    say "keeping existing $2"
  else
    curl -fsSL "$RAW/$1" -o "$2"
  fi
}

mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/data"

if command -v docker >/dev/null 2>&1; then
  say "Docker found -- installing compose deployment in $INSTALL_DIR"
  docker compose version >/dev/null 2>&1 || die "docker is present but 'docker compose' is not; install the compose plugin"

  cd "$INSTALL_DIR"
  fetch docker-compose.yml docker-compose.yml
  fetch .env.example .env.example
  fetch .env.example .env
  # data dir must be writable by the container's non-root uid
  chown -R 10001:10001 "$INSTALL_DIR/data"

  say "pulling $IMAGE"
  docker pull "$IMAGE"

  if [ ! -f "$INSTALL_DIR/config/sidecar.yml" ]; then
    if [ -t 0 ]; then
      say "generating config (answer the prompts; defaults suit a stock Frigate install)"
      docker compose run --rm marcellus init -o /config/sidecar.yml
    else
      warn "stdin is not a tty (curl|bash) -- writing a default config; edit $INSTALL_DIR/config/sidecar.yml"
      docker compose run --rm marcellus init --non-interactive -o /config/sidecar.yml
    fi
  fi

  say "starting"
  docker compose up -d
  say "done. Check: curl http://localhost:5001/healthz  |  logs: docker logs -f marcellus"
  say "Edit $INSTALL_DIR/.env (host paths) and $INSTALL_DIR/config/sidecar.yml, then: docker compose up -d"
else
  say "Docker not found -- installing bare-metal (venv + systemd) in $INSTALL_DIR"
  command -v python3 >/dev/null 2>&1 || die "python3 is required"
  command -v systemctl >/dev/null 2>&1 || die "systemd is required for the bare-metal install"
  python3 - <<'EOF' || die "python >= 3.10 is required"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF
  command -v ffmpeg >/dev/null 2>&1 || warn "ffmpeg not found -- required for the scrub cache; install it via your package manager"

  say "creating venv + installing package"
  python3 -m venv "$INSTALL_DIR/venv" 2>/dev/null || {
    die "python3 -m venv failed (on Debian/Ubuntu: apt install python3-venv)"
  }
  "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
  # [http2]: long-lived HTTP/2 relay connection instead of the HTTP/1.1
  # keep-alive fallback (push/transport.py warns at startup without it).
  "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade "marcellus[http2]" 2>/dev/null || \
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade "marcellus[http2] @ git+https://github.com/$REPO"

  mkdir -p /etc/marcellus
  if [ ! -f /etc/marcellus/sidecar.yml ]; then
    if [ -t 0 ]; then
      say "generating config (answer the prompts; defaults suit a stock Frigate install)"
      "$INSTALL_DIR/venv/bin/fsc" init -o /etc/marcellus/sidecar.yml \
        --sidecar-db "$INSTALL_DIR/data/marcellus.db"
    else
      warn "stdin is not a tty (curl|bash) -- writing a default config; edit /etc/marcellus/sidecar.yml"
      "$INSTALL_DIR/venv/bin/fsc" init --non-interactive -o /etc/marcellus/sidecar.yml \
        --sidecar-db "$INSTALL_DIR/data/marcellus.db"
    fi
  else
    say "keeping existing /etc/marcellus/sidecar.yml"
  fi

  say "creating service user"
  id -u marcellus >/dev/null 2>&1 || \
    useradd --system --no-create-home --shell /usr/sbin/nologin marcellus
  mkdir -p "$INSTALL_DIR/data"
  chown -R marcellus: "$INSTALL_DIR" /etc/marcellus

  # The scrub cache lives outside the install dir; the unit's ProtectSystem=strict
  # blocks writes everywhere else, so grant it explicitly if configured.
  SCRUB_CACHE_DIR="$("$INSTALL_DIR/venv/bin/python" - <<'EOF' 2>/dev/null || true
import os
os.environ.setdefault("MARCELLUS_CONFIG", "/etc/marcellus/sidecar.yml")
from marcellus.config import load_settings
print(load_settings().scrub.cache_dir)
EOF
)"
  EXTRA_RW=""
  if [ -n "$SCRUB_CACHE_DIR" ]; then
    mkdir -p "$SCRUB_CACHE_DIR" && chown marcellus: "$SCRUB_CACHE_DIR"
    EXTRA_RW="ReadWritePaths=$SCRUB_CACHE_DIR"
  fi

  say "installing systemd unit"
  cat > /etc/systemd/system/marcellus.service <<EOF
[Unit]
Description=Marcellus (triage UI + analysis)
Documentation=https://github.com/$REPO
After=network.target

[Service]
Type=simple
# Needs READ access to Frigate's recordings and frigate.db -- grant via group
# membership or ACLs if Frigate's files aren't world-readable.
User=marcellus
WorkingDirectory=$INSTALL_DIR
Environment="MARCELLUS_CONFIG=/etc/marcellus/sidecar.yml"
# Optional, 0600 root-owned file for secrets that shouldn't sit in this unit
# file, e.g. MARCELLUS_PUSH__MQTT_PASSWORD. Leading "-" makes it fine if absent.
EnvironmentFile=-/etc/marcellus-push.env
ExecStart=$INSTALL_DIR/venv/bin/python -m marcellus serve
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
ReadWritePaths=$INSTALL_DIR /etc/marcellus
$EXTRA_RW
# Signal only the main process on stop: the default (control-group) SIGTERMs
# in-flight ffmpeg children out from under the scrub generator.
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now marcellus.service
  systemctl restart marcellus.service
  say "service runs as user 'marcellus' -- if Frigate's recordings/db are not"
  say "readable by it, add group access (e.g. usermod -aG <frigate-group> marcellus)"
  say "done. Check: curl http://localhost:5001/healthz  |  logs: journalctl -fu marcellus"
  say "Config: /etc/marcellus/sidecar.yml (restart with: systemctl restart marcellus)"
fi

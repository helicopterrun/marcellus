#!/usr/bin/env bash
# marcellus installer.
#
#   curl -fsSL https://raw.githubusercontent.com/helicopterrun/marcellus/v0.5.0/install.sh | sudo bash
#
# Docker present  -> compose deployment in $INSTALL_DIR (image from ghcr.io)
# No Docker       -> bare-metal venv + systemd unit
#
# Docker path order: fetch files, work out Frigate's HOST paths (detect, then
# confirm or take from the environment), write .env, validate, and only then
# initialise the config and start the container. A wrong host path is never
# mounted: Docker would silently create an empty root-owned directory there.
#
# Everything is pinned to ONE release, MARCELLUS_VERSION below: the script's own
# companion files (docker-compose.yml, .env.example) come from that release's
# tag, the image is that release's minor train, and the bare-metal install is
# that release's wheel (no git needed). MARCELLUS_VERSION=main installs the
# development branch on purpose (image :latest, pip from git).
#
# Both paths finish by running `fsc doctor` and showing its checklist, then one
# closing line: everything works, or N checks need attention and the exact
# command to run again. That never fails the install, and is skipped with one
# line when the release installed has no `doctor` command.
#
# Options:  --non-interactive   never prompt (also implied without a tty)
# Docker host paths (env override, else detected, else asked):
#   FRIGATE_CONFIG       Frigate's config.yml (file)
#   FRIGATE_DB_DIR       directory containing frigate.db
#   FRIGATE_RECORDINGS   Frigate's recordings directory
#
# Idempotent: re-running upgrades (pulls the new image / pip upgrade) and
# restarts, keeping the values already in .env / sidecar.yml.
set -euo pipefail

# Bumped with __version__ on every release (tests/test_install_script.py fails
# if they disagree). Override for testing, or MARCELLUS_VERSION=main.
MARCELLUS_VERSION="${MARCELLUS_VERSION:-0.5.0}"
REPO="helicopterrun/marcellus"
INSTALL_DIR="${INSTALL_DIR:-/opt/marcellus}"

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# A bare-metal install made before the rename (v0.3 and earlier, then called
# frigate-sidecar) has its own unit, user-less root service, venv and config.
# This script cannot move those safely, so it stops instead of creating a
# second, empty install beside them. Paths are overridable for testing.
LEGACY_UNIT="${LEGACY_UNIT:-/etc/systemd/system/frigate-sidecar.service}"
LEGACY_DIR="${LEGACY_DIR:-/opt/frigate-sidecar}"
LEGACY_CONF="${LEGACY_CONF:-/etc/frigate-sidecar}"
NEW_UNIT="${NEW_UNIT:-/etc/systemd/system/marcellus.service}"

# legacy_install_found: true when an old frigate-sidecar bare-metal install is
# present and no marcellus unit exists yet (i.e. it has not been migrated).
legacy_install_found() {
  [ -e "$NEW_UNIT" ] && return 1
  [ -e "$LEGACY_UNIT" ] || [ -d "$LEGACY_DIR" ] || [ -d "$LEGACY_CONF" ]
}

legacy_install_message() {
  cat <<MSG
An older frigate-sidecar install was found ($LEGACY_UNIT, $LEGACY_DIR or $LEGACY_CONF).
Re-running this installer would create a second, empty Marcellus install beside
it, so nothing has been changed. Move the old install first. Back up first,
then, as root:

  systemctl stop frigate-sidecar
  systemctl disable frigate-sidecar
  cp -a $LEGACY_DIR /opt/marcellus
  cp -a $LEGACY_CONF /etc/marcellus
  rm -rf /opt/marcellus/venv     # the old venv has the old paths baked in; this script rebuilds it
  # then edit /etc/marcellus/sidecar.yml: any path that still points under
  # /opt/frigate-sidecar (notably sidecar.db_path, the database) must point
  # under /opt/marcellus instead. The database file may keep its old name.
  mv /etc/systemd/system/frigate-sidecar.service /root/frigate-sidecar.service.bak

then run this installer again. It keeps your config and installs the new
service. Your old directories are left in place; delete them yourself once the
new service is healthy. Details: "Upgrading from frigate-sidecar" in
docs/deployment.md. To install alongside the old one anyway, re-run with
ALLOW_LEGACY_COEXIST=1.
MSG
}

# ---- version-derived locations ----------------------------------------------
version_plain() { printf '%s' "${MARCELLUS_VERSION#v}"; }

version_valid() {
  case "$(version_plain)" in
    main) return 0 ;;
    *[!0-9A-Za-z.+-]* | "") return 1 ;;
  esac
  printf '%s' "$(version_plain)" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+'
}

release_ref() { # git ref of the release: v0.5.0, or main
  if [ "$(version_plain)" = main ]; then printf 'main'; else printf 'v%s' "$(version_plain)"; fi
}
raw_base()   { printf 'https://raw.githubusercontent.com/%s/%s' "$REPO" "$(release_ref)"; }
wheel_name() { printf 'marcellus-%s-py3-none-any.whl' "$(version_plain)"; }
wheel_url()  { printf 'https://github.com/%s/releases/download/%s/%s' "$REPO" "$(release_ref)" "$(wheel_name)"; }
image_ref() { # release minor train (0.5.0 -> :0.5), not :latest; main -> :latest
  local v; v="$(version_plain)"
  if [ "$v" = main ]; then printf 'ghcr.io/%s:latest' "$REPO"; else printf 'ghcr.io/%s:%s' "$REPO" "${v%.*}"; fi
}

# ---- Frigate host paths (Docker path) ---------------------------------------
PATH_KEYS="FRIGATE_CONFIG FRIGATE_DB_DIR FRIGATE_RECORDINGS"
FRIGATE_SEARCH_ROOTS="${FRIGATE_SEARCH_ROOTS:-/opt/frigate /srv/frigate /mnt/frigate /media/frigate /var/lib/frigate}"
FRIGATE_MOUNTS="${FRIGATE_MOUNTS:-}"   # "destination=source" lines, filled from docker
BAD_PATHS=""                           # "KEY|reason" lines left unresolved

path_what() {
  case "$1" in
    FRIGATE_CONFIG)     printf "Frigate's config.yml (a file)" ;;
    FRIGATE_DB_DIR)     printf "the directory that holds Frigate's frigate.db" ;;
    FRIGATE_RECORDINGS) printf "Frigate's recordings directory (the 'recordings' folder inside its media dir)" ;;
  esac
}

# path_problem KEY VALUE: prints why VALUE is unusable and returns 0; returns 1
# (printing nothing) when it is fine.
path_problem() {
  local key="$1" val="$2"
  [ -n "$val" ] || { echo "not set"; return 0; }
  case "$val" in /*) ;; *) echo "must be an absolute path"; return 0 ;; esac
  case "$val" in
    *[[:space:]\"\'\#\$\\\`]*) echo "contains a space or one of \" ' # \$ \\ \` (not safe in .env)"; return 0 ;;
  esac
  [ -e "$val" ] || { echo "does not exist"; return 0; }
  case "$key" in
    FRIGATE_CONFIG)
      [ -f "$val" ] || { echo "is not a file (give the config.yml itself)"; return 0; }
      ;;
    FRIGATE_DB_DIR|FRIGATE_RECORDINGS)
      [ -d "$val" ] || { echo "is not a directory"; return 0; }
      ;;
  esac
  [ -r "$val" ] || { echo "is not readable"; return 0; }
  if [ "$key" = FRIGATE_DB_DIR ]; then
    [ -f "$val/frigate.db" ] || { echo "has no frigate.db inside it (give the directory, not the file)"; return 0; }
    [ -r "$val/frigate.db" ] || { echo "frigate.db is not readable"; return 0; }
  fi
  return 1
}

# frigate_mounts: "destination=source" mounts of a running Frigate container.
# Read-only inspection; empty when docker or such a container is absent.
frigate_mounts() {
  command -v docker >/dev/null 2>&1 || return 0
  local name
  name="$(docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null \
    | awk 'tolower($2) ~ /frigate/ && tolower($2) !~ /marcellus|frigate-sidecar/ {print $1; exit}')" || true
  [ -n "$name" ] || return 0
  docker inspect -f '{{range .Mounts}}{{.Destination}}={{.Source}}{{"\n"}}{{end}}' "$name" 2>/dev/null || true
}

# candidate_paths KEY: candidate host paths, one per line, best first -- those
# derived from a running Frigate container's mounts, then common locations.
candidate_paths() {
  local key="$1" line dest src root
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    dest="${line%%=*}"; src="${line#*=}"
    case "$key:$dest" in
      FRIGATE_CONFIG:/config) echo "$src/config.yml"; echo "$src/config.yaml" ;;
      FRIGATE_CONFIG:/config/config.yml|FRIGATE_CONFIG:/config/config.yaml) echo "$src" ;;
      FRIGATE_DB_DIR:/config|FRIGATE_DB_DIR:/db|FRIGATE_DB_DIR:/media/frigate) echo "$src" ;;
      FRIGATE_RECORDINGS:/media/frigate) echo "$src/recordings" ;;
      FRIGATE_RECORDINGS:/media/frigate/recordings) echo "$src" ;;
    esac
  done <<EOF
$FRIGATE_MOUNTS
EOF
  for root in $FRIGATE_SEARCH_ROOTS; do
    case "$key" in
      FRIGATE_CONFIG)
        echo "$root/config.yml"; echo "$root/config.yaml"
        echo "$root/config/config.yml"; echo "$root/config/config.yaml" ;;
      FRIGATE_DB_DIR)
        echo "$root/database"; echo "$root/config"; echo "$root" ;;
      FRIGATE_RECORDINGS)
        echo "$root/media/recordings"; echo "$root/recordings" ;;
    esac
  done
}

# best_candidate KEY: the first candidate that passes path_problem.
best_candidate() {
  local c
  while IFS= read -r c; do
    if ! path_problem "$1" "$c" >/dev/null; then printf '%s' "$c"; return 0; fi
  done <<EOF
$(candidate_paths "$1")
EOF
  return 1
}

# env_file_value FILE KEY: the active (uncommented) value in a .env, or empty.
env_file_value() {
  [ -f "$1" ] || return 0
  grep -E "^$2=" "$1" | tail -n 1 | cut -d= -f2- || true
}

# set_env_value FILE KEY VALUE: replace the active line, else the commented
# example line, else append. Other lines are left as they are.
set_env_value() {
  local file="$1" key="$2" val="$3" tmp
  tmp="$(mktemp "$file.XXXXXX")"
  if grep -Eq "^$key=" "$file"; then
    K="$key" V="$val" awk 'BEGIN{k=ENVIRON["K"]; v=ENVIRON["V"]} index($0, k"=")==1 {print k"="v; next} {print}' "$file" >"$tmp"
  elif grep -Eq "^#$key=" "$file"; then
    K="$key" V="$val" awk 'BEGIN{k=ENVIRON["K"]; v=ENVIRON["V"]; d=0} !d && index($0, "#"k"=")==1 {print k"="v; d=1; next} {print}' "$file" >"$tmp"
  else
    cat "$file" >"$tmp"; printf '%s=%s\n' "$key" "$val" >>"$tmp"
  fi
  cat "$tmp" >"$file"; rm -f "$tmp"   # keep the file's mode and owner
}

# resolve_paths ENVFILE INTERACTIVE(1|0): sets RESOLVED_<KEY> for each key.
# Precedence: environment override, then a valid value already in ENVFILE
# (kept silently), then a detected candidate (confirmed when interactive,
# accepted when not). Unresolvable keys are listed in BAD_PATHS, never guessed.
resolve_paths() {
  local envfile="$1" interactive="$2" key val src problem answer
  BAD_PATHS=""
  for key in $PATH_KEYS; do
    val=""; src=""
    if [ -n "${!key:-}" ]; then val="${!key}"; src="from the environment"
    elif [ -n "$(env_file_value "$envfile" "$key")" ]; then val="$(env_file_value "$envfile" "$key")"; src="already in .env"
    else val="$(best_candidate "$key" || true)"; [ -z "$val" ] || src="detected"
    fi
    problem="$(path_problem "$key" "$val" || true)"
    if [ "$interactive" = 1 ] && { [ "$src" = detected ] || [ -n "$problem" ]; }; then
      printf '\n%s\n' "$(path_what "$key")" >&2
      [ -z "$val" ] || printf '  %s: %s%s\n' "${src:-suggested}" "$val" "${problem:+  (PROBLEM: $problem)}" >&2
      while :; do
        printf '  path [%s]: ' "$val" >&2
        IFS= read -r answer || answer=""
        [ -z "$answer" ] && answer="$val"
        problem="$(path_problem "$key" "$answer" || true)"
        [ -z "$problem" ] && { val="$answer"; break; }
        printf '  %s: %s\n' "$answer" "$problem" >&2
      done
    elif [ -n "$problem" ]; then
      BAD_PATHS="$BAD_PATHS$key|${val:-<none found>}|$problem
"
    fi
    printf -v "RESOLVED_$key" '%s' "$val"
  done
  [ -z "$BAD_PATHS" ]
}

paths_stop_message() {
  local line key val why
  printf 'Could not settle Frigate'"'"'s host paths without asking, so nothing was started\n'
  printf '(a wrong path would be mounted as an empty root-owned directory).\n\n'
  while IFS='|' read -r key val why; do
    [ -n "$key" ] || continue
    printf '  %s  %s\n      %s: %s\n' "$key" "$(path_what "$key")" "$val" "$why"
  done <<EOF
$BAD_PATHS
EOF
  cat <<'MSG'

Pass them in the environment and run again, for example:

  curl -fsSL <install.sh url> | sudo FRIGATE_CONFIG=/opt/frigate/config.yml \
    FRIGATE_DB_DIR=/opt/frigate/database \
    FRIGATE_RECORDINGS=/opt/frigate/media/recordings bash

or download install.sh and run `sudo bash install.sh` in a terminal to be asked.
MSG
}

# write_env_values ENVFILE: persist the resolved paths (only what changed).
write_env_values() {
  local key val
  for key in $PATH_KEYS; do
    local ref="RESOLVED_$key"; val="${!ref}"
    [ "$(env_file_value "$1" "$key")" = "$val" ] || set_env_value "$1" "$key" "$val"
  done
}

# recordings_empty DIR: true when DIR has no entries (a new Frigate: warn only).
recordings_empty() { [ -z "$(ls -A "$1" 2>/dev/null)" ]; }

# wait_healthy [SECONDS]: wait for the marcellus container's health check.
wait_healthy() {
  local deadline=$((SECONDS + ${1:-180})) st
  while [ "$SECONDS" -lt "$deadline" ]; do
    st="$(docker inspect -f '{{.State.Health.Status}}' marcellus 2>/dev/null || true)"
    [ "$st" = healthy ] && return 0
    [ "$(docker inspect -f '{{.State.Running}}' marcellus 2>/dev/null || echo false)" = true ] || return 2
    sleep 2
  done
  return 1
}

# ---- the closing checklist (fsc doctor) --------------------------------------
# Both install paths end by running `fsc doctor` and showing it. The install
# itself already succeeded, so nothing here ever fails the script: it prints the
# checklist, then one closing line, either that everything works or how many
# checks need attention and the exact command to run again.
PUSH_ENV_FILE="${PUSH_ENV_FILE:-/etc/marcellus-push.env}"
SYSTEMD_CONFIG="${SYSTEMD_CONFIG:-/etc/marcellus/sidecar.yml}"

# as_service_user CMD...: run as the marcellus user when we are root and it
# exists, otherwise as ourselves.
as_service_user() {
  if [ "$(id -u)" -eq 0 ] && id -u marcellus >/dev/null 2>&1 && command -v runuser >/dev/null 2>&1; then
    runuser -u marcellus -- "$@"
  else
    "$@"
  fi
}

# systemd_fsc ARGS...: the venv's `fsc` as the service user, with the same MQTT
# password the unit gets from its EnvironmentFile (read here, never put on a
# command line).
systemd_fsc() {
  (
    pw="$(env_file_value "$PUSH_ENV_FILE" MARCELLUS_PUSH__MQTT_PASSWORD)"
    case "$pw" in
      \'*\') pw="${pw#\'}"; pw="${pw%\'}" ;;
      \"*\") pw="${pw#\"}"; pw="${pw%\"}" ;;
    esac
    [ -z "$pw" ] || export MARCELLUS_PUSH__MQTT_PASSWORD="$pw"
    export MARCELLUS_CONFIG="$SYSTEMD_CONFIG"
    as_service_user "$INSTALL_DIR/venv/bin/fsc" "$@"
  )
}

# doctor_exec KIND ARGS...: `fsc ARGS` the way this install runs it.
doctor_exec() {
  local kind="$1"; shift
  case "$kind" in
    docker)  docker compose exec -T marcellus fsc "$@" ;;
    systemd) systemd_fsc "$@" ;;
    *) return 127 ;;
  esac
}

# doctor_rerun_command KIND: the command a person types to run the checklist again.
doctor_rerun_command() {
  case "$1" in
    docker) printf 'cd %s && docker compose exec marcellus fsc doctor' "$INSTALL_DIR" ;;
    systemd)
      if [ -f "$PUSH_ENV_FILE" ]; then
        printf "sudo bash -c 'set -a; . %s; exec %s/venv/bin/fsc doctor --config %s'" \
          "$PUSH_ENV_FILE" "$INSTALL_DIR" "$SYSTEMD_CONFIG"
      else
        printf 'sudo %s/venv/bin/fsc doctor --config %s' "$INSTALL_DIR" "$SYSTEMD_CONFIG"
      fi ;;
  esac
}

# doctor_available KIND: does this release's `fsc` have a doctor command? An
# older image (MARCELLUS_VERSION pointing before it was added) does not.
doctor_available() {
  local help
  help="$(doctor_exec "$1" --help 2>&1 </dev/null || true)"
  case "$help" in *doctor*) return 0 ;; esac
  return 1
}

# finish_with_doctor KIND: print the checklist and the closing line. Always returns 0.
finish_with_doctor() {
  local kind="$1" out rc=0 n cmd
  cmd="$(doctor_rerun_command "$kind")"
  say "checking the install (fsc doctor)"
  if ! doctor_available "$kind"; then
    say "skipping the checklist: this release has no 'fsc doctor' (after upgrading, run: $cmd)"
    return 0
  fi
  out="$(doctor_exec "$kind" doctor --no-color --mqtt-wait 10 2>&1 </dev/null)" || rc=$?
  printf '%s\n' "$out"
  n="$(printf '%s\n' "$out" | grep -cE '^ (✗|!) ' || true)"
  n="${n:-0}"
  if [ "$rc" -eq 0 ] && [ "$n" -eq 0 ]; then
    say "everything is working"
  elif [ "$n" -eq 0 ]; then
    say "the checklist did not finish (see above): fix that and run \`$cmd\` again"
  elif [ "$n" -eq 1 ]; then
    say "1 check needs attention: fix it and run \`$cmd\` again"
  else
    say "$n checks need attention: fix them and run \`$cmd\` again"
  fi
  return 0
}

# wait_service_active [SECONDS]: best-effort wait for the systemd unit to be up.
wait_service_active() {
  local deadline=$((SECONDS + ${1:-20}))
  while [ "$SECONDS" -lt "$deadline" ]; do
    systemctl is-active --quiet marcellus 2>/dev/null && return 0
    sleep 1
  done
  return 1
}

# Sourcing with MARCELLUS_INSTALL_SOURCE_ONLY=1 loads the functions above only
# (used by tests/test_install_script.py).
if [ "${MARCELLUS_INSTALL_SOURCE_ONLY:-}" = 1 ]; then return 0 2>/dev/null || exit 0; fi

[ "$(id -u)" -eq 0 ] || die "run as root (sudo) -- installs into $INSTALL_DIR"

if [ "${ALLOW_LEGACY_COEXIST:-}" != 1 ] && legacy_install_found; then
  legacy_install_message >&2
  exit 1
fi

NON_INTERACTIVE=0
for arg in "$@"; do
  case "$arg" in
    --non-interactive) NON_INTERACTIVE=1 ;;
    *) die "unknown option: $arg" ;;
  esac
done
INTERACTIVE=0
if [ "$NON_INTERACTIVE" = 0 ] && [ "${MARCELLUS_NON_INTERACTIVE:-}" != 1 ] && [ -t 0 ]; then INTERACTIVE=1; fi

version_valid || die "MARCELLUS_VERSION='$MARCELLUS_VERSION' is not a release like 0.5.0 (or 'main')"
[ "$(version_plain)" != main ] || warn "MARCELLUS_VERSION=main: installing the development branch (image :latest), not a release"

fetch() { # fetch <relpath> <dest> -- never clobber an existing user-edited file
  if [ -e "$2" ]; then
    say "keeping existing $2"
  else
    curl -fsSL "$(raw_base)/$1" -o "$2" || die "could not download $(raw_base)/$1 (is release $(release_ref) real, and is the network up?)"
  fi
}

mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/data"

if command -v docker >/dev/null 2>&1; then
  say "Docker found -- installing compose deployment in $INSTALL_DIR (release $(release_ref))"
  docker compose version >/dev/null 2>&1 || die "docker is present but 'docker compose' is not; install the compose plugin"

  cd "$INSTALL_DIR"
  # 1. files from the same release as this script
  if [ -e docker-compose.yml ]; then
    # The compose file is not meant to be edited (every host setting is in .env)
    # and carries the image pin, so an upgrade must replace it; keep a copy.
    curl -fsSL "$(raw_base)/docker-compose.yml" -o docker-compose.yml.new \
      || die "could not download $(raw_base)/docker-compose.yml"
    if cmp -s docker-compose.yml docker-compose.yml.new; then
      rm -f docker-compose.yml.new
    else
      mv docker-compose.yml "docker-compose.yml.bak-$(date +%Y%m%d%H%M%S)"
      mv docker-compose.yml.new docker-compose.yml
      say "updated docker-compose.yml to $(release_ref) (old one kept as docker-compose.yml.bak-*)"
    fi
  else
    fetch docker-compose.yml docker-compose.yml
  fi
  curl -fsSL "$(raw_base)/.env.example" -o .env.example \
    || die "could not download $(raw_base)/.env.example"
  [ -e .env ] || cp .env.example .env

  # 2. Frigate's host paths, settled BEFORE anything starts
  say "locating Frigate's files on this host"
  FRIGATE_MOUNTS="$(frigate_mounts)"
  if [ -n "$FRIGATE_MOUNTS" ]; then say "found a running Frigate container; using its mounts as suggestions"; fi
  if ! resolve_paths "$INSTALL_DIR/.env" "$INTERACTIVE"; then
    paths_stop_message >&2
    exit 1
  fi

  # 3. record and validate them
  write_env_values "$INSTALL_DIR/.env"
  for key in $PATH_KEYS; do
    ref="RESOLVED_$key"; say "$key=${!ref}"
  done
  if recordings_empty "$RESOLVED_FRIGATE_RECORDINGS"; then
    warn "$RESOLVED_FRIGATE_RECORDINGS is empty -- fine for a brand-new Frigate with no recordings yet; otherwise check the path"
  fi
  # data dir must be writable by the container's non-root uid
  chown -R 10001:10001 "$INSTALL_DIR/data"

  IMAGE="$(image_ref)"
  say "pulling $IMAGE"
  docker pull "$IMAGE"

  # 4. config, then start. The host paths are already known, so `init` is told
  # the matching in-container paths (see docker-compose.yml's volumes) instead
  # of prompting for them; only Frigate's URLs are still asked.
  if [ ! -f "$INSTALL_DIR/config/sidecar.yml" ]; then
    init_args=(init -o /config/sidecar.yml
      --frigate-config /opt/frigate/config.yml
      --frigate-db /opt/frigate/database/frigate.db
      --recordings-path "$RESOLVED_FRIGATE_RECORDINGS"
      --sidecar-db /data/marcellus.db)
    if [ "$INTERACTIVE" = 1 ]; then
      say "generating config (Frigate's folders come from .env; init finds the rest in Frigate and asks you to confirm)"
      docker compose run --rm marcellus "${init_args[@]}"
    else
      warn "not interactive -- writing a default config; edit $INSTALL_DIR/config/sidecar.yml (Frigate's URLs)"
      docker compose run --rm marcellus "${init_args[@]}" --non-interactive
    fi
  fi

  say "starting"
  docker compose up -d
  say "waiting for the container to report healthy"
  rc=0; wait_healthy "${HEALTH_WAIT_S:-180}" || rc=$?
  case "$rc" in
    0) ;;
    2) die "the container exited; see: docker logs marcellus" ;;
    *) die "not healthy after ${HEALTH_WAIT_S:-180}s; see: docker logs marcellus" ;;
  esac
  host_ip="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  say "done. Open http://${host_ip:-localhost}:5001  |  logs: docker logs -f marcellus"
  say "Host paths: $INSTALL_DIR/.env  |  settings: $INSTALL_DIR/config/sidecar.yml (then: docker compose up -d)"
  finish_with_doctor docker || true
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
  # Marcellus is not on PyPI: install the wheel the release workflow attaches
  # to the GitHub release (no git needed). main is only for MARCELLUS_VERSION=main.
  if [ "$(version_plain)" = main ]; then
    command -v git >/dev/null 2>&1 || die "MARCELLUS_VERSION=main installs from git; git is required"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade "marcellus[http2] @ git+https://github.com/$REPO"
  else
    WHEEL_DIR="$(mktemp -d)"
    WHEEL="$WHEEL_DIR/$(wheel_name)"
    say "downloading $(wheel_url)"
    curl -fsSL "$(wheel_url)" -o "$WHEEL" \
      || die "could not download $(wheel_url) -- check the network and that release $(release_ref) exists. Nothing was installed (MARCELLUS_VERSION=main installs the development branch instead)."
    "$INSTALL_DIR/venv/bin/python" -c 'import sys, zipfile; sys.exit(0 if zipfile.is_zipfile(sys.argv[1]) else 1)' "$WHEEL" \
      || die "the download from $(wheel_url) is not a wheel; nothing was installed"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade "$WHEEL[http2]"
    rm -rf "$WHEEL_DIR"
  fi

  mkdir -p /etc/marcellus
  if [ ! -f /etc/marcellus/sidecar.yml ]; then
    if [ -t 0 ]; then
      say "generating config (init finds Frigate's settings and asks you to confirm them)"
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
  wait_service_active 20 || true
  sleep 3   # let the server bind its port before the checklist asks it
  finish_with_doctor systemd || true
fi

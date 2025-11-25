#!/usr/bin/env bash
# OpenMux Raspberry Pi Installation / Upgrade Script
#
# Idempotent: safe to re-run. Creates/updates a virtualenv, installs the
# specified version (git ref or PyPI), sets up systemd units, and validates
# configuration.
#
# Usage:
#   curl -fsSL https://example.org/openmux-install.sh | sudo bash -s -- \
#       --version 1.0.0 --user openmux --group openmux \
#       --config /etc/openmux/server.yaml
#
#   # Dev (git) install:
#   sudo ./openmux-install.sh --git https://github.com/yourorg/openmux.git --ref main
#
#   # Local source install (from an already extracted source directory):
#   sudo ./openmux-install.sh --src-dir /path/to/openmux-source
#
set -euo pipefail

VERSION=""
GIT_URL=""
GIT_REF="main"
SRC_DIR=""
OM_USER="openmux"
OM_GROUP="openmux"
PREFIX="/opt/openmux"
VENV_DIR="$PREFIX/venv"
BIN_DIR="$VENV_DIR/bin"
SYSTEMD_DIR="/etc/systemd/system"
CONFIG_DST="/etc/openmux/server.yaml"
AUTO_ENABLE=1
PYTHON_BIN="python3"

log() { echo -e "[openmux-install] $*"; }
fail() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2;;
    --git) GIT_URL="$2"; shift 2;;
    --ref) GIT_REF="$2"; shift 2;;
    --src-dir|--src|--local-dir) SRC_DIR="$2"; shift 2;;
    --user) OM_USER="$2"; shift 2;;
    --group) OM_GROUP="$2"; shift 2;;
    --prefix) PREFIX="$2"; shift 2;;
    --config) CONFIG_DST="$2"; shift 2;;
    --python) PYTHON_BIN="$2"; shift 2;;
    --no-enable) AUTO_ENABLE=0; shift;;
    *) fail "Unknown arg: $1";;
  esac
done

if [[ -z "$VERSION" && -z "$GIT_URL" && -z "$SRC_DIR" ]]; then
  fail "Provide either --version (PyPI) or --git <url> [--ref <ref>] or --src-dir <path>"
fi

log "Ensuring system packages (build tools, python venv)"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip python3-dev build-essential git ca-certificates rsync

# Create user/group if missing
if ! id -u "$OM_USER" >/dev/null 2>&1; then
  log "Creating user $OM_USER"
  useradd --system --create-home --shell /usr/sbin/nologin "$OM_USER" || true
fi
if ! getent group "$OM_GROUP" >/dev/null 2>&1; then
  groupadd --system "$OM_GROUP" || true
fi
usermod -a -G "$OM_GROUP" "$OM_USER" || true

install_root="$PREFIX/versions"
mkdir -p "$install_root" "$PREFIX/log" /etc/openmux "$PREFIX"

if [[ ! -d "$VENV_DIR" ]]; then
  log "Creating virtualenv in $VENV_DIR"
  $PYTHON_BIN -m venv "$VENV_DIR"
fi

. "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip wheel setuptools

if [[ -n "$VERSION" ]]; then
  log "Installing openmux==$VERSION from PyPI"
  pip install "openmux==$VERSION"
  src_dir=$(python -c 'import inspect,openmux,os;print(os.path.dirname(inspect.getfile(openmux)))')
  # Copy package tree and docs into PREFIX (no delete; keep venv/log)
  rsync -a "$src_dir/" "$PREFIX/" || cp -a "$src_dir"/* "$PREFIX/"
elif [[ -n "$GIT_URL" ]]; then
  log "Installing from git $GIT_URL@$GIT_REF"
  tmp=$(mktemp -d)
  git clone --depth 1 --branch "$GIT_REF" "$GIT_URL" "$tmp/repo"
  pushd "$tmp/repo" >/dev/null
  pip install .
  rsync -a . "$PREFIX/"
  popd >/dev/null
  rm -rf "$tmp"
else
  # Local extracted source directory
  if [[ ! -d "$SRC_DIR" ]]; then
    fail "--src-dir '$SRC_DIR' does not exist or is not a directory"
  fi
  if [[ ! -f "$SRC_DIR/pyproject.toml" && ! -f "$SRC_DIR/setup.py" ]]; then
    log "WARNING: '$SRC_DIR' does not contain pyproject.toml or setup.py; attempting install regardless"
  fi
  log "Installing from local source directory: $SRC_DIR"
  pushd "$SRC_DIR" >/dev/null
  pip install .
  # Copy the working tree into PREFIX (preserve existing venv/logs)
  rsync -a . "$PREFIX/"
  popd >/dev/null
fi

# Sync selected config templates into /etc/openmux without overwriting admin changes
if [[ -d "$PREFIX/config" ]]; then
  log "Ensuring baseline config files exist in /etc/openmux"
  for cfg in server.yaml authentication.yaml security.yaml; do
    if [[ -f "$PREFIX/config/$cfg" && ! -f "/etc/openmux/$cfg" ]]; then
      cp "$PREFIX/config/$cfg" "/etc/openmux/$cfg"
    fi
  done
fi

# Permissions
chown -R "$OM_USER":"$OM_GROUP" "$PREFIX" /etc/openmux

# Ensure server config exists (no auto-generation)
if [[ ! -f "$CONFIG_DST" ]]; then
  template="$PREFIX/config/server.yaml"
  if [[ -f "$template" ]]; then
    cp "$template" "$CONFIG_DST"
    chown "$OM_USER":"$OM_GROUP" "$CONFIG_DST"
    chmod 640 "$CONFIG_DST"
  else
    fail "Missing server config at $CONFIG_DST and no template found; please supply one via --config"
  fi
fi

# Create default client config if missing by copying template when available
CLIENT_CFG="/etc/openmux/client.yaml"
if [[ ! -f "$CLIENT_CFG" ]]; then
  template="$PREFIX/client.yaml"
  if [[ -f "$template" ]]; then
    cp "$template" "$CLIENT_CFG"
    chown "$OM_USER":"$OM_GROUP" "$CLIENT_CFG"
    chmod 640 "$CLIENT_CFG"
  else
    log "WARNING: $CLIENT_CFG is missing and no template was found; please provide one manually"
  fi
fi

# Systemd unit
cat > "$SYSTEMD_DIR/openmux-server.service" <<UNIT
[Unit]
Description=OpenMux Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$OM_USER
Group=$OM_GROUP
WorkingDirectory=$PREFIX
Environment=PYTHONUNBUFFERED=1
ExecStart=$BIN_DIR/python -m openmux.server.main -c $CONFIG_DST
ExecReload=/bin/kill -HUP \$MAINPID
# Allow binding to privileged ports (e.g., 80) without running as root
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
Restart=on-failure
RestartSec=5
StandardOutput=append:$PREFIX/log/server.stdout.log
StandardError=append:$PREFIX/log/server.stderr.log

[Install]
WantedBy=multi-user.target
UNIT

# Consolidated client wrapper script
mkdir -p /usr/local/bin
cat > /usr/local/bin/openmux <<'EOS'
#!/usr/bin/env bash
# Consolidated OpenMux client wrapper
CLIENT="/opt/openmux/venv/bin/openmux-client"
if [[ ! -x "$CLIENT" ]]; then
  echo "ERROR: $CLIENT not found; is OpenMux installed?" >&2
  exit 127
fi
if [[ "$1" == "-h" || "$1" == "--help" ]]; then
  exec "$CLIENT" "$@"
fi
if [[ $# -eq 0 ]]; then
  echo "Listing ports..." >&2
  exec "$CLIENT" -l
fi
exec "$CLIENT" "$@"
EOS
chmod 755 /usr/local/bin/openmux

# Remove legacy wrappers if present
rm -f /usr/local/bin/openmux-console || true

systemctl daemon-reload
if [[ $AUTO_ENABLE -eq 1 ]]; then
  systemctl enable openmux-server.service
  systemctl restart openmux-server.service || systemctl start openmux-server.service
  log "Service openmux-server (re)started"
else
  log "Service installed but not enabled (--no-enable used)"
fi

# Login banner with logo and quick usage (profile.d)
cat > /etc/profile.d/openmux.sh <<'EOF'
# OpenMux login banner (shown once per interactive session)
if [ -n "$PS1" ] && [ -t 1 ] && [ -z "$OPENMUX_BANNER_SHOWN" ]; then
  export OPENMUX_BANNER_SHOWN=1
  # ANSI colors (fallback-safe)
  c_reset="\033[0m"; c_cyan="\033[36m"; c_green="\033[32m"; c_yellow="\033[33m"; c_magenta="\033[35m"
  # Simple ASCII logo
  cat <<'BANNER'
   ____                   __  __            
  / __ \                 |  \/  |           
 | |  | |_ __   ___ _ __ | \  / |_   ___  __
 | |  | | '_ \ / _ \ '_ \| |\/| | | | \ \/ /
 | |__| | |_) |  __/ | | | |  | | |_| |>  < 
  \____/| .__/ \___|_| |_|_|  |_|\__,_/_/\_\
        | |                                 
        |_|      
BANNER
  echo -e "${c_cyan}OpenMux${c_reset} — serial console multiplexing"
  echo -e "${c_yellow}Try:${c_reset}  openmux                     ${c_magenta}# list ports on localhost (websocket)${c_reset}"
  echo -e "       openmux loopback1          ${c_magenta}# connect to a port${c_reset}"
  echo -e "       openmux -s 127.0.0.1 -l    ${c_magenta}# list ports on a host${c_reset}"
  echo -e "       openmux --help              ${c_magenta}# client help${c_reset}"
fi
EOF
chmod 644 /etc/profile.d/openmux.sh

log "Installation complete. Logs: $PREFIX/log/server.*"

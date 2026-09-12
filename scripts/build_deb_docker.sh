#!/usr/bin/env bash
#
# build_deb_docker.sh - build an OpenMux .deb in a fresh Debian trixie container.
#
# Two common modes:
#   1) From a release tag (clean version, e.g. openmux_1.0.2-1_all.deb):
#        ./scripts/build_deb_docker.sh v1.0.2
#   2) From HEAD (snapshot version, e.g. openmux_1.0.2-48~git202608271201.ab12cd3_all.deb):
#        ./scripts/build_deb_docker.sh              # default: main (latest HEAD)
#        ./scripts/build_deb_docker.sh feature-x    # any other branch
#
# A tag build gets the clean version (1.0.2-1). A branch/HEAD build gets a
# ~git<timestamp>.<sha> snapshot suffix so repeated builds never claim the
# same .deb version.
#
# The container installs every build prerequisite with apt, clones the
# repository from GitHub, runs `make deb`, and copies the .deb and .changes
# into the output directory. The package is Architecture: all, so the
# resulting .deb installs on any Debian / Raspberry Pi OS machine
# (arm64, amd64, ...).

set -euo pipefail

REPO="https://github.com/OpenMux/openmux-server.git"
REF="main"
SNAPSHOT=""
OUT_DIR="./deb-out"
IMAGE="debian:trixie"

usage() {
  # Only the header block, i.e. everything before the first set statement,
  # minus the shebang.
  sed -n '/^set -euo pipefail/q;p' "$0" | tail -n +2 | grep '^#' | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -o)      OUT_DIR="${2:?-o needs a directory}"; shift 2 ;;
    -s)      SNAPSHOT="force"; shift ;;
    --image) IMAGE="${2:?--image needs a value}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    -*)      echo "Unknown option: $1" >&2; usage; exit 1 ;;
    *)       REF="$1"; shift ;;
  esac
done

command -v docker >/dev/null 2>&1 || { echo "error: docker is not on PATH" >&2; exit 1; }
mkdir -p "$OUT_DIR"

# Tag build => clean version. Branch/HEAD build => snapshot suffix.
if [[ -z "$SNAPSHOT" ]]; then
  if [[ "$REF" =~ ^v[0-9] ]]; then SNAPSHOT="off"; else SNAPSHOT="auto"; fi
fi

echo "==> Ref: $REF | Snapshot: $SNAPSHOT | Output: $OUT_DIR | Image: $IMAGE"

docker run --rm \
  -v "$(cd "$OUT_DIR" && pwd):/out" \
  -e REPO="$REPO" -e REF="$REF" -e SNAPSHOT="$SNAPSHOT" \
  "$IMAGE" \
  bash -lc '
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# --- build prerequisites ---------------------------------------------
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates make git fakeroot dpkg-dev build-essential \
  debhelper dh-python pybuild-plugin-pyproject \
  python3-all python3-setuptools python3-wheel python3-setuptools-scm python3-pip

# --- fetch the exact ref (full clone: setuptools-scm needs the v* tags) --
mkdir -p /work && cd /work
git clone "$REPO" openmux-server
cd openmux-server
if ! git checkout --detach "$REF" 2>/dev/null && \
   ! git checkout --detach "$REF^{commit}" 2>/dev/null; then
  echo "error: ref not found: $REF" >&2
  exit 1
fi
echo "==> Building from $(git describe --tags --always) (commit $(git rev-parse --short HEAD), dirty files: $(git status --porcelain | wc -l | tr -d " "))"

# --- build -------------------------------------------------------------
echo "==> Running make deb (SNAPSHOT=$SNAPSHOT)..."
if [[ "$SNAPSHOT" == "auto" ]]; then
  make deb DEB_SNAPSHOT=auto
else
  make deb
fi

# collect artifacts: dpkg-buildpackage writes them next to the repo dir (/work)
find /work -maxdepth 1 \( -name "openmux_*.deb" -o -name "openmux_*.changes" \) -exec cp -v {} /out/ \;
echo "==> Artifacts in /out:"
ls -lh /out
'

echo
echo "==> Done. Install on the target with, e.g.:"
echo "    sudo apt install $OUT_DIR/openmux_*.deb"

#!/usr/bin/env bash
# Set up HCAS on a fresh Linux box for single-user testing on the Claude CLI.
#
# This is the personal-box path, not the hosted one. It deliberately does NOT
# use Docker: the container exists to make a deploy reproducible for other
# people, and it actively fights the CLI path, which needs your own OAuth
# credentials in ~/.claude. See DEPLOY.md for the hosted (container) route.
#
# Idempotent on purpose. An Oracle Always Free instance can be reclaimed when
# it is idle, so the cost of losing the box should be one command, not an
# evening. Re-running this on a fresh instance rebuilds everything.
#
#   ./deploy/setup-linux.sh
#
# Afterwards: `claude` once to log in, then `sudo systemctl start hcas`.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${HCAS_DATA_DIR:-$HOME/hcas-data}"
PORT="${HCAS_PORT:-8000}"
RUN_USER="$(id -un)"
VENV="$REPO_DIR/.venv"

say() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
warn() { printf '\n\033[33m!!  %s\033[0m\n' "$1"; }

# --- 0. What are we on? -----------------------------------------------------
ARCH="$(uname -m)"
. /etc/os-release
say "Detected $PRETTY_NAME on $ARCH"

case "$ARCH" in
    x86_64|aarch64) ;;
    *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

# Every dependency in requirements.txt publishes an aarch64 wheel (pyogrio's is
# an abi3 wheel, so one build covers 3.11+), which is what makes an Ampere A1
# instance viable at all — nothing here compiles GDAL from source.

case "$ID" in
    ubuntu|debian) PKG=apt ;;
    ol|rhel|fedora|centos|rocky|almalinux) PKG=dnf ;;
    *) echo "Unrecognised distro '$ID'. Install python3 + venv + git by hand, then re-run." >&2; exit 1 ;;
esac

if [ "$ID" = "ol" ]; then
    warn "Oracle Linux is not on Claude Code's supported-OS list (Ubuntu 20.04+,
    Debian 10+, Alpine 3.19+). Its RHEL-family dnf repo is published and Oracle
    Linux is RHEL-compatible, so this usually works — but if you have not built
    the instance yet, choose the Ubuntu image and avoid the question entirely."
fi

# --- 1. System packages -----------------------------------------------------
say "Installing system packages"
if [ "$PKG" = apt ]; then
    sudo apt-get update -qq
    # python3-venv is a separate package on Debian/Ubuntu and its absence is a
    # confusing failure: `python3 -m venv` exits non-zero telling you to apt
    # install the very thing you just tried to use.
    sudo apt-get install -y -qq python3 python3-venv python3-pip git curl
else
    sudo dnf install -y -q python3 python3-pip git curl
fi

python3 --version

# --- 2. Python environment --------------------------------------------------
say "Building the virtualenv"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -r "$REPO_DIR/requirements.txt"
"$VENV/bin/python" -c "import geopandas, osmnx, folium; print('geospatial stack OK')"

# --- 3. Data that must outlive a `git pull` ---------------------------------
# The SQLite database and the osmnx tile cache live outside the repo. osmnx
# reads its cache path from ox.settings.cache_folder, which defaults to ./cache
# relative to the working directory — hence a symlink rather than an env var.
say "Preparing $DATA_DIR"
mkdir -p "$DATA_DIR/osm-cache"
if [ ! -L "$REPO_DIR/cache" ]; then
    rm -rf "$REPO_DIR/cache"
    ln -s "$DATA_DIR/osm-cache" "$REPO_DIR/cache"
fi

# --- 4. Claude Code ---------------------------------------------------------
if command -v claude >/dev/null 2>&1; then
    say "Claude CLI already installed: $(claude --version)"
else
    say "Installing Claude Code"
    curl -fsSL https://claude.ai/install.sh | bash
fi

CLAUDE_BIN="$(command -v claude || echo "$HOME/.local/bin/claude")"
CLAUDE_DIR="$(dirname "$CLAUDE_BIN")"

# --- 5. systemd unit --------------------------------------------------------
# Two settings below are the whole reason this script exists rather than a
# README paragraph. Both fail in the same misleading way — every step 2 and
# step 4 returns "no API key configured", pointing at a key you deliberately
# did not set — because hcas._llm_structured falls through to the CLI only when
# no key is present, and _llm_via_claude_cli then cannot find or authenticate
# the binary:
#
#   PATH  systemd's default PATH does not include ~/.local/bin, which is where
#         the native installer puts `claude`. shutil.which("claude") returns
#         None and the step raises before it ever runs.
#   HOME  the CLI reads AND REFRESHES its OAuth token under ~/.claude. Without
#         a correct, writable HOME it cannot authenticate, and a token refresh
#         has nowhere to land.
say "Writing /etc/systemd/system/hcas.service"
sudo tee /etc/systemd/system/hcas.service >/dev/null <<UNIT
[Unit]
Description=HCAS (Healthy Community Assessment System)
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=$RUN_USER
WorkingDirectory=$REPO_DIR
Environment=HOME=$HOME
Environment=PATH=$CLAUDE_DIR:/usr/local/bin:/usr/bin:/bin
Environment=HCAS_DB=$DATA_DIR/hcas.db
Environment=PYTHONUNBUFFERED=1
# Bound to loopback on purpose: reach it with an SSH tunnel rather than opening
# a port. Nothing is exposed, so there is no TLS to terminate and no shared
# password to leak. Publishing it later is a reverse proxy away.
ExecStart=$VENV/bin/uvicorn backend.app:app --host 127.0.0.1 --port $PORT --workers 1 --timeout-keep-alive 950
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable hcas >/dev/null

# --- 6. What is left for a human -------------------------------------------
cat <<NEXT

$(say "Installed. Two steps left, both yours:")

1. Log in to Claude Code (opens a browser flow; on a headless box, open the
   printed URL on your laptop and paste the code back):

       claude

   Leave ANTHROPIC_API_KEY unset and out of .env — an absent key is exactly
   what selects the subscription path. Confirm with:

       claude --version && grep -c ANTHROPIC_API_KEY $REPO_DIR/.env || true

2. Start it:

       sudo systemctl start hcas
       systemctl status hcas --no-pager
       journalctl -u hcas -f

Then, from your laptop:

       ssh -L $PORT:localhost:$PORT $RUN_USER@<this-box>

and open http://localhost:$PORT/hcas

NEXT

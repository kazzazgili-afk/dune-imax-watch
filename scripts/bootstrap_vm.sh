#!/usr/bin/env bash
# One-shot setup for an always-on Linux VM (Debian/Ubuntu).
#
# Run it from a fresh checkout on the VM:
#   git clone https://github.com/kazzazgili-afk/dune-imax-watch.git
#   cd dune-imax-watch && ./scripts/bootstrap_vm.sh
#
# It deliberately never handles secrets: it writes a 0600 template and stops so you
# can fill it in yourself.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UA="dune-imax-watch/0.1 (personal use; contact: kazzazgili@gmail.com)"
PAGE="https://www.sciencemuseum.org.uk/see-and-do/dune-part-three"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# --- 1. Preflight: can this host even see the museum page? ------------------------
# GitHub Actions' IP ranges get 403 here. Datacenter IPs generally do not, but find
# out now rather than after a month of silent half-coverage.
say "Preflight: checking this host can reach the Science Museum page"
code="$(curl -sS -o /dev/null -m 25 -w '%{http_code}' -A "$UA" "$PAGE" || echo 000)"
case "$code" in
    200) echo "    HTTP 200 - the page channel will work from this host." ;;
    403) cat >&2 <<EOF
    HTTP 403 - this host's IP is blocked by sciencemuseum.org.uk.

    The mailbox and Bluesky channels will still work, but the page channel will not,
    which is most of the reason to run a VM at all. Consider a different provider or
    region, or run this on a machine on a home connection instead.
EOF
         read -r -p "    Continue anyway? [y/N] " ans
         [[ "${ans:-N}" =~ ^[Yy]$ ]] || exit 1 ;;
    *)   echo "    HTTP $code - unexpected; check networking before continuing." >&2
         read -r -p "    Continue anyway? [y/N] " ans
         [[ "${ans:-N}" =~ ^[Yy]$ ]] || exit 1 ;;
esac

# --- 2. Dependencies -------------------------------------------------------------
say "Installing python3-venv and curl"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip curl

say "Creating the virtualenv"
python3 -m venv "$PROJECT_DIR/.venv"
"$PROJECT_DIR/.venv/bin/pip" install -q --upgrade pip
"$PROJECT_DIR/.venv/bin/pip" install -q -r "$PROJECT_DIR/requirements.txt"

# --- 3. Config, adjusted for a headless host -------------------------------------
say "Writing config/config.yaml (headless: no desktop notifications, no browser)"
if [ -f "$PROJECT_DIR/config/config.yaml" ]; then
    echo "    config/config.yaml already exists - leaving it alone."
else
    "$PROJECT_DIR/.venv/bin/python" - "$PROJECT_DIR" <<'PY'
import pathlib, sys, re
root = pathlib.Path(sys.argv[1])
text = (root / "config/config.example.yaml").read_text()
# There is no display or default browser on a server.
text = re.sub(r"(    macos_native:\n      enabled: )true", r"\1false", text)
text = re.sub(r"(  auto_open_booking_link:\n    enabled: )true", r"\1false", text)
(root / "config/config.yaml").write_text(text)
print("    wrote config/config.yaml")
PY
fi

# --- 4. Secrets template (never populated by this script) -------------------------
say "Preparing the secrets file"
ENV_FILE="$PROJECT_DIR/deploy/dune-watch.env"
if [ -f "$ENV_FILE" ]; then
    echo "    $ENV_FILE already exists - leaving it alone."
else
    cp "$PROJECT_DIR/deploy/dune-watch.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "    wrote $ENV_FILE (0600) - you must fill it in before starting the service."
fi

# --- 5. systemd ------------------------------------------------------------------
say "Installing the systemd unit"
UNIT=/etc/systemd/system/dune-watch.service
sudo cp "$PROJECT_DIR/deploy/dune-watch.service" "$UNIT"
# The shipped unit assumes /opt/dune-imax-watch and a dedicated user; point it at this
# checkout and whoever is running the bootstrap.
sudo sed -i "s#/opt/dune-imax-watch#$PROJECT_DIR#g" "$UNIT"
sudo sed -i "s#^User=.*#User=$(id -un)#" "$UNIT"
sudo systemctl daemon-reload

cat <<EOF

$(say "Almost done - two manual steps")

1. Put your real secrets in:
       $ENV_FILE
   Use the SAME 16-character Gmail app password for DUNE_WATCH_IMAP_PASS and
   DUNE_WATCH_SMTP_PASS.

2. Start it:
       sudo systemctl enable --now dune-watch
       journalctl -u dune-watch -f

Then sanity-check the setup:
       $PROJECT_DIR/.venv/bin/python -m dune_watch robots-check --config $PROJECT_DIR/config/config.yaml
       $PROJECT_DIR/.venv/bin/python -m dune_watch test-notify --config $PROJECT_DIR/config/config.yaml

EOF

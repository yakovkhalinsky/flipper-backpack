#!/usr/bin/env bash
# OPTIONAL: install Kismet for the kismet-* Flipper tools. Run after install.sh:
#   sudo ./install-kismet.sh
# - Kismet from the official Kismet apt repo (Raspberry Pi OS / Debian bookworm, arm64),
#   repo key dearmored into its own keyring and pinned with signed-by=.
# - Only kismet-core + the Linux Wi-Fi capture helper. No extra capture/attack plugins.
# - Kismet runs as user "kismetd"; web UI / REST API bound to 127.0.0.1:2501.
# - A read-only API key is created and stored in /etc/flipper-runner/kismet.env
#   (0600, owned by flipper-runner). It is never printed.
# PASSIVE MONITORING ONLY. Only monitor networks you own or are permitted to.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")"
RUNNER=flipper-runner
KUSER=kismetd
KHOME=/var/lib/kismetd
ENVF=/etc/flipper-runner/kismet.env
KEYRING=/usr/share/keyrings/kismet-archive-keyring.gpg

id "$RUNNER" >/dev/null 2>&1 || { echo "run ./install.sh first"; exit 1; }
. /etc/os-release
if [[ "${VERSION_CODENAME:-}" != bookworm ]]; then
  echo "This script targets bookworm; found '${VERSION_CODENAME:-?}'. See https://www.kismetwireless.net/packages/"; exit 1
fi
if [[ "$(dpkg --print-architecture)" != arm64 ]]; then
  echo "Kismet's bookworm repo is arm64/amd64 only: use 64-bit Raspberry Pi OS."; exit 1
fi

echo "== Kismet apt repo (signed-by keyring)"
apt-get install -y curl gnupg ca-certificates >/dev/null
tmpk=$(mktemp)
curl -fsSL https://www.kismetwireless.net/repos/kismet-release.gpg.key -o "$tmpk"
gpg --show-keys --with-fingerprint "$tmpk" | sed -n '1,3p'   # shown so you can compare with kismetwireless.net
gpg --dearmor < "$tmpk" > "$KEYRING.new"
install -o root -g root -m 644 "$KEYRING.new" "$KEYRING"; rm -f "$KEYRING.new" "$tmpk"
echo "deb [signed-by=$KEYRING] https://www.kismetwireless.net/repos/apt/release/bookworm bookworm main" \
  > /etc/apt/sources.list.d/kismet.list
apt-get update

echo "== packages (suid-root capture helpers, group-restricted)"
echo "kismet-capture-common kismet-capture-common/install-setuid boolean true" | debconf-set-selections || true
DEBIAN_FRONTEND=noninteractive apt-get install -y kismet-core kismet-capture-linux-wifi

echo "== user + dirs"
id "$KUSER" >/dev/null 2>&1 || useradd --system --home-dir "$KHOME" --shell /usr/sbin/nologin "$KUSER"
usermod -aG kismet "$KUSER"
install -d -o "$KUSER" -g "$KUSER" -m 700 "$KHOME" "$KHOME/.kismet"
install -d -o "$KUSER" -g "$KUSER" -m 755 /var/log/kismet

echo "== config + unit"
install -o root -g root -m 644 kismet/kismet_site.conf /etc/kismet/kismet_site.conf
install -o root -g root -m 644 kismet/kismet.service /etc/systemd/system/kismet.service
systemctl daemon-reload
systemctl disable kismet.service 2>/dev/null || true   # on demand from the Flipper only

if [[ -s $ENVF ]]; then
  echo "== $ENVF exists; leaving the API key alone"
else
  echo "== admin login (random, stays in $KHOME/.kismet) + read-only API key"
  HC="$KHOME/.kismet/kismet_httpd.conf"
  if [[ ! -s $HC ]]; then
    umask 077
    printf 'httpd_username=admin\nhttpd_password=%s\n' "$(head -c 24 /dev/urandom | base64 | tr -d '/+=')" > "$HC"
    chown "$KUSER:$KUSER" "$HC"; chmod 600 "$HC"
  fi
  systemctl start kismet.service
  for _ in $(seq 1 30); do curl -fs -o /dev/null http://127.0.0.1:2501/ && break; sleep 2; done
  install -o "$RUNNER" -g "$RUNNER" -m 600 /dev/null "$ENVF"
  if HC="$HC" ENVF="$ENVF" python3 - <<'PY'
import base64, json, os, re, urllib.parse, urllib.request
cfg = dict(l.split("=", 1) for l in open(os.environ["HC"]).read().split() if "=" in l)
auth = base64.b64encode(f"{cfg['httpd_username']}:{cfg['httpd_password']}".encode()).decode()
body = urllib.parse.urlencode({"json": json.dumps({"name": "flipper", "role": "readonly", "duration": 0})}).encode()
req = urllib.request.Request("http://127.0.0.1:2501/auth/apikey/generate.cmd", data=body,
                             headers={"Authorization": "Basic " + auth})
key = urllib.request.urlopen(req, timeout=10).read().decode().strip().strip('"')
if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key):
    raise SystemExit("unexpected reply from Kismet")
with open(os.environ["ENVF"], "w") as f:
    f.write("KISMET_API_KEY=" + key + "\n")
PY
  then echo "   read-only API key written to $ENVF (not shown)"
  else
    echo "!! Could not create the API key automatically. In the Kismet web UI (ssh -L 2501:127.0.0.1:2501 pi),"
    echo "   Settings > API Keys > create a 'readonly' key, then:"
    echo "   sudo install -o $RUNNER -g $RUNNER -m 600 /dev/null $ENVF && sudoedit $ENVF   # KISMET_API_KEY=..."
  fi
  systemctl stop kismet.service
fi

echo
echo "Done. Plug in a USB Wi-Fi adapter that supports monitor mode, set its interface in"
echo "/etc/kismet/kismet_site.conf (source=...), then use 'Kismet start' on the Flipper."

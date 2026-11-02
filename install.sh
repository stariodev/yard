#!/bin/sh
# Install Yard on an Ubuntu 26.04 LTS VM; its packaged Podman 5.x is used.
# A second run repairs a partial install.
# A healthy Yard container stays in place. Data under /srv/yard stays in place.
# The source is the public Yard repo. --domain is the public host for Yard.
# Caddy listens on 443 inside its container; the host publishes 8443 for TCP and UDP.
# This script forwards public 443, TCP and UDP, to that port.
set -eu

die() {
  echo "error: $1" >&2
  shift
  while [ $# -gt 0 ]; do
    echo "$1" >&2
    shift
  done
  exit 1
}

origin="https://github.com/stariodev/yard.git"
domain=""
root="/srv/yard"
public_port="8000"
proxy_port="8443"

while [ $# -gt 0 ]; do
  case "$1" in
    --domain)
      [ $# -ge 2 ] || die "--domain needs a value" "Hint: ./install.sh --domain yard.example.com"
      domain="$2"
      shift 2
      ;;
    --remote)
      [ $# -ge 2 ] || die "--remote needs a git url" "Hint: ./install.sh --remote https://github.com/stariodev/yard.git"
      origin="$2"
      shift 2
      ;;
    *)
      die "unknown arg: $1" "Hint: ./install.sh --domain <host>"
      ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Run this script as root." \
  "It creates the yard user, writes /etc files, and loads an nft table." \
  "Hint: sudo ./install.sh --domain <host>"

os_pretty="an unknown OS"
if [ -r /etc/os-release ]; then
  . /etc/os-release
  os_pretty="${PRETTY_NAME:-an unknown OS}"
fi
if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "26.04" ]; then
  echo "warning: Yard targets Ubuntu 26.04 LTS; this host is ${os_pretty}." >&2
fi

if ! command -v nft >/dev/null 2>&1 || ! command -v sudo >/dev/null 2>&1 || ! command -v newuidmap >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1 || ! command -v podman >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  if ! command -v apt-get >/dev/null 2>&1; then
    die "nft, sudo, uidmap, git, podman, and curl are required" \
      "The script installs them with apt-get when that command exists." \
      "Hint: install nftables, sudo, uidmap, git, podman, and curl, then rerun this script."
  fi
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  # uidmap gives rootless Podman newuidmap/newgidmap; acl gives setfacl for the
  # traverse check; podman is the Ubuntu 26.04 package; curl writes the Caddy
  # site for --domain through the local API.
  apt-get install -y nftables git ca-certificates sudo iproute2 procps uidmap acl podman curl
fi
command -v nft >/dev/null 2>&1 || die "nft is not installed" "Hint: apt-get install -y nftables"
command -v podman >/dev/null 2>&1 || die "podman is not installed" "Hint: apt-get install -y podman"
command -v curl >/dev/null 2>&1 || die "curl is not installed" "Hint: apt-get install -y curl"

# Podman 4's compat API drops container labels on the list endpoint, so Compose
# recreates every container on every up. The gate keeps the install on 5.x.
podman_version="$(podman --version 2>/dev/null | sed -n 's/^podman version //p')"
podman_major="${podman_version%%.*}"
case "$podman_major" in
  '' | *[!0-9]*) podman_major=0 ;;
esac
if [ "$podman_major" -lt 5 ]; then
  die "Yard needs Podman 5 or newer." \
    "Context: this host has ${podman_version:-no podman}." \
    "Hint: use Ubuntu 26.04 LTS, whose podman package is 5.x."
fi

if ! id yard >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash yard
fi
add_subids() {
  file="$1"
  flag="$2"
  if [ -f "$file" ] && grep -q "^yard:" "$file"; then
    return 0
  fi
  start=100000
  if [ -f "$file" ]; then
    while IFS=: read -r _name begin count _; do
      if [ -z "$begin" ] || [ -z "$count" ]; then
        continue
      fi
      end=$((begin + count))
      if [ "$end" -gt "$start" ]; then
        start=$end
      fi
    done < "$file"
  fi
  usermod "$flag" "${start}-$((start + 65535))" yard
}

add_subids /etc/subuid --add-subuids
add_subids /etc/subgid --add-subgids

uid="$(id -u yard)"
loginctl enable-linger yard
systemctl start "user@${uid}"

ensure_dir() {
  install -d -o yard -g yard -m 755 "$1"
  chown yard:yard "$1"
  chmod 755 "$1"
}

ensure_dir "$root"
# Yard's container writes apps/<name>/repo as the yard user. A root-owned
# directory here would reject every deploy on a fresh host.
ensure_dir "$root/apps/yard"
ensure_dir "$root/apps/yard/caddy"
ensure_dir "$root/apps/yard/caddy/config"
ensure_dir "$root/apps/yard/caddy/data"
ensure_dir "$root/apps/yard/caddy/run"
ensure_dir "$root/apps/yard/caddy/sites"
install -d -o yard -g yard -m 700 "$root/apps/yard"

install -d /etc/yard
cat > /etc/yard/proxy.nft <<EOF
table inet yard {
  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    tcp dport 443 redirect to :${proxy_port}
    udp dport 443 redirect to :${proxy_port}
  }
}
EOF
nft_bin="$(command -v nft)"
cat > /etc/systemd/system/yard-proxy.service <<EOF
[Unit]
Description=Forward public port 443 (TCP and UDP) to Yard Caddy
After=network-pre.target nftables.service
Before=network.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c '${nft_bin} delete table inet yard 2>/dev/null || true; ${nft_bin} -f /etc/yard/proxy.nft'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable yard-proxy.service
systemctl restart yard-proxy.service
if ! nft list table inet yard 2>/dev/null | grep -q "tcp dport 443 redirect to :${proxy_port}" ||
  ! nft list table inet yard 2>/dev/null | grep -q "udp dport 443 redirect to :${proxy_port}"; then
  die "The redirect from port 443 to ${proxy_port} is not loaded." \
    "Caddy is published on ${proxy_port} for TCP and UDP. Public clients use port 443." \
    "Hint: nft -f /etc/yard/proxy.nft && systemctl status yard-proxy.service" \
    "Context: table inet yard is missing or has a different target."
fi

run_yard() {
  sudo -u yard -- env HOME=/home/yard XDG_RUNTIME_DIR="/run/user/${uid}" "$@"
}

retry() {
  limit="$1"
  shift
  try=0
  while [ "$try" -lt "$limit" ]; do
    if "$@"; then
      return 0
    fi
    try=$((try + 1))
    sleep 1
  done
  return 1
}

allow_redirect_port() {
  if ! command -v ufw >/dev/null 2>&1; then
    return 0
  fi
  status="$(ufw status 2>/dev/null || true)"
  printf '%s\n' "$status" | grep -q "Status: active" || return 0
  for proto in tcp udp; do
    if printf '%s\n' "$status" | grep -q "^${proxy_port}/${proto}"; then
      continue
    fi
    detail="$(ufw allow "${proxy_port}/${proto}" 2>&1)" || die "ufw could not allow port ${proxy_port}/${proto}." \
      "This script redirects public port 443 to ${proxy_port}." \
      "ufw sees the packet after that redirect, so a rule for 443 does not cover ${proxy_port}." \
      "Hint: ufw allow ${proxy_port}/${proto}" \
      "Context: ${detail}"
    ufw status | grep -q "^${proxy_port}/${proto}" || die "ufw still does not allow port ${proxy_port}/${proto}." \
      "The allow command returned, but the rule is not listed." \
      "Hint: ufw status" \
      "Context: public port 443 is redirected to ${proxy_port}."
  done
}

ensure_traverse() {
  dir="$1"
  while [ "$dir" != "/" ]; do
    if ! run_yard test -x "$dir"; then
      if ! command -v setfacl >/dev/null 2>&1; then
        die "User yard cannot enter ${dir}." \
          "Podman mounts ${root} from this path. A parent without execute blocks the mount." \
          "Hint: setfacl -m u:yard:--x ${dir}" \
          "Context: stat from user yard returns permission denied. Install the acl package if setfacl is missing."
      fi
      setfacl -m "u:yard:--x" "$dir" || die "setfacl could not grant user yard execute on ${dir}." \
        "Podman mounts ${root} from this path." \
        "Hint: setfacl -m u:yard:--x ${dir}" \
        "Context: user yard cannot search ${dir}."
      run_yard test -x "$dir" || die "User yard still cannot enter ${dir}." \
        "The ACL was written, but the directory is still closed." \
        "Hint: namei -l ${root}" \
        "Context: user yard is $(id -u yard)."
    fi
    dir=$(dirname "$dir")
  done
}

release_port() {
  holders="$(ss -H -ltnp "sport = :${public_port}" 2>/dev/null || true)"
  if [ -z "$holders" ]; then
    return 0
  fi
  if run_yard podman container exists yard-app-1 >/dev/null 2>&1; then
    return 0
  fi
  die "Port ${public_port} is already in use." \
    "The Yard container publishes 127.0.0.1:${public_port}." \
    "Hint: ss -ltnp sport = :${public_port}" \
    "Context: ${holders}"
}

i=0
while [ ! -d "/run/user/${uid}" ]; do
  i=$((i + 1))
  [ "$i" -le 30 ] || die "The user runtime directory /run/user/${uid} did not appear." \
    "Rootless Podman needs that directory for user yard." \
    "Hint: loginctl enable-linger yard && systemctl start user@${uid}" \
    "Context: waited 30 seconds."
  sleep 1
done

# Container logs go to journald so they survive a container recreate.
# Persistent storage is what splits per-user journals for the yard user.
install -d /etc/systemd/journald.conf.d
journald_conf='[Journal]
Storage=persistent
SystemMaxUse=2G
RateLimitIntervalSec=30s
RateLimitBurst=20000'
if [ "$(cat /etc/systemd/journald.conf.d/yard.conf 2>/dev/null)" != "$journald_conf" ]; then
  printf '%s\n' "$journald_conf" > /etc/systemd/journald.conf.d/yard.conf
  mkdir -p /var/log/journal
  systemd-tmpfiles --create --prefix /var/log/journal
  systemctl restart systemd-journald
fi

# Rootless Podman ships container logs and engine events to journald; the Yard API reads them.
install -d -o yard -g yard /home/yard/.config /home/yard/.config/containers
containers_conf='[containers]
log_driver = "journald"

[engine]
events_logger = "journald"'
if [ "$(cat /home/yard/.config/containers/containers.conf 2>/dev/null)" != "$containers_conf" ]; then
  printf '%s\n' "$containers_conf" > /home/yard/.config/containers/containers.conf
  chown yard:yard /home/yard/.config/containers/containers.conf
  chmod 644 /home/yard/.config/containers/containers.conf
  run_yard systemctl --user stop podman.service >/dev/null 2>&1 || true
  run_yard systemctl --user restart podman.socket
fi

run_yard systemctl --user enable --now podman.socket
run_yard systemctl --user enable podman-restart.service || die "Could not enable podman-restart.service." \
  "Containers marked for restart would not come back after a reboot." \
  "Hint: systemctl --user --machine yard@ status podman-restart.service"
sock="/run/user/${uid}/podman/podman.sock"
if ! retry 30 sh -c "test -S '$sock'"; then
  die "The Podman socket ${sock} is not ready." \
    "Yard talks to Podman through that socket." \
    "Hint: systemctl --user --machine yard@ status podman.socket" \
    "Context: waited 30 seconds for the socket file."
fi
if ! retry 30 run_yard podman info >/dev/null 2>&1; then
  info="$(run_yard podman info 2>&1 || true)"
  die "Podman is not ready for user yard." \
    "Rootless Podman must answer before containers can start." \
    "Hint: sudo -u yard podman info" \
    "Context: ${info}"
fi
allow_redirect_port
ensure_traverse "$root"

if ! run_yard podman network exists yard >/dev/null 2>&1; then
  run_yard podman network create yard >/dev/null || die "Podman could not create the yard network." \
    "Caddy and Yard join this bridge so they can reach each other and the internet." \
    "Hint: sudo -u yard podman network ls" \
    "Context: network create failed for user yard."
fi

release_port

# The Yard clone and the image build stay in shell; Python does the first deploy.
# An existing clone is left as it is; the running Yard owns it from then on.
src="$root/apps/yard/repo"
if [ ! -d "$src/.git" ]; then
  run_yard git clone --depth 1 "$origin" "$src" || die "Could not clone ${origin}." \
    "The host clones Yard once. After the first deploy, pushes keep it moving." \
    "Hint: git ls-remote ${origin} HEAD" \
    "Context: clone failed for user yard."
fi
sha="$(run_yard git -C "$src" rev-parse HEAD)"
short="$(printf '%s' "$sha" | cut -c1-12)"

ensure_dir "$root/apps/yard/data"
ensure_dir "$root/tokens"

run_yard podman build -t localhost/yard-apply -f "$src/ops/apply/Containerfile" "$src/ops/apply" || die "Could not build the apply image." \
  "That image runs Compose. The build uses ${src}/ops/apply." \
  "Hint: sudo -u yard podman build -f ${src}/ops/apply/Containerfile ${src}/ops/apply" \
  "Context: the build failed for user yard."

# One apply container builds and starts Yard, the same way Yard deploys apps.
# The -e set matches what compose.apply passes for the yard app.
run_yard podman run --replace --name deploy-yard --network yard \
  -v "$sock:/run/podman/podman.sock" -v "$root:$root" \
  -e COMPOSE_PROJECT_NAME=yard -e COMPOSE_FILE="$src/ops/compose.yml" \
  -e APP_ENV="$root/apps/yard/env" -e CHECKOUT="$src" -e DATA_DIR="$root/apps/yard/data" \
  -e IMAGE="localhost/yard:$short" -e SHA="$sha" -e PROJECT_NAME=yard \
  -e YARD_ROOT="$root" -e YARD_PODMAN_SOCK="$sock" -e YARD_PODMAN_DIR="/run/user/${uid}/podman" \
  localhost/yard-apply up || die "The first deploy failed." "Hint: podman logs deploy-yard"

i=0
while [ ! -s "$root/tokens/admin.token" ] && [ "$i" -lt 60 ]; do
  i=$((i + 1))
  sleep 1
done
[ -s "$root/tokens/admin.token" ] || die "Yard did not write its admin token." \
  "Hint: podman logs yard-app-1" \
  "Context: the file was missing or empty after 60 seconds."
token="$(cat "$root/tokens/admin.token")"
echo "token: $token"
if [ -n "$domain" ]; then
  # --domain publishes Yard's own host: Yard's site file, written through the local API.
  # Caddy's admin socket can lag the token file, so the PUT retries.
  site="$(printf '%s {\n\treverse_proxy yard-app-1:8000 {\n\t\tlb_try_duration 10s\n\t\tlb_try_interval 250ms\n\t}\n}\n' "$domain")"
  site_url="http://127.0.0.1:${public_port}/api/v1/apps/yard/caddy"
  put_site() {
    printf '%s\n' "$site" | curl -fsS -X PUT -H "Authorization: Bearer $token" \
      -H 'Content-Type: text/caddyfile' --data-binary @- "$site_url"
  }
  if ! retry 30 put_site >/dev/null 2>&1; then
    out="$(put_site 2>&1 || true)"
    die "Could not write the Caddy site for ${domain}." \
      "Yard answers locally, but Caddy did not load the site file." \
      "Hint: yard caddy edit --app yard" \
      "Context: PUT ${site_url} answered: ${out:-no output}"
  fi
  echo "caddy: https://$domain -> yard-app-1:8000"
  # The site is loaded; the certificate and DNS can still lag. Warn, not fail.
  if ! retry 10 sh -c "curl -fsS -o /dev/null --max-time 5 'https://${domain}/health'"; then
    echo "warning: https://${domain}/health did not answer yet." >&2
    echo "  The site file is loaded. Caddy still needs DNS and a certificate." >&2
    echo "  Check: curl -I https://${domain}/health" >&2
  fi
  echo "url: https://$domain"
else
  echo "url: pass --domain to publish this host"
fi

# The yard app deploys Yard itself, but a fresh record has an empty repo and
# `yard app deploy --app yard` fails until one is set. Point it at the install
# origin, trigger manual. Best effort: the warning carries the fix command.
app_url="http://127.0.0.1:${public_port}/api/v1/apps/yard"
app_page="$(curl -fsS -H "Authorization: Bearer $token" "$app_url" 2>/dev/null || true)"
# The first "repo" key is the app record's. Only an explicit empty value is
# patched, so a re-install never overwrites a repo the user chose.
repo_field="$(printf '%s' "$app_page" | grep -o '"repo": *"[^"]*"' | head -n 1 | tr -d ' ')"
if [ -z "$app_page" ] || [ -z "$repo_field" ]; then
  echo "warning: could not read the yard app record; its repo may be unset." >&2
  echo "  Set it: yard app set --app yard --repo ${origin%.git}" >&2
elif [ "$repo_field" = '"repo":""' ]; then
  if curl -fsS -X PATCH -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -d "{\"repo\":\"${origin%.git}\"}" "$app_url" >/dev/null 2>&1; then
    echo "repo: yard follows ${origin%.git} (manual; yard app deploy --app yard upgrades)"
  else
    echo "warning: could not set the yard repo." >&2
    echo "  Set it: yard app set --app yard --repo ${origin%.git}" >&2
  fi
fi
echo "local: http://127.0.0.1:8000"

# Setup runs on the laptop, through the yard CLI.
echo ""
echo "Next steps:"
echo "  yard login --url https://${domain:-<domain>} --token <token>"
echo "  yard forge setup origin    # or: yard forge setup github"
echo "  yard app set --app yard --trigger push   # optional: upgrade Yard on every push to ${origin%.git}"

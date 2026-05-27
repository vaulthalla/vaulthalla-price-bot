#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="vaulthalla-price-bot"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

YES=0
INSTALL_SERVICE=0
INSTALL_NGINX=0
INSTALL_CERTBOT=0
COMPONENT_FLAG_SET=0
PORT=""
DOMAIN=""

usage() {
    cat <<'USAGE'
Usage: deploy/install.sh [options]

Install vaulthalla-price-bot deploy components.

Options:
  -y, --yes          Install selected components without component prompts.
                     With no component flags, installs service, nginx, and certbot.
                     Still prompts for missing --port and --domain.
      --service      Install the systemd service.
      --nginx        Install the nginx site.
      --certbot      Run certbot for the nginx site.
      --port PORT    Local app port for gunicorn/nginx.
      --domain NAME  Public domain name for nginx/certbot.
  -h, --help         Show this help.

Examples:
  make install ARGS="-y --domain prices.example.com --port 8080"
  make install ARGS="--service --port 8080"
  make install ARGS="--nginx --certbot --domain prices.example.com --port 8080"
USAGE
}

log() {
    printf '[install] %s\n' "$*"
}

die() {
    printf '[install] ERROR: %s\n' "$*" >&2
    exit 1
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

as_root() {
    if [[ "${EUID}" -eq 0 ]]; then
        "$@"
        return
    fi
    command_exists sudo || die "sudo is required for system installation steps"
    sudo "$@"
}

install_user() {
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        printf '%s\n' "${SUDO_USER}"
    else
        id -un
    fi
}

install_group() {
    local user="$1"
    id -gn "${user}" 2>/dev/null || id -gn
}

run_as_install_user() {
    local user="$1"
    shift
    if [[ "${EUID}" -eq 0 && "${user}" != "root" ]]; then
        if command_exists sudo; then
            sudo -H -u "${user}" "$@"
        elif command_exists runuser; then
            runuser -u "${user}" -- "$@"
        else
            die "sudo or runuser is required to run Python setup as ${user}"
        fi
        return
    fi
    "$@"
}

prompt_yes_no() {
    local prompt="$1"
    local default="$2"
    local answer=""
    local suffix="[y/n]"

    if [[ "${default}" == "y" ]]; then
        suffix="[Y/n]"
    elif [[ "${default}" == "n" ]]; then
        suffix="[y/N]"
    fi

    while true; do
        read -r -p "${prompt} ${suffix} " answer
        answer="${answer:-${default}}"
        case "${answer,,}" in
            y|yes) return 0 ;;
            n|no) return 1 ;;
            *) printf 'Please answer yes or no.\n' ;;
        esac
    done
}

validate_port() {
    local port="$1"
    [[ "${port}" =~ ^[0-9]+$ ]] && (( 10#${port} >= 1 && 10#${port} <= 65535 ))
}

validate_domain() {
    [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]]
}

prompt_port() {
    local value=""
    while true; do
        read -r -p "Local app port [8080]: " value
        value="${value:-8080}"
        if validate_port "${value}"; then
            PORT="${value}"
            return
        fi
        printf 'Port must be an integer from 1 to 65535.\n'
    done
}

prompt_domain() {
    local value=""
    while true; do
        read -r -p "Domain name: " value
        value="${value,,}"
        if validate_domain "${value}"; then
            DOMAIN="${value}"
            return
        fi
        printf 'Domain must be a fully qualified DNS name, for example prices.example.com.\n'
    done
}

sed_escape() {
    printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

install_missing_apt_packages() {
    local -a requested=("$@")
    local -a missing=()
    local package=""

    (( ${#requested[@]} == 0 )) && return
    command_exists apt-get || die "apt-get is required by this installer"

    for package in "${requested[@]}"; do
        if command_exists dpkg && dpkg -s "${package}" >/dev/null 2>&1; then
            continue
        fi
        missing+=("${package}")
    done

    (( ${#missing[@]} == 0 )) && return
    log "Installing OS packages with apt: ${missing[*]}"
    as_root apt-get update
    as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}

dedupe_packages() {
    local -n input_ref=$1
    local -a unique=()
    local package=""
    local seen=""

    for package in "${input_ref[@]}"; do
        if [[ " ${seen} " == *" ${package} "* ]]; then
            continue
        fi
        unique+=("${package}")
        seen+=" ${package}"
    done

    input_ref=("${unique[@]}")
}

nginx_available_path() {
    if [[ -d /etc/nginx/sites-available ]]; then
        printf '/etc/nginx/sites-available/%s.conf\n' "${SERVICE_NAME}"
    else
        printf '/etc/nginx/conf.d/%s.conf\n' "${SERVICE_NAME}"
    fi
}

nginx_enabled_path() {
    if [[ -d /etc/nginx/sites-enabled ]]; then
        printf '/etc/nginx/sites-enabled/%s.conf\n' "${SERVICE_NAME}"
    fi
}

setup_venv() {
    local user="$1"
    local venv="${REPO_ROOT}/.venv"

    log "Creating or updating Python virtual environment at ${venv}"
    run_as_install_user "${user}" python3 -m venv "${venv}"
    run_as_install_user "${user}" "${venv}/bin/python" -m pip install --upgrade pip
    run_as_install_user "${user}" "${venv}/bin/python" -m pip install -e "${REPO_ROOT}"
}

install_service() {
    local user="$1"
    local group="$2"
    local template="${SCRIPT_DIR}/systemd/${SERVICE_NAME}.service.template"
    local tmp=""
    local target="/etc/systemd/system/${SERVICE_NAME}.service"
    local venv="${REPO_ROOT}/.venv"

    [[ -f "${template}" ]] || die "missing template: ${template}"
    command_exists systemctl || die "systemctl is required to install the service"
    [[ -x "${venv}/bin/gunicorn" ]] || die "gunicorn was not installed at ${venv}/bin/gunicorn"

    tmp="$(mktemp)"
    sed \
        -e "s|__USER__|$(sed_escape "${user}")|g" \
        -e "s|__GROUP__|$(sed_escape "${group}")|g" \
        -e "s|__WORKING_DIRECTORY__|$(sed_escape "${REPO_ROOT}")|g" \
        -e "s|__ENV_FILE__|$(sed_escape "${REPO_ROOT}/.env")|g" \
        -e "s|__GUNICORN__|$(sed_escape "${venv}/bin/gunicorn")|g" \
        -e "s|__PORT__|$(sed_escape "${PORT}")|g" \
        "${template}" > "${tmp}"

    log "Installing systemd service ${target}"
    as_root install -m 0644 "${tmp}" "${target}"
    rm -f "${tmp}"
    as_root systemctl daemon-reload
    as_root systemctl enable "${SERVICE_NAME}.service"
    as_root systemctl restart "${SERVICE_NAME}.service"
}

install_nginx_site() {
    local template="${SCRIPT_DIR}/nginx/${SERVICE_NAME}.conf.template"
    local tmp=""
    local available=""
    local enabled=""

    [[ -f "${template}" ]] || die "missing template: ${template}"
    command_exists nginx || die "nginx is required to install the site"
    command_exists systemctl || die "systemctl is required to manage nginx"

    available="$(nginx_available_path)"
    enabled="$(nginx_enabled_path)"
    tmp="$(mktemp)"

    sed \
        -e "s|__DOMAIN__|$(sed_escape "${DOMAIN}")|g" \
        -e "s|__PORT__|$(sed_escape "${PORT}")|g" \
        "${template}" > "${tmp}"

    log "Installing nginx site ${available}"
    as_root install -d "$(dirname -- "${available}")"
    as_root install -m 0644 "${tmp}" "${available}"
    rm -f "${tmp}"

    if [[ -n "${enabled}" ]]; then
        as_root install -d "$(dirname -- "${enabled}")"
        as_root ln -sfn "${available}" "${enabled}"
    fi

    as_root nginx -t
    as_root systemctl enable --now nginx
    as_root systemctl reload nginx || as_root systemctl restart nginx
}

run_certbot() {
    local -a args=(--nginx -d "${DOMAIN}" --keep-until-expiring --redirect)

    command_exists nginx || die "certbot --nginx requires nginx; install nginx first or pass --nginx"
    command_exists certbot || die "certbot is required"

    as_root nginx -t

    if as_root certbot certificates --cert-name "${DOMAIN}" >/dev/null 2>&1; then
        log "Existing certificate found for ${DOMAIN}; attempting renewal first"
        if ! as_root certbot renew --cert-name "${DOMAIN}"; then
            log "Renewal did not complete; continuing with certbot nginx install"
        fi
    fi

    if [[ "${YES}" -eq 1 ]]; then
        args+=(--non-interactive --agree-tos --register-unsafely-without-email)
    fi

    log "Running certbot for ${DOMAIN}"
    as_root certbot "${args[@]}"
    as_root systemctl reload nginx || true
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes)
            YES=1
            shift
            ;;
        --service)
            INSTALL_SERVICE=1
            COMPONENT_FLAG_SET=1
            shift
            ;;
        --nginx)
            INSTALL_NGINX=1
            COMPONENT_FLAG_SET=1
            shift
            ;;
        --certbot)
            INSTALL_CERTBOT=1
            COMPONENT_FLAG_SET=1
            shift
            ;;
        --port)
            [[ $# -ge 2 ]] || die "--port requires a value"
            PORT="$2"
            shift 2
            ;;
        --domain)
            [[ $# -ge 2 ]] || die "--domain requires a value"
            DOMAIN="${2,,}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

cd "${REPO_ROOT}"
[[ -f "${REPO_ROOT}/pyproject.toml" && -f "${REPO_ROOT}/app.py" ]] || die "run from the vaulthalla-price-bot checkout"

if [[ "${COMPONENT_FLAG_SET}" -eq 0 ]]; then
    if [[ "${YES}" -eq 1 ]]; then
        INSTALL_SERVICE=1
        INSTALL_NGINX=1
        INSTALL_CERTBOT=1
    else
        prompt_yes_no "Install systemd service?" "y" && INSTALL_SERVICE=1
        prompt_yes_no "Install nginx site?" "y" && INSTALL_NGINX=1
        prompt_yes_no "Run certbot for the nginx site?" "y" && INSTALL_CERTBOT=1
    fi
fi

if [[ "${INSTALL_SERVICE}" -eq 0 && "${INSTALL_NGINX}" -eq 0 && "${INSTALL_CERTBOT}" -eq 0 ]]; then
    log "No components selected; nothing to install"
    exit 0
fi

if [[ "${INSTALL_SERVICE}" -eq 1 || "${INSTALL_NGINX}" -eq 1 ]]; then
    if [[ -z "${PORT}" ]]; then
        prompt_port
    elif ! validate_port "${PORT}"; then
        die "--port must be an integer from 1 to 65535"
    fi
fi

if [[ "${INSTALL_NGINX}" -eq 1 || "${INSTALL_CERTBOT}" -eq 1 ]]; then
    if [[ -z "${DOMAIN}" ]]; then
        prompt_domain
    elif ! validate_domain "${DOMAIN}"; then
        die "--domain must be a fully qualified DNS name, for example prices.example.com"
    fi
fi

INSTALL_USER="$(install_user)"
INSTALL_GROUP="$(install_group "${INSTALL_USER}")"
declare -a PACKAGES=()

if [[ "${INSTALL_SERVICE}" -eq 1 ]]; then
    PACKAGES+=(python3 python3-venv python3-pip)
fi
if [[ "${INSTALL_NGINX}" -eq 1 ]]; then
    PACKAGES+=(nginx)
fi
if [[ "${INSTALL_CERTBOT}" -eq 1 ]]; then
    PACKAGES+=(certbot python3-certbot-nginx)
fi

dedupe_packages PACKAGES
install_missing_apt_packages "${PACKAGES[@]}"

if [[ "${INSTALL_SERVICE}" -eq 1 ]]; then
    setup_venv "${INSTALL_USER}"
    install_service "${INSTALL_USER}" "${INSTALL_GROUP}"
fi

if [[ "${INSTALL_NGINX}" -eq 1 ]]; then
    install_nginx_site
fi

if [[ "${INSTALL_CERTBOT}" -eq 1 ]]; then
    if [[ "${INSTALL_NGINX}" -eq 0 ]]; then
        log "Using the existing nginx site for ${DOMAIN}"
    fi
    run_certbot
fi

log "Install complete"

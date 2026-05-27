#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="vaulthalla-price-bot"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

YES=0
REMOVE_SERVICE=0
REMOVE_NGINX=0
REMOVE_CERTBOT=0
COMPONENT_FLAG_SET=0
DOMAIN=""

usage() {
    cat <<'USAGE'
Usage: deploy/uninstall.sh [options]

Remove vaulthalla-price-bot deploy components.

Options:
  -y, --yes          Remove selected components without prompts.
                     With no component flags, removes service, nginx site, and certbot cert.
                     Cert deletion requires --domain unless it can be inferred from nginx.
      --service      Remove the systemd service.
      --nginx        Remove the nginx site.
      --certbot      Delete the certbot certificate for --domain.
      --domain NAME  Certificate domain name.
  -h, --help         Show this help.

Examples:
  make uninstall ARGS="-y --domain prices.example.com"
  make uninstall ARGS="--service"
  make uninstall ARGS="--nginx --certbot --domain prices.example.com"
USAGE
}

log() {
    printf '[uninstall] %s\n' "$*"
}

die() {
    printf '[uninstall] ERROR: %s\n' "$*" >&2
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
    command_exists sudo || die "sudo is required for system uninstall steps"
    sudo "$@"
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

validate_domain() {
    [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]]
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

infer_domain_from_nginx() {
    local path=""
    local inferred=""

    for path in "$(nginx_available_path)" "$(nginx_enabled_path)" "/etc/nginx/conf.d/${SERVICE_NAME}.conf"; do
        [[ -n "${path}" && -f "${path}" ]] || continue
        inferred="$(awk '
            $1 == "server_name" {
                for (i = 2; i <= NF; i++) {
                    gsub(";", "", $i)
                    if ($i != "" && $i != "_") {
                        print $i
                        exit
                    }
                }
            }
        ' "${path}")"
        if [[ -n "${inferred}" ]]; then
            printf '%s\n' "${inferred}"
            return
        fi
    done
}

remove_service() {
    local unit="/etc/systemd/system/${SERVICE_NAME}.service"

    command_exists systemctl || die "systemctl is required to remove the service"
    log "Removing systemd service ${unit}"
    as_root systemctl stop "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
    as_root systemctl disable "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
    as_root rm -f "${unit}"
    as_root systemctl daemon-reload
}

remove_nginx_site() {
    local available=""
    local enabled=""
    local removed=0

    available="$(nginx_available_path)"
    enabled="$(nginx_enabled_path)"

    if [[ -n "${enabled}" && -e "${enabled}" ]]; then
        log "Removing nginx enabled site ${enabled}"
        as_root rm -f "${enabled}"
        removed=1
    fi

    if [[ -e "${available}" ]]; then
        log "Removing nginx site ${available}"
        as_root rm -f "${available}"
        removed=1
    fi

    if [[ -e "/etc/nginx/conf.d/${SERVICE_NAME}.conf" && "${available}" != "/etc/nginx/conf.d/${SERVICE_NAME}.conf" ]]; then
        log "Removing nginx site /etc/nginx/conf.d/${SERVICE_NAME}.conf"
        as_root rm -f "/etc/nginx/conf.d/${SERVICE_NAME}.conf"
        removed=1
    fi

    if [[ "${removed}" -eq 0 ]]; then
        log "No nginx site found for ${SERVICE_NAME}"
        return
    fi

    if command_exists nginx && command_exists systemctl; then
        as_root nginx -t
        as_root systemctl reload nginx || true
    fi
}

delete_certbot_certificate() {
    command_exists certbot || die "certbot is required to delete certificates"

    if ! validate_domain "${DOMAIN}"; then
        die "invalid certificate domain: ${DOMAIN}"
    fi

    log "Deleting certbot certificate ${DOMAIN}"
    as_root certbot delete --cert-name "${DOMAIN}" --non-interactive
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -y|--yes)
            YES=1
            shift
            ;;
        --service)
            REMOVE_SERVICE=1
            COMPONENT_FLAG_SET=1
            shift
            ;;
        --nginx)
            REMOVE_NGINX=1
            COMPONENT_FLAG_SET=1
            shift
            ;;
        --certbot)
            REMOVE_CERTBOT=1
            COMPONENT_FLAG_SET=1
            shift
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

if [[ "${COMPONENT_FLAG_SET}" -eq 0 ]]; then
    if [[ "${YES}" -eq 1 ]]; then
        REMOVE_SERVICE=1
        REMOVE_NGINX=1
        REMOVE_CERTBOT=1
    else
        prompt_yes_no "Remove systemd service?" "y" && REMOVE_SERVICE=1
        prompt_yes_no "Remove nginx site?" "y" && REMOVE_NGINX=1
        prompt_yes_no "Delete certbot certificate?" "n" && REMOVE_CERTBOT=1
    fi
fi

if [[ "${REMOVE_SERVICE}" -eq 0 && "${REMOVE_NGINX}" -eq 0 && "${REMOVE_CERTBOT}" -eq 0 ]]; then
    log "No components selected; nothing to remove"
    exit 0
fi

if [[ "${REMOVE_CERTBOT}" -eq 1 && -z "${DOMAIN}" ]]; then
    DOMAIN="$(infer_domain_from_nginx || true)"
    if [[ -z "${DOMAIN}" ]]; then
        if [[ "${YES}" -eq 1 ]]; then
            die "unable to infer certificate domain; pass --domain"
        fi
        prompt_domain
    else
        log "Inferred certificate domain ${DOMAIN}"
    fi
fi

if [[ "${REMOVE_CERTBOT}" -eq 1 && "${YES}" -eq 0 && "${COMPONENT_FLAG_SET}" -eq 1 ]]; then
    if ! prompt_yes_no "Delete certbot certificate for ${DOMAIN}?" "n"; then
        REMOVE_CERTBOT=0
    fi
fi

if [[ "${REMOVE_SERVICE}" -eq 1 ]]; then
    remove_service
fi

if [[ "${REMOVE_CERTBOT}" -eq 1 ]]; then
    delete_certbot_certificate
fi

if [[ "${REMOVE_NGINX}" -eq 1 ]]; then
    remove_nginx_site
fi

log "Uninstall complete"

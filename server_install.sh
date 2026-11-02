#!/usr/bin/env bash
# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

set +x
set -Eeuo pipefail
umask 077
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=l
REPOSITORY='https://github.com/FacuSecX/StoryPulse-Private.git'
VERACRYPT_VERSION='1.26.30'
MEGACMD_VERSION='2.7.0-1.1'
SYSTEM_TIMEZONE='America/Argentina/Buenos_Aires'
CONFIG_INPUT=''
SKIP_START=0
SOURCE_DIR=''
STAGE=''
BACKUPS_RESULT='No solicitados'
FILEBROWSER_RESULT='No solicitado'
LOG_FILE=''

usage() {
    cat <<'HELP'
Uso: sudo bash instalar-debian13.sh [--config /ruta/privada.json] [--skip-start]

El modo normal pregunta servicio, usuario Linux, token/Chat ID de Telegram y MEGA
opcional y FileBrowser Quantum opcional. No se necesita una sesión Instagram para
arrancar; se agrega después.
La configuración --config debe ser de root, modo 600. Campos: service, user,
telegram_token, telegram_chat_id; opcionales timezone, panel_url, backups (bool),
mega_email, mega_password, filebrowser (bool). Nunca pases secretos como argumentos.
--skip-start instala y valida sin iniciar el bot ni contactar Telegram o MEGA.
--source /ruta permite instalar una copia local para pruebas antes de publicarla.
Sin --source, se clona exclusivamente el repositorio oficial.
La zona horaria del servidor y del bot se configura para Argentina.
HELP
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
safe_path() {
    local path=$1 component current=''
    local -a components
    [[ "$path" == /* ]] || return 1
    IFS='/' read -r -a components <<< "$path"
    for component in "${components[@]}"; do
        [[ -n "$component" ]] || continue
        [[ "$component" != . && "$component" != .. ]] || return 1
        current+="/$component"
        [[ ! -L "$current" ]] || return 1
    done
}
root_directory() {
    local path=$1 mode
    safe_path "$path" || return 1
    if [[ -e "$path" ]]; then
        [[ -d "$path" && $(stat -c %u "$path") == 0 ]] || return 1
        mode=$(stat -c %a "$path")
        (( (8#$mode & 8#022) == 0 )) || return 1
    fi
}
run_step() {
    local title=$1
    shift
    printf '%s… ' "$title"
    if "$@" >>"$LOG_FILE" 2>&1; then
        printf 'completado\n'
    else
        printf 'falló\n' >&2
        printf 'Registro privado: %s\n' "$LOG_FILE" >&2
        return 1
    fi
}
cleanup() { [[ -z "$STAGE" || ! -d "$STAGE" ]] || rm -rf -- "$STAGE"; }
trap cleanup EXIT
trap 'printf "La instalación se detuvo. Los datos existentes se conservan. Registro privado: %s\n" "${LOG_FILE:-no creado}" >&2' ERR
while (($#)); do
    case "$1" in
        --config) [[ $# -ge 2 ]] || die 'Falta la ruta de configuración'; CONFIG_INPUT=$2; shift 2 ;;
        --skip-start) SKIP_START=1; shift ;;
        --source) [[ $# -ge 2 ]] || die 'Falta el proyecto local'; SOURCE_DIR=$2; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die 'Opción desconocida. Consultá --help.' ;;
    esac
done
[[ $EUID -eq 0 ]] || die 'Ejecutá el instalador con sudo o como root.'
[[ -f /etc/os-release ]] || die 'No se pudo identificar el sistema.'
. /etc/os-release
[[ "$ID" == debian && "$VERSION_ID" == 13 ]] || die 'Este instalador requiere Debian 13; no cambia la distribución.'
[[ -d /run/systemd/system ]] || die 'Se requiere un servidor con systemd activo.'
exec 9>/run/lock/storypulse-installer.lock
flock -n 9 || die 'Ya hay otra instalación en curso.'
ARCH=$(dpkg --print-architecture)
[[ "$ARCH" == amd64 ]] || die 'Esta versión comprobada del instalador requiere Debian 13 amd64.'
STAGE=$(mktemp -d /var/tmp/storypulse-install.XXXXXXXX)
chmod 700 "$STAGE"
safe_path /var/log/storypulse-installer || die 'Directorio de registro inseguro.'
if [[ -e /var/log/storypulse-installer ]]; then
    [[ -d /var/log/storypulse-installer && $(stat -c %u /var/log/storypulse-installer) == 0 ]] || die 'Directorio de registro ajeno.'
fi
install -d -m 700 -o root -g root /var/log/storypulse-installer
LOG_FILE="/var/log/storypulse-installer/instalacion-$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
install -m 600 /dev/null "$LOG_FILE"
printf 'Instalador StoryPulse Private · Debian 13\n'
run_step 'Actualizando lista de paquetes' apt-get update
run_step 'Instalando dependencias del sistema' apt-get install -y --no-remove --no-install-recommends ca-certificates curl git python3 python3-venv \
    python3-pip sudo acl tzdata util-linux gnupg openssl iproute2
run_step 'Configurando hora de Argentina' timedatectl set-timezone "$SYSTEM_TIMEZONE"
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
SUPPORT_DIR=$SCRIPT_DIR
[[ -f "$SUPPORT_DIR/configure.py" ]] || SUPPORT_DIR="$SCRIPT_DIR/installer"
if [[ ! -f "$SUPPORT_DIR/configure.py" ]]; then
    SUPPORT_DIR="$STAGE/support"
    install -d -m 700 "$SUPPORT_DIR"
    for helper in configure.py; do
        if ! run_step 'Preparando asistente de instalación' curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 20 --max-time 90 \
            --output "$SUPPORT_DIR/$helper" "https://raw.githubusercontent.com/FacuSecX/StoryPulse-Private/main/installer/$helper"; then
            die 'Publicá primero esta versión completa en el repositorio oficial, incluido installer/.'
        fi
    done
fi
CONFIG="$STAGE/config.json"
if [[ -n "$CONFIG_INPUT" ]]; then
    python3 -B "$SUPPORT_DIR/configure.py" prepare "$CONFIG" --input "$CONFIG_INPUT"
else
    python3 -B "$SUPPORT_DIR/configure.py" prepare "$CONFIG"
fi
mapfile -t META < <(python3 -B "$SUPPORT_DIR/configure.py" metadata "$CONFIG")
[[ ${#META[@]} -eq 6 ]] || die 'No se pudo validar la configuración.'
SERVICE_NAME=${META[0]}
RUN_USER=${META[1]}
RUN_GROUP=$(id -gn "$RUN_USER")
BOT_GID=${META[2]}
RUN_HOME=${META[3]}
WANT_BACKUPS=${META[4]}
WANT_FILEBROWSER=${META[5]}
PROJECT_DIR="$RUN_HOME/StoryPulse-Private"
VENV_DIR="$PROJECT_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
BROWSERS_DIR="$PROJECT_DIR/.pw-browsers"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"
for target in "$RUN_HOME" "$PROJECT_DIR" "$SERVICE_FILE" /historys; do
    safe_path "$target" || die 'Una ruta de la instalación contiene un enlace inseguro.'
done
[[ $(stat -c %u "$RUN_HOME") == "$(id -u "$RUN_USER")" ]] || die 'El directorio personal no pertenece al usuario.'
if [[ -e "$SERVICE_FILE" ]] && ! grep -Fq '# Managed by StoryPulse installer' "$SERVICE_FILE"; then
    die 'Ese nombre pertenece a otro servicio; elegí uno diferente.'
fi
if [[ -L "$PROJECT_DIR" ]]; then die 'El directorio del proyecto no puede ser un enlace.'; fi
if [[ -n "$SOURCE_DIR" ]]; then
    SOURCE_DIR=$(realpath -e -- "$SOURCE_DIR")
    [[ -f "$SOURCE_DIR/bot.py" && -f "$SOURCE_DIR/requirements.txt" ]] || die 'La copia local no contiene el proyecto.'
    if [[ "$SOURCE_DIR" != "$PROJECT_DIR" ]]; then
        [[ ! -e "$PROJECT_DIR" ]] || die 'Para las pruebas locales el destino debe ser nuevo.'
        install -d -m 700 -o "$RUN_USER" -g "$RUN_GROUP" "$PROJECT_DIR"
        cp -a -- "$SOURCE_DIR/." "$PROJECT_DIR/"
        chown -R "$RUN_USER:$RUN_GROUP" "$PROJECT_DIR"
    fi
elif [[ ! -e "$PROJECT_DIR" ]]; then
    run_step 'Descargando proyecto oficial' runuser -u "$RUN_USER" -- git clone --depth 1 "$REPOSITORY" "$PROJECT_DIR"
else
    [[ -d "$PROJECT_DIR/.git" ]] || die 'El destino existe y no es un clon; no se sobrescribió.'
    ORIGIN=$(runuser -u "$RUN_USER" -- git -C "$PROJECT_DIR" remote get-url origin)
    [[ "$ORIGIN" == "$REPOSITORY" || "$ORIGIN" == "${REPOSITORY%.git}" ]] || die 'El destino pertenece a otro repositorio.'
    printf 'Se conserva el clon existente y todos sus datos. No se ejecuta reset/pull automático.\n'
fi
[[ -f "$PROJECT_DIR/bot.py" && -f "$PROJECT_DIR/requirements.txt" && -f "$PROJECT_DIR/navigation_ui.py" ]] \
    || die 'El repositorio todavía no contiene esta versión. Publicá primero la copia limpia.'
for target in "$VENV_DIR" "$VENV_DIR/bin" "$BROWSERS_DIR" "$PROJECT_DIR/.env" \
              "$PROJECT_DIR/sesiones_instagram" "$PROJECT_DIR/perfiles_instagram"; do
    safe_path "$target" || die 'Una carpeta de ejecución contiene un enlace inseguro.'
done
if [[ -e "$PYTHON_BIN" || -L "$PYTHON_BIN" ]]; then
    [[ $(readlink -e -- "$PYTHON_BIN") == "$(readlink -e /usr/bin/python3)" ]] \
        || die 'El entorno Python existente usa un intérprete inesperado; se conservó.'
fi
chmod 700 "$PROJECT_DIR"
install -d -m 700 -o "$RUN_USER" -g "$RUN_GROUP" "$PROJECT_DIR/sesiones_instagram" "$PROJECT_DIR/perfiles_instagram"
if [[ -L /historys ]]; then die '/historys no puede ser un enlace.'; fi
if [[ ! -d /historys ]]; then install -d -m 2770 -o "$RUN_USER" -g "$RUN_GROUP" /historys; fi
setfacl -m "u:$RUN_USER:rwx" /historys
setfacl -d -m "u:$RUN_USER:rwx" /historys
if [[ ! -x "$PYTHON_BIN" ]]; then run_step 'Creando entorno Python' runuser -u "$RUN_USER" -- python3 -m venv "$VENV_DIR"; fi
run_step 'Instalando módulos Python' runuser -u "$RUN_USER" -- "$PYTHON_BIN" -m pip install --disable-pip-version-check --requirement "$PROJECT_DIR/requirements.txt"
run_step 'Preparando dependencias del navegador' "$PYTHON_BIN" -m playwright install-deps chromium
install -d -m 700 -o "$RUN_USER" -g "$RUN_GROUP" "$BROWSERS_DIR"
run_step 'Instalando navegador Chromium' runuser -u "$RUN_USER" -- env PLAYWRIGHT_BROWSERS_PATH="$BROWSERS_DIR" "$PYTHON_BIN" -m playwright install chromium
run_step 'Guardando configuración privada del bot' python3 -B "$SUPPORT_DIR/configure.py" env "$CONFIG" --project "$PROJECT_DIR"
run_step 'Verificando entorno Python' runuser -u "$RUN_USER" -- "$PYTHON_BIN" -B -c \
    'import telegram, dotenv, playwright, PIL; from zoneinfo import ZoneInfo; ZoneInfo("America/Argentina/Buenos_Aires")'

install_backups() (
    set -Eeuo pipefail
    for target in /usr/local/lib /usr/local/lib/storypulse-backups /usr/local/lib/storypulse-backups/storypulse_backups \
                  /etc/storypulse-backups /var/lib /var/spool; do
        root_directory "$target"
    done
    for target in /usr/local/lib/storypulse-backups/run.py /etc/storypulse-backups/config.json \
                  /etc/storypulse-backups/credentials.json /var/lib/storypulse-backups \
                  /var/lib/storypulse-backups/work /var/lib/storypulse-backups/mega /var/spool/storypulse-backups \
                  /etc/systemd/system/storypulse-backups.service; do
        safe_path "$target"
    done
    for target in /usr/local/lib/storypulse-backups/run.py /usr/local/lib/storypulse-backups/storypulse_backups/*.py; do
        [[ -e "$target" || -L "$target" ]] || continue
        [[ -f "$target" && ! -L "$target" && $(stat -c %u "$target") == 0 && $(stat -c %h "$target") == 1 ]]
        mode=$(stat -c %a "$target")
        (( (8#$mode & 8#022) == 0 ))
    done
    for target in /var/lib/storypulse-backups /var/lib/storypulse-backups/work /var/lib/storypulse-backups/mega; do
        [[ -e "$target" ]] || continue
        [[ -d "$target" && $(stat -c %u "$target") == 0 ]]
        mode=$(stat -c %a "$target")
        (( (8#$mode & 8#022) == 0 ))
    done
    if [[ -e /var/spool/storypulse-backups ]]; then
        [[ -d /var/spool/storypulse-backups && $(stat -c %u /var/spool/storypulse-backups) == "$(id -u "$RUN_USER")" \
           && $(stat -c %a /var/spool/storypulse-backups) == 700 ]]
    fi
    if [[ -e /usr/local/lib/storypulse-backups && ! -f /usr/local/lib/storypulse-backups/.storypulse-managed ]]; then
        printf 'Se conserva un directorio de backups previo ajeno a este instalador.\n' >&2
        exit 1
    fi
    if [[ -e /etc/systemd/system/storypulse-backups.service ]]; then
        if ! grep -Fq '# Managed by StoryPulse installer' /etc/systemd/system/storypulse-backups.service; then
            printf 'Se conserva un servicio de backups previo ajeno a este instalador.\n' >&2
            exit 1
        fi
        if systemctl is-active --quiet storypulse-backups.service; then
            python3 -B "$SUPPORT_DIR/configure.py" backups "$CONFIG"
            printf 'El servicio de backups ya está activo: no se sustituye código, clave ni estado.\n'
            exit 0
        fi
    fi
    case "$ARCH" in
        amd64) VC_SHA=4afcaefaada73de258a9a34e03898be957221f0178290a84f0be57a8ede51c29
               MEGA_SHA=d8c3c77d0417cba498fa6d4b3804fb657721afed072d6472c10e6b0b1b02dab9 ;;
        arm64) VC_SHA=f4c75c7a213e49361cb3d93446862bff97aef1b25f854386d61555273d99a045
               MEGA_SHA=6d032193817581984a698f641a2f35703a677d5697923df9fda43f7a317d528b ;;
    esac
    VC_NAME="veracrypt-console-$VERACRYPT_VERSION-Debian-13-$ARCH.deb"
    MEGA_NAME="megacmd_$MEGACMD_VERSION""_$ARCH.deb"
    curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 20 --max-time 240 \
        --output "$STAGE/$VC_NAME" "https://github.com/veracrypt/VeraCrypt/releases/download/VeraCrypt_$VERACRYPT_VERSION/$VC_NAME"
    printf '%s  %s\n' "$VC_SHA" "$STAGE/$VC_NAME" | sha256sum --check --status
    curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 20 --max-time 240 \
        --output "$STAGE/$MEGA_NAME" "https://mega.nz/linux/repo/Debian_13/$ARCH/$MEGA_NAME"
    printf '%s  %s\n' "$MEGA_SHA" "$STAGE/$MEGA_NAME" | sha256sum --check --status
    chmod 644 "$STAGE/$VC_NAME" "$STAGE/$MEGA_NAME"
    apt-get install -y --no-remove exfatprogs fuse3 dmsetup "$STAGE/$VC_NAME" "$STAGE/$MEGA_NAME"
    python3 -B "$SUPPORT_DIR/configure.py" backups "$CONFIG"
    [[ ! -L /usr/local/lib/storypulse-backups ]] || exit 1
    install -d -m 755 /usr/local/lib/storypulse-backups /usr/local/lib/storypulse-backups/storypulse_backups
    printf '%s\n' 'StoryPulse installer backups v1' > /usr/local/lib/storypulse-backups/.storypulse-managed
    chmod 600 /usr/local/lib/storypulse-backups/.storypulse-managed
    [[ -f "$PROJECT_DIR/backups_service/run.py" && ! -L "$PROJECT_DIR/backups_service/run.py" ]] || exit 1
    install -m 644 "$PROJECT_DIR/backups_service/run.py" /usr/local/lib/storypulse-backups/run.py
    for source in "$PROJECT_DIR"/backups_service/storypulse_backups/*.py; do
        [[ -f "$source" && ! -L "$source" ]] || exit 1
        install -m 644 "$source" "/usr/local/lib/storypulse-backups/storypulse_backups/${source##*/}"
    done
    install -d -m 750 -o root -g "$RUN_GROUP" /var/lib/storypulse-backups
    install -d -m 700 -o root -g root /var/lib/storypulse-backups/work /var/lib/storypulse-backups/mega
    install -d -m 700 -o "$RUN_USER" -g "$RUN_GROUP" /var/spool/storypulse-backups
    python3 -I -B /usr/local/lib/storypulse-backups/run.py --check
    if [[ "$SKIP_START" == 0 ]]; then
        python3 -I -B - <<'PY'
import sys
sys.path.insert(0, '/usr/local/lib/storypulse-backups')
from pathlib import Path
from storypulse_backups.core import credentials
from storypulse_backups.adapters import CloudAdapter
secret = credentials(Path('/etc/storypulse-backups/credentials.json'))
try:
    CloudAdapter().ensure_session(secret['mega_email'], secret['mega_password'])
except Exception:
    print('MEGA no confirmó el acceso. Los backups quedan sin activar; el bot continúa.', file=sys.stderr)
    sys.exit(1)
finally:
    secret.clear()
PY
    fi
    cat > "$STAGE/storypulse-backups.service" <<'UNIT'
# Managed by StoryPulse installer
[Unit]
Description=StoryPulse encrypted history backups
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=root
Group=root
WorkingDirectory=/usr/local/lib/storypulse-backups
ExecStart=/usr/bin/python3 -I -B /usr/local/lib/storypulse-backups/run.py
Environment=HOME=/var/lib/storypulse-backups/mega
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
Restart=on-failure
RestartSec=10
TimeoutStopSec=infinity
KillMode=mixed
ProtectSystem=full
ReadOnlyPaths=/historys
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
UNIT
    install -m 644 "$STAGE/storypulse-backups.service" /etc/systemd/system/storypulse-backups.service
    systemctl daemon-reload
    if [[ "$SKIP_START" == 0 ]]; then systemctl enable --now storypulse-backups.service; fi
)
if [[ "$WANT_BACKUPS" == yes ]]; then
    export STAGE ARCH VERACRYPT_VERSION MEGACMD_VERSION SUPPORT_DIR CONFIG PROJECT_DIR RUN_USER RUN_GROUP SKIP_START
    export -f install_backups safe_path root_directory
    if run_step 'Preparando backups cifrados y MEGA' bash -Eeuo pipefail -c install_backups; then
        BACKUPS_RESULT='Preparados; credenciales en /etc/storypulse-backups/credentials.json (root:600)'
    else
        BACKUPS_RESULT='Pendientes: falló MEGA o una dependencia opcional. El bot sigue instalado.'
        printf '%s\n' "$BACKUPS_RESULT" >&2
    fi
fi

install_filebrowser_optional() (
    set -Eeuo pipefail
    FB_HELPER="$SUPPORT_DIR/filebrowser-quantum.sh"
    if [[ ! -f "$FB_HELPER" ]]; then
        FB_HELPER="$STAGE/filebrowser-quantum.sh"
        curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 20 --max-time 90 \
            --output "$FB_HELPER" 'https://raw.githubusercontent.com/FacuSecX/StoryPulse-Private/main/installer/filebrowser-quantum.sh'
    fi
    source "$FB_HELPER"
    install_filebrowser_quantum "$RUN_USER" "$RUN_GROUP" /historys
)
if [[ "$WANT_FILEBROWSER" == yes ]]; then
    export STAGE SUPPORT_DIR RUN_USER RUN_GROUP SKIP_START
    export -f install_filebrowser_optional
    if run_step 'Instalando FileBrowser Quantum' bash -Eeuo pipefail -c install_filebrowser_optional; then
        FILEBROWSER_RESULT='Local: http://127.0.0.1:8085 (usar túnel SSH o proxy HTTPS)'
    else
        FILEBROWSER_RESULT='Pendiente: falló el paso opcional. El bot sigue instalado.'
        printf 'FileBrowser: %s\n' "$FILEBROWSER_RESULT" >&2
    fi
fi

cat > "$STAGE/bot.service" <<UNIT
# Managed by StoryPulse installer
[Unit]
Description=StoryPulse Private Telegram Bot
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PLAYWRIGHT_BROWSERS_PATH=$BROWSERS_DIR
ExecStart=$PYTHON_BIN -B $PROJECT_DIR/bot.py
Restart=on-failure
RestartSec=10
KillSignal=SIGINT
KillMode=mixed
TimeoutStopSec=120
NoNewPrivileges=true
PrivateTmp=true
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
install -m 644 "$STAGE/bot.service" "$SERVICE_FILE"
run_step 'Verificando servicio del bot' systemd-analyze verify "$SERVICE_FILE"
run_step 'Registrando servicio del bot' systemctl daemon-reload
if [[ "$SKIP_START" == 0 ]]; then
    run_step 'Habilitando servicio del bot' systemctl enable "$SERVICE_NAME.service"
    run_step 'Iniciando servicio del bot' systemctl restart "$SERVICE_NAME.service"
    sleep 3
    systemctl is-active --quiet "$SERVICE_NAME.service" || die 'El bot no arrancó. Revisá su journal privado.'
else
    printf 'Modo de prueba: el bot y los backups quedaron preparados sin iniciar conexiones.\n'
fi
printf '\nInstalación completada.\nProyecto: %s\nServicio: %s.service\nFileBrowser: %s\nBackups: %s\n' \
    "$PROJECT_DIR" "$SERVICE_NAME" "$FILEBROWSER_RESULT" "$BACKUPS_RESULT"
printf 'En Telegram: /start. Para consultar Instagram, creá tu sesión con CREAR_SESSION.bat o crear_session.py y transferila al proyecto.\n'
printf 'No se abrieron puertos ni se modificaron Nginx, SSH o el firewall.\n'
printf 'Registro detallado privado: %s\n' "$LOG_FILE"

#!/usr/bin/env bash
set -Eeuo pipefail

# ============================================================
# StoryPulse Private - Instalador Linux
# ============================================================
# Diseñado para ejecutarse desde la raíz del repositorio.
#
# Instala:
#   - Python + pip + venv
#   - dependencias de requirements.txt
#   - Chromium para Playwright
#   - usuario de servicio "storypulse"
#   - proyecto en el home del usuario de servicio
#   - almacenamiento persistente en /historys
#   - servicio systemd de StoryPulse
#   - File Browser opcional apuntando a /historys
#
# IMPORTANTE:
#   - Debe ejecutarse como root.
#   - Playwright soporta oficialmente Debian/Ubuntu en Linux.
#     En otras distribuciones se intenta una instalación compatible
#     y luego se verifica que Chromium pueda iniciar correctamente.
# ============================================================

APP_USER="storypulse"
APP_NAME="StoryPulse-Private"
STORAGE_DIR="/historys"
SERVICE_NAME="storypulse"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

FB_SERVICE_NAME="storypulse-filemanager"
FB_DB_DIR=""
FB_DB=""
FB_BIND="127.0.0.1"
FB_PORT="8080"
FB_PASSWORD_CREATED=""

green()  { printf '\033[1;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[1;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[1;31m%s\033[0m\n' "$*"; }
blue()   { printf '\033[1;34m%s\033[0m\n' "$*"; }

die() {
    red "ERROR: $*"
    exit 1
}

trap 'red "La instalación se detuvo en la línea $LINENO."; exit 1' ERR

echo
echo "============================================================"
echo "             StoryPulse Private - Instalador"
echo "============================================================"
echo

# ------------------------------------------------------------
# 1. ROOT
# ------------------------------------------------------------
if [[ "${EUID}" -ne 0 ]]; then
    die "Necesitás ejecutar este instalador como usuario root.
Ejemplo:
    sudo bash setup_server.sh"
fi

green "✓ Permisos root confirmados"

# ------------------------------------------------------------
# 2. VERIFICACIONES BÁSICAS
# ------------------------------------------------------------
if [[ ! -f "$SOURCE_DIR/bot.py" ]]; then
    die "No encuentro bot.py junto al instalador."
fi

for required in history.py publicaciones.py database.py requirements.txt .env.example; do
    [[ -f "$SOURCE_DIR/$required" ]] || die "Falta $required en $SOURCE_DIR"
done

if ! command -v systemctl >/dev/null 2>&1; then
    die "Este instalador necesita una distribución con systemd/systemctl."
fi

# ------------------------------------------------------------
# 3. DETECTAR DISTRIBUCIÓN / GESTOR DE PAQUETES
# ------------------------------------------------------------
OS_ID="unknown"
OS_LIKE=""

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"
    OS_LIKE="${ID_LIKE:-}"
fi

PKG_MANAGER=""

if command -v apt-get >/dev/null 2>&1; then
    PKG_MANAGER="apt"
elif command -v dnf >/dev/null 2>&1; then
    PKG_MANAGER="dnf"
elif command -v yum >/dev/null 2>&1; then
    PKG_MANAGER="yum"
elif command -v zypper >/dev/null 2>&1; then
    PKG_MANAGER="zypper"
elif command -v pacman >/dev/null 2>&1; then
    PKG_MANAGER="pacman"
else
    die "No se detectó un gestor compatible (apt, dnf, yum, zypper o pacman)."
fi

blue "Distribución detectada: ${OS_ID}"
blue "Gestor de paquetes: ${PKG_MANAGER}"

install_base_packages() {
    case "$PKG_MANAGER" in
        apt)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update
            apt-get install -y \
                python3 python3-pip python3-venv \
                curl ca-certificates tar gzip
            ;;
        dnf)
            dnf install -y \
                python3 python3-pip \
                curl ca-certificates tar gzip
            ;;
        yum)
            yum install -y \
                python3 python3-pip \
                curl ca-certificates tar gzip
            ;;
        zypper)
            zypper --non-interactive refresh
            zypper --non-interactive install \
                python3 python3-pip \
                curl ca-certificates tar gzip
            ;;
        pacman)
            pacman -Sy --noconfirm --needed \
                python python-pip \
                curl ca-certificates tar gzip
            ;;
    esac
}

echo
blue "[1/9] Instalando dependencias base del sistema..."
install_base_packages

command -v python3 >/dev/null 2>&1 || die "Python 3 no quedó instalado."
command -v curl >/dev/null 2>&1 || die "curl no quedó instalado."

PYTHON_BIN="$(command -v python3)"

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(
        f"StoryPulse requiere Python 3.11 o superior. "
        f"Detectado: {sys.version.split()[0]}"
    )
print(f"Python detectado: {sys.version.split()[0]}")
PY

# ------------------------------------------------------------
# 4. USUARIO DE SERVICIO Y HOME UNIVERSAL
# ------------------------------------------------------------
echo
blue "[2/9] Preparando usuario y directorio del proyecto..."

if ! id "$APP_USER" >/dev/null 2>&1; then
    command -v useradd >/dev/null 2>&1 || die "No existe el comando useradd."

    NOLOGIN_SHELL="$(command -v nologin 2>/dev/null || true)"
    [[ -n "$NOLOGIN_SHELL" ]] || NOLOGIN_SHELL="/bin/false"

    useradd \
        --create-home \
        --home-dir "/home/$APP_USER" \
        --shell "$NOLOGIN_SHELL" \
        "$APP_USER"
fi

APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
[[ -n "$APP_HOME" ]] || APP_HOME="/home/$APP_USER"

APP_GROUP="$(id -gn "$APP_USER")"
APP_DIR="$APP_HOME/$APP_NAME"
VENV_DIR="$APP_DIR/venv"
PW_DIR="$APP_HOME/.cache/ms-playwright"

mkdir -p "$APP_DIR" "$PW_DIR"

# Copiamos sólo archivos públicos/esperados del repositorio.
PUBLIC_FILES=(
    "bot.py"
    "history.py"
    "publicaciones.py"
    "database.py"
    "crearsession.py"
    "requirements.txt"
    ".env.example"
)

OPTIONAL_FILES=(
    "README.md"
    ".gitignore"
)

for file in "${PUBLIC_FILES[@]}"; do
    cp -f "$SOURCE_DIR/$file" "$APP_DIR/$file"
done

for file in "${OPTIONAL_FILES[@]}"; do
    if [[ -f "$SOURCE_DIR/$file" ]]; then
        cp -f "$SOURCE_DIR/$file" "$APP_DIR/$file"
    fi
done

chown -R "$APP_USER:$APP_GROUP" "$APP_HOME"
chmod 750 "$APP_HOME" "$APP_DIR"

green "✓ Proyecto instalado en: $APP_DIR"

# ------------------------------------------------------------
# 5. /historys
# ------------------------------------------------------------
echo
blue "[3/9] Creando almacenamiento raíz..."

mkdir -p "$STORAGE_DIR"
chown "$APP_USER:$APP_GROUP" "$STORAGE_DIR"
chmod 750 "$STORAGE_DIR"

green "✓ Almacenamiento preparado en: $STORAGE_DIR"

# ------------------------------------------------------------
# 6. VENV + REQUIREMENTS
# ------------------------------------------------------------
echo
blue "[4/9] Creando entorno virtual..."

rm -rf "$VENV_DIR"
"$PYTHON_BIN" -m venv "$VENV_DIR"

"$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

chown -R "$APP_USER:$APP_GROUP" "$VENV_DIR"

green "✓ Entorno virtual y requirements instalados"

# ------------------------------------------------------------
# 7. PLAYWRIGHT + CHROMIUM
# ------------------------------------------------------------
echo
blue "[5/9] Instalando Chromium para Playwright..."

PLAYWRIGHT_OFFICIALLY_SUPPORTED="no"
case "$OS_ID" in
    debian|ubuntu)
        PLAYWRIGHT_OFFICIALLY_SUPPORTED="yes"
        ;;
esac

if [[ "$PLAYWRIGHT_OFFICIALLY_SUPPORTED" == "yes" ]]; then
    PLAYWRIGHT_BROWSERS_PATH="$PW_DIR" \
        "$VENV_DIR/bin/python" -m playwright install --with-deps chromium
else
    yellow "⚠ Playwright no declara soporte oficial para '${OS_ID}'."
    yellow "  Se instalará Chromium y luego se hará una prueba real de arranque."

    PLAYWRIGHT_BROWSERS_PATH="$PW_DIR" \
        "$VENV_DIR/bin/python" -m playwright install chromium
fi

chown -R "$APP_USER:$APP_GROUP" "$PW_DIR"

echo
blue "Verificando que Chromium pueda iniciar..."

if ! PLAYWRIGHT_BROWSERS_PATH="$PW_DIR" \
    "$VENV_DIR/bin/python" - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    browser.close()
print("Chromium OK")
PY
then
    die "Chromium/Playwright no pudo iniciar en esta distribución.
La instalación base quedó creada, pero esta distribución necesita
dependencias adicionales o no es compatible con el Chromium de Playwright."
fi

green "✓ Playwright + Chromium funcionando"

# ------------------------------------------------------------
# 8. FILE MANAGER OPCIONAL
# ------------------------------------------------------------
echo
echo "============================================================"
echo "              PANEL WEB / FILE MANAGER"
echo "============================================================"
echo
read -r -p "¿Deseás instalar un FILE MANAGER web para /historys? [s/N]: " INSTALL_FB
INSTALL_FB="${INSTALL_FB,,}"

PANEL_URL=""

if [[ "$INSTALL_FB" == "s" || "$INSTALL_FB" == "si" || "$INSTALL_FB" == "sí" || "$INSTALL_FB" == "y" || "$INSTALL_FB" == "yes" ]]; then
    echo
    blue "[6/9] Instalando File Browser..."

    # Instalador oficial de File Browser.
    curl -fsSL https://raw.githubusercontent.com/filebrowser/get/master/get.sh | bash

    FILEBROWSER_BIN="$(command -v filebrowser || true)"
    [[ -n "$FILEBROWSER_BIN" ]] || die "File Browser no quedó instalado."

    read -r -p "Puerto del File Manager [8080]: " FB_PORT_INPUT
    FB_PORT="${FB_PORT_INPUT:-8080}"

    if ! [[ "$FB_PORT" =~ ^[0-9]+$ ]] || (( FB_PORT < 1 || FB_PORT > 65535 )); then
        die "Puerto inválido: $FB_PORT"
    fi

    echo
    echo "Por seguridad puede quedar escuchando sólo en localhost"
    echo "para usarlo detrás de Nginx/Cloudflare/reverse proxy."
    echo
    read -r -p "¿Querés exponerlo directamente en la red? [s/N]: " FB_PUBLIC
    FB_PUBLIC="${FB_PUBLIC,,}"

    if [[ "$FB_PUBLIC" == "s" || "$FB_PUBLIC" == "si" || "$FB_PUBLIC" == "sí" || "$FB_PUBLIC" == "y" || "$FB_PUBLIC" == "yes" ]]; then
        FB_BIND="0.0.0.0"
        yellow "⚠ El File Manager quedará escuchando en 0.0.0.0:$FB_PORT"
        yellow "  El instalador NO abre el firewall ni configura HTTPS."
    else
        FB_BIND="127.0.0.1"
        green "✓ File Manager limitado a localhost:$FB_PORT"
    fi

    FB_DB_DIR="$APP_HOME/.filebrowser"
    FB_DB="$FB_DB_DIR/filebrowser.db"
    mkdir -p "$FB_DB_DIR"
    chown "$APP_USER:$APP_GROUP" "$FB_DB_DIR"

    if [[ ! -f "$FB_DB" ]]; then
        "$FILEBROWSER_BIN" \
            -d "$FB_DB" \
            config init \
            --root "$STORAGE_DIR" \
            --address "$FB_BIND" \
            --port "$FB_PORT" \
            --branding.name "StoryPulse Files"

        FB_PASSWORD_CREATED="$("$PYTHON_BIN" - <<'PY'
import secrets
print(secrets.token_urlsafe(18))
PY
)"

        "$FILEBROWSER_BIN" \
            -d "$FB_DB" \
            users add admin "$FB_PASSWORD_CREATED" \
            --perm.admin

        chown -R "$APP_USER:$APP_GROUP" "$FB_DB_DIR"
        chmod 700 "$FB_DB_DIR"
        chmod 600 "$FB_DB"
    else
        yellow "File Browser ya tenía base de datos. Se conservarán sus usuarios."
        "$FILEBROWSER_BIN" \
            -d "$FB_DB" \
            config set \
            --root "$STORAGE_DIR" \
            --address "$FB_BIND" \
            --port "$FB_PORT"
    fi

    cat > "/etc/systemd/system/${FB_SERVICE_NAME}.service" <<EOF
[Unit]
Description=StoryPulse File Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
ExecStart=$FILEBROWSER_BIN -d $FB_DB -r $STORAGE_DIR -a $FB_BIND -p $FB_PORT
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable --now "${FB_SERVICE_NAME}.service"

    if [[ "$FB_BIND" == "0.0.0.0" ]]; then
        SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
        if [[ -n "$SERVER_IP" ]]; then
            DEFAULT_PANEL_URL="http://${SERVER_IP}:${FB_PORT}/"
        else
            DEFAULT_PANEL_URL="http://SERVIDOR:${FB_PORT}/"
        fi
    else
        DEFAULT_PANEL_URL="http://127.0.0.1:${FB_PORT}/"
    fi

    echo
    read -r -p "URL que querés usar en el botón del bot [$DEFAULT_PANEL_URL]: " PANEL_INPUT
    PANEL_URL="${PANEL_INPUT:-$DEFAULT_PANEL_URL}"

    green "✓ File Manager instalado sobre $STORAGE_DIR"
else
    blue "[6/9] File Manager omitido por el usuario."
    echo
    read -r -p "URL de un panel existente (opcional, ENTER para usar example.com): " PANEL_INPUT
    PANEL_URL="${PANEL_INPUT:-https://example.com/}"
fi

# ------------------------------------------------------------
# 9. CONFIGURACIÓN .env
# ------------------------------------------------------------
echo
blue "[7/9] Configuración inicial de StoryPulse..."
echo

DEFAULT_TZ="UTC"
if command -v timedatectl >/dev/null 2>&1; then
    DETECTED_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
    [[ -n "$DETECTED_TZ" ]] && DEFAULT_TZ="$DETECTED_TZ"
elif [[ -f /etc/timezone ]]; then
    DETECTED_TZ="$(head -n1 /etc/timezone 2>/dev/null || true)"
    [[ -n "$DETECTED_TZ" ]] && DEFAULT_TZ="$DETECTED_TZ"
fi

read -r -s -p "Token del bot de Telegram (ENTER para configurar después): " TELEGRAM_TOKEN
echo
read -r -p "Telegram Chat ID autorizado (ENTER para configurar después): " TELEGRAM_CHAT_ID
read -r -p "Zona horaria [$DEFAULT_TZ]: " STORYPULSE_TZ
STORYPULSE_TZ="${STORYPULSE_TZ:-$DEFAULT_TZ}"

cat > "$APP_DIR/.env" <<EOF
# StoryPulse Private - Configuración local
# NO subir este archivo a GitHub.

TELEGRAM_BOT_TOKEN=$TELEGRAM_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
INSTAGRAM_STORAGE_STATE=instagram_state.json
HISTORYS_DIR=$STORAGE_DIR
STORYPULSE_PANEL_URL=$PANEL_URL
STORYPULSE_TIMEZONE=$STORYPULSE_TZ
EOF

chown "$APP_USER:$APP_GROUP" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

green "✓ .env creado"

# ------------------------------------------------------------
# 10. SERVICIO SYSTEMD STORYPULSE
# ------------------------------------------------------------
echo
blue "[8/9] Creando servicio systemd..."

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=StoryPulse Private Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_GROUP
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
Environment=PLAYWRIGHT_BROWSERS_PATH=$PW_DIR
ExecStart=$VENV_DIR/bin/python $APP_DIR/bot.py
Restart=always
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"

if [[ -n "$TELEGRAM_TOKEN" && -n "$TELEGRAM_CHAT_ID" ]]; then
    systemctl restart "${SERVICE_NAME}.service"
    green "✓ StoryPulse iniciado"
else
    yellow "⚠ StoryPulse quedó instalado pero NO iniciado."
    yellow "  Completá TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID en:"
    yellow "  $APP_DIR/.env"
    yellow "  Después ejecutá: systemctl start ${SERVICE_NAME}"
fi

# ------------------------------------------------------------
# 11. RESUMEN
# ------------------------------------------------------------
echo
blue "[9/9] Instalación terminada"
echo
echo "============================================================"
echo "                 INSTALACIÓN COMPLETADA"
echo "============================================================"
echo
echo "Proyecto:"
echo "  $APP_DIR"
echo
echo "Almacenamiento:"
echo "  $STORAGE_DIR"
echo
echo "Entorno virtual:"
echo "  $VENV_DIR"
echo
echo "Configuración privada:"
echo "  $APP_DIR/.env"
echo
echo "Sesión de Instagram:"
echo "  Copiá instagram_state.json a:"
echo "  $APP_DIR/instagram_state.json"
echo
echo "Servicio StoryPulse:"
echo "  systemctl status ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
echo

if [[ "$INSTALL_FB" == "s" || "$INSTALL_FB" == "si" || "$INSTALL_FB" == "sí" || "$INSTALL_FB" == "y" || "$INSTALL_FB" == "yes" ]]; then
    echo "File Manager:"
    echo "  Raíz: $STORAGE_DIR"
    echo "  Escucha: $FB_BIND:$FB_PORT"
    echo "  Servicio: ${FB_SERVICE_NAME}.service"
    echo "  URL configurada: $PANEL_URL"
    echo

    if [[ -n "$FB_PASSWORD_CREATED" ]]; then
        yellow "IMPORTANTE: credenciales iniciales del File Manager"
        echo "  Usuario: admin"
        echo "  Contraseña: $FB_PASSWORD_CREATED"
        yellow "Guardá esta contraseña ahora. No se volverá a mostrar."
        echo
    fi
fi

yellow "IMPORTANTE:"
echo "  instagram_state.json NO se crea en el servidor."
echo "  Generá la sesión en un equipo con interfaz gráfica usando crearsession.py"
echo "  y copiá solamente instagram_state.json al directorio del proyecto."
echo
green "StoryPulse Private listo."
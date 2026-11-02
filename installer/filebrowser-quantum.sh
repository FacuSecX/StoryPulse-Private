#!/usr/bin/env bash
# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

fbq_error() { printf 'FileBrowser: %s\n' "$*" >&2; return 1; }

fbq_safe_path() {
    local path=$1 component current=''
    [[ $path == /* && $path != *'/../'* && $path != *'/./'* && $path != */.. && $path != */. ]] || return 1
    IFS='/' read -r -a fbq_components <<< "$path"
    for component in "${fbq_components[@]}"; do
        [[ -n $component ]] || continue
        current+="/$component"
        [[ ! -L $current ]] || return 1
    done
}

install_filebrowser_quantum() (
    set -euo pipefail
    umask 077
    local runtime_user=${1:?usuario requerido} runtime_group=${2:?grupo requerido} media_root=${3:-/historys}
    local version='v1.5.1-stable'
    local expected_sha arch
    local install_dir='/usr/local/lib/storypulse-filebrowser'
    local config_dir='/etc/storypulse-filebrowser'
    local state_dir='/var/lib/storypulse-filebrowser'
    local unit_path='/etc/systemd/system/storypulse-filebrowser.service'
    local marker='StoryPulse FileBrowser Quantum installer v1'
    local tmp='' uid gid
    trap '[[ -z $tmp ]] || rm -f -- "$tmp"' EXIT

    [[ $EUID -eq 0 ]] || { fbq_error 'requiere root.'; exit 1; }
    arch=$(dpkg --print-architecture)
    case "$arch" in
        amd64) expected_sha='e4d6fada87b2f15222d9f1ec0f0348d9d38dc2f50a7f56531e0edf9d018330c9' ;;
        arm64) expected_sha='5531876d6083a7c59c7035a609ed75dec0182920ffb1fefc1a601e7c0dd576a9' ;;
        *) fbq_error 'arquitectura no admitida: se requiere amd64 o arm64.'; exit 1 ;;
    esac
    [[ $runtime_user =~ ^[a-z_][a-z0-9_-]*[$]?$ && $runtime_user != root ]] || { fbq_error 'usuario de ejecución inválido.'; exit 1; }
    [[ $runtime_group =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || { fbq_error 'grupo inválido.'; exit 1; }
    uid=$(id -u -- "$runtime_user")
    gid=$(getent group "$runtime_group" | cut -d: -f3)
    [[ $uid -gt 0 && $gid =~ ^[0-9]+$ ]] || { fbq_error 'usuario o grupo inexistente.'; exit 1; }
    [[ $media_root == /historys ]] || { fbq_error 'la raíz debe ser /historys.'; exit 1; }

    local p
    for p in "$install_dir" "$install_dir/filebrowser" "$install_dir/.storypulse-managed" \
             "$config_dir" "$config_dir/config.yaml" "$config_dir/credentials.env" \
             "$config_dir/.storypulse-managed" "$config_dir/.runtime-user" "$state_dir" "$state_dir/cache" \
             "$state_dir/database.db" "$unit_path" "$media_root"; do
        fbq_safe_path "$p" || { fbq_error "ruta insegura: $p"; exit 1; }
    done
    if [[ -e $unit_path ]] && ! grep -Fxq "# $marker" "$unit_path"; then
        fbq_error 'existe una unidad ajena; se conserva.'; exit 1
    fi
    for p in "$install_dir" "$config_dir"; do
        if [[ -e $p && ( ! -d $p || ! -f $p/.storypulse-managed ) ]]; then
            fbq_error "directorio ajeno: $p; se conserva."; exit 1
        fi
        if [[ -e $p ]] && [[ $(stat -c %u "$p") != 0 || $((8#$(stat -c %a "$p") & 8#022)) != 0 ]]; then
            fbq_error "directorio de configuración inseguro: $p; se conserva."; exit 1
        fi
    done
    if [[ -e $state_dir && ( ! -d $state_dir || ! -f $config_dir/.storypulse-managed ) ]]; then
        fbq_error 'existe un directorio de datos ajeno; se conserva.'; exit 1
    fi
    if [[ -d $config_dir ]] && [[ ! -f $config_dir/.runtime-user || $(cat "$config_dir/.runtime-user") != "$runtime_user:$runtime_group" ]]; then
        fbq_error 'la instalación anterior pertenece a otro usuario; se conserva.'; exit 1
    fi
    if ss -H -lnt 'sport = :8085' | grep -q .; then
        systemctl is-active --quiet storypulse-filebrowser.service || {
            fbq_error 'el puerto local 8085 está ocupado por otro servicio; se conserva.'; exit 1;
        }
    fi

    install -d -m 0755 -o root -g root "$install_dir"
    install -d -m 0750 -o root -g "$runtime_group" "$config_dir"
    install -d -m 0700 -o "$runtime_user" -g "$runtime_group" "$state_dir" "$state_dir/cache"
    if [[ ! -d $media_root ]]; then
        install -d -m 2770 -o "$runtime_user" -g "$runtime_group" "$media_root"
    fi
    runuser -u "$runtime_user" -- test -r "$media_root" || { fbq_error 'el usuario no puede leer /historys.'; exit 1; }
    runuser -u "$runtime_user" -- test -w "$media_root" || { fbq_error 'el usuario no puede escribir /historys.'; exit 1; }
    printf '%s\n' "$marker" > "$install_dir/.storypulse-managed"
    printf '%s\n' "$marker" > "$config_dir/.storypulse-managed"
    printf '%s\n' "$runtime_user:$runtime_group" > "$config_dir/.runtime-user"
    chmod 0600 "$install_dir/.storypulse-managed" "$config_dir/.storypulse-managed" "$config_dir/.runtime-user"

    if [[ -e $install_dir/filebrowser ]]; then
        [[ -f $install_dir/filebrowser ]] || { fbq_error 'binario inválido; se conserva.'; exit 1; }
        printf '%s  %s\n' "$expected_sha" "$install_dir/filebrowser" | sha256sum -c --status || {
            fbq_error 'el binario existente no coincide con la versión oficial; se conserva.'; exit 1;
        }
    else
        tmp=$(mktemp "$install_dir/.download.XXXXXXXX")
        curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
            --retry 3 --connect-timeout 30 --max-time 600 \
            "https://github.com/gtsteffaniak/filebrowser/releases/download/$version/linux-$arch-filebrowser" -o "$tmp"
        printf '%s  %s\n' "$expected_sha" "$tmp" | sha256sum -c --status || {
            fbq_error 'la descarga no coincide con el SHA-256 oficial.'; exit 1;
        }
        chmod 0755 "$tmp"
        chown root:root "$tmp"
        mv -T -- "$tmp" "$install_dir/filebrowser"
        tmp=''
    fi

    python3 - "$config_dir" "$state_dir" "$media_root" "$uid" "$gid" <<'PY'
import json, os, pathlib, secrets, stat, sys
config_dir, state_dir, source = map(pathlib.Path, sys.argv[1:4])
uid, gid = map(int, sys.argv[4:6])
config = config_dir / 'config.yaml'
secret = config_dir / 'credentials.env'
for path in (config, secret):
    if path.exists():
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise SystemExit('Archivo FileBrowser inseguro; se conserva sin modificar.')
if config.exists():
    cfg = json.loads(config.read_text())
    server = cfg.get('server', {})
    sources = server.get('sources', [])
    auth = cfg.get('auth', {})
    methods = auth.get('methods', {})
    if (server.get('listen') != '127.0.0.1' or server.get('port') != 8085
        or [v.get('path') for v in sources] != [str(source)]
        or methods.get('noauth', False) or not methods.get('password', {}).get('enabled')
        or methods.get('password', {}).get('signup', False)
        or methods.get('proxy', {}).get('enabled', False)):
        raise SystemExit('Configuración FileBrowser existente no local/segura; se conserva y se detiene este paso.')
    if not secret.exists():
        raise SystemExit('Falta credentials.env en la instalación existente; no se reemplaza su contraseña.')
else:
    if secret.exists() or (state_dir / 'database.db').exists():
        raise SystemExit('Se encontraron datos previos sin configuración; no se reemplazan.')
    cfg = {
        'server': {'port': 8085, 'listen': '127.0.0.1', 'baseURL': '/',
            'database': str(state_dir / 'database.db'),
            'cacheDir': str(state_dir / 'cache'), 'disableUpdateCheck': True,
            'disableWebDAV': True,
            'logging': [{'levels': 'info|warning|error', 'apiLevels': 'info|warning|error',
                         'output': 'stdout', 'noColors': True}],
            'filesystem': {'createFilePermission': '660', 'createDirectoryPermission': '770'},
            'sources': [{'path': str(source), 'config': {'defaultEnabled': True, 'defaultUserScope': '/'}}]},
        'auth': {'adminUsername': 'administrador', 'methods': {'noauth': False,
            'proxy': {'enabled': False}, 'password': {'enabled': True, 'minLength': 10, 'signup': False}}},
        'frontend': {'name': 'StoryPulse Files'},
        'userDefaults': {'listing': {'deleteAfterArchive': False},
            'account': {'permissions': {'admin': False, 'share': False, 'modify': False,
                'delete': False, 'create': False, 'download': True}}},
    }
    with secret.open('x', encoding='utf-8') as fh:
        fh.write('FILEBROWSER_ADMIN_PASSWORD=' + secrets.token_urlsafe(36) + '\n')
    os.chmod(secret, 0o600)
    with config.open('x', encoding='utf-8') as fh:
        json.dump(cfg, fh, indent=2); fh.write('\n')
    os.chown(config, 0, gid)
    os.chmod(config, 0o640)
if stat.S_IMODE(secret.stat().st_mode) != 0o600:
    raise SystemExit('credentials.env debe tener permisos 0600.')
PY

    tmp=$(mktemp '/etc/systemd/system/.storypulse-filebrowser.XXXXXXXX')
    cat > "$tmp" <<EOF
# $marker
[Unit]
Description=StoryPulse Files - FileBrowser Quantum (solo localhost)
After=network.target
ConditionPathIsDirectory=$media_root

[Service]
Type=simple
User=$runtime_user
Group=$runtime_group
WorkingDirectory=$state_dir
EnvironmentFile=$config_dir/credentials.env
ExecStart=$install_dir/filebrowser -c $config_dir/config.yaml
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
UMask=0007
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$state_dir $media_root
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
EOF
    chmod 0644 "$tmp"
    mv -T -- "$tmp" "$unit_path"
    tmp=''
    systemctl daemon-reload
    if [[ ${SKIP_START:-0} == 1 ]]; then
        printf '%s\n' 'FileBrowser Quantum instalado; arranque omitido por --skip-start.'
        exit 0
    fi
    systemctl enable --now storypulse-filebrowser.service
    local ready=false attempt
    for attempt in {1..30}; do
        if systemctl is-active --quiet storypulse-filebrowser.service && \
           curl --proto '=http' --noproxy '*' --fail --silent --max-time 2 http://127.0.0.1:8085/health >/dev/null; then
            ready=true; break
        fi
        sleep 1
    done
    [[ $ready == true ]] || { fbq_error 'no superó la verificación local de arranque; revisá su servicio.'; exit 1; }
    printf '%s\n' 'FileBrowser Quantum activo únicamente en http://127.0.0.1:8085.' \
        'Usuario inicial: administrador. Contraseña inicial protegida: /etc/storypulse-filebrowser/credentials.env.' \
        'Para abrirlo desde otra PC usá un túnel SSH o un proxy HTTPS configurado por vos.'
)

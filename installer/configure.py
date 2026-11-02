# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Private installer configuration. No secret is returned on stdout or argv."""
from __future__ import annotations

import argparse
import getpass
import json
import os
import pwd
import re
import secrets
import stat
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

SERVICE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
ARGENTINA_TIMEZONE = "America/Argentina/Buenos_Aires"


def private_read(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
            raise ValueError("La configuración debe ser un archivo de root con permisos 600.")
        result = json.load(stream)
    if not isinstance(result, dict):
        raise ValueError("Configuración inválida.")
    return result


def atomic(path: Path, data: bytes, uid=0, gid=0, mode=0o600):
    temp = path.parent / ("." + path.name + "." + secrets.token_hex(8))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def save(path: Path, data: dict, **kwargs):
    atomic(path, (json.dumps(data, ensure_ascii=False) + "\n").encode(), **kwargs)


def safe_text(value, label, maximum=4096):
    if not isinstance(value, str) or len(value) > maximum or any(c in value for c in "\0\r\n"):
        raise ValueError("Campo inválido: " + label)
    return value


def validate(data):
    allowed = {"service", "user", "telegram_token", "telegram_chat_id", "timezone", "panel_url",
               "backups", "mega_email", "mega_password", "filebrowser"}
    if set(data) - allowed:
        raise ValueError("La configuración contiene campos desconocidos.")
    result = {"timezone": ARGENTINA_TIMEZONE, "panel_url": "", "backups": False,
              "mega_email": "", "mega_password": "", "filebrowser": False, **data}
    service = safe_text(result.get("service"), "service")
    if service.endswith(".service"):
        service = service[:-8]
    if not SERVICE.fullmatch(service) or service in {
            "ssh", "sshd", "nginx", "apache2", "systemd", "cron", "dbus", "networking",
            "storypulse-backups", "storypulse-filebrowser"}:
        raise ValueError("Nombre de servicio inválido o reservado.")
    result["service"] = service
    user = safe_text(result.get("user"), "user")
    if not USER.fullmatch(user):
        raise ValueError("Usuario del sistema inválido.")
    account = pwd.getpwnam(user)
    home = Path(account.pw_dir)
    if account.pw_uid == 0 or home.parent != Path("/home") or home.name != user or home.is_symlink():
        raise ValueError("Usá un usuario existente con carpeta propia /home/usuario; nunca root.")
    token = safe_text(result.get("telegram_token"), "telegram_token")
    if not re.fullmatch(r"[0-9]{6,16}:[A-Za-z0-9_-]{25,100}", token):
        raise ValueError("El token de Telegram no tiene un formato válido.")
    chat = str(result.get("telegram_chat_id", ""))
    if not re.fullmatch(r"[1-9][0-9]{0,19}", chat):
        raise ValueError("Indicá tu ID personal positivo de Telegram; el bot es de uso privado.")
    result["telegram_chat_id"] = chat
    ZoneInfo(safe_text(result["timezone"], "timezone", 128))
    if result["timezone"] != ARGENTINA_TIMEZONE:
        raise ValueError("Este instalador configura el servidor y el bot con la hora de Argentina.")
    url = safe_text(result["panel_url"], "panel_url", 2048)
    if url and (not url.startswith("https://") or any(c.isspace() for c in url)):
        raise ValueError("La URL pública opcional debe usar HTTPS.")
    if type(result["backups"]) is not bool:
        raise ValueError("backups debe ser true o false.")
    if type(result["filebrowser"]) is not bool:
        raise ValueError("filebrowser debe ser true o false.")
    for field in ("mega_email", "mega_password"):
        safe_text(result[field], field)
    if result["backups"] and (not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", result["mega_email"])
                              or not result["mega_password"]):
        result["backups"] = False
        print("MEGA incompleto: se omite la activación de backups.", file=sys.stderr)
    return result


def prompt():
    if not sys.stdin.isatty():
        raise ValueError("Ejecutá el instalador en una terminal o usá --config con un archivo privado.")
    service = input("Nombre del servicio del bot [servicio-historias]: ").strip() or "servicio-historias"
    default = os.environ.get("SUDO_USER", "debian")
    if default == "root":
        default = "debian"
    user = input(f"Usuario Linux existente [{default}]: ").strip() or default
    token = getpass.getpass("Token de Telegram de BotFather (oculto): ").strip()
    chat = input("Tu Chat ID personal de Telegram: ").strip()
    timezone = ARGENTINA_TIMEZONE
    print("Zona horaria del servidor y del bot: Argentina (Buenos Aires).")
    panel_url = input("URL HTTPS pública del gestor (opcional, Enter para omitir): ").strip()
    backups = input("¿Configurar backups cifrados en MEGA? [s/N]: ").strip().lower() in {"s", "si", "sí"}
    email = password = ""
    if backups:
        email = input("Correo de MEGA (Enter para omitir backups): ").strip()
        if email:
            password = getpass.getpass("Contraseña de MEGA (oculta; Enter para omitir): ")
    filebrowser = input("¿Instalar FileBrowser Quantum (gestor de archivos local)? [s/N]: ").strip().lower() in {"s", "si", "sí"}
    return validate(dict(service=service, user=user, telegram_token=token, telegram_chat_id=chat,
                         timezone=timezone, panel_url=panel_url, backups=backups,
                         mega_email=email, mega_password=password, filebrowser=filebrowser))


def env_file(config, project):
    account = pwd.getpwnam(config["user"])
    target = project / ".env"
    if target.exists() or target.is_symlink():
        info = target.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != account.pw_uid
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            raise ValueError("El .env existente tiene propietario o permisos inseguros.")
        print("Se conserva el .env existente; no se reemplazan sus credenciales.")
        return
    values = dict(TELEGRAM_BOT_TOKEN=config["telegram_token"], TELEGRAM_CHAT_ID=config["telegram_chat_id"],
                  HISTORYS_DIR="/historys", STORYPULSE_PANEL_URL=config["panel_url"],
                  STORYPULSE_TIMEZONE=config["timezone"])
    def quote(value):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if any("${" in value for value in values.values()):
        raise ValueError("La configuración no permite interpolación de variables.")
    atomic(target, "".join(f"{key}={quote(value)}\n" for key, value in values.items()).encode(),
           uid=account.pw_uid, gid=account.pw_gid)


def backup_config(config):
    base = Path("/etc/storypulse-backups")
    base.mkdir(mode=0o700, exist_ok=True)
    info = base.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("La carpeta privada de backups tiene permisos inseguros.")
    path = base / "config.json"
    if path.exists() or path.is_symlink():
        previous = private_read(path)
        if (set(previous) not in ({"source"}, {"source", "bot_user"})
                or previous.get("source") != "/historys"
                or previous.get("bot_user", "debian") != config["user"]):
            raise ValueError("Hay otra configuración de backups; se conserva sin modificar.")
    else:
        save(path, dict(source="/historys", bot_user=config["user"]))
    credentials = base / "credentials.json"
    if credentials.exists() or credentials.is_symlink():
        previous = private_read(credentials)
        if set(previous) != {"mega_email", "mega_password", "veracrypt_password"}:
            raise ValueError("Archivo de credenciales previo inválido; se conservó.")
        for name in ("mega_email", "mega_password", "veracrypt_password"):
            if not safe_text(previous[name], name):
                raise ValueError("Las credenciales previas están incompletas; se conservaron.")
        if (not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", previous["mega_email"])
                or len(previous["veracrypt_password"]) < 32):
            raise ValueError("Las credenciales previas necesitan revisión; no se reemplazó la clave.")
        if previous["mega_email"].casefold() != config["mega_email"].casefold():
            raise ValueError("La cuenta MEGA anterior es distinta; se conserva su configuración.")
        print("Se conservan la cuenta MEGA y la contraseña VeraCrypt existentes.")
    else:
        save(credentials, dict(mega_email=config["mega_email"], mega_password=config["mega_password"],
                               veracrypt_password=secrets.token_urlsafe(48)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("prepare", "metadata", "env", "backups"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--project", type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError("El instalador necesita root.")
    if args.action == "prepare":
        data = validate(private_read(args.input)) if args.input else prompt()
        save(args.path, data)
    else:
        data = validate(private_read(args.path))
        if args.action == "metadata":
            account = pwd.getpwnam(data["user"])
            print("\n".join((data["service"], data["user"], str(account.pw_gid), account.pw_dir,
                              "yes" if data["backups"] else "no", "yes" if data["filebrowser"] else "no")))
        elif args.action == "env":
            env_file(data, args.project)
        else:
            backup_config(data)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        print("No se pudo preparar la configuración privada. Revisá los campos y permisos.", file=sys.stderr)
        sys.exit(1)

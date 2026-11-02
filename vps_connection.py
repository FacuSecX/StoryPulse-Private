# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Conexión WinSCP compartida para las utilidades locales de sesiones.

La configuración privada se lee de vps_connection.local.json (o de --config).
Las contraseñas pueden dejarse vacías: se consultan en una entrada oculta.
Nunca se incluyen secretos en argumentos, URLs, mensajes o scripts de WinSCP.
"""
from __future__ import annotations

import csv
import getpass
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

BASE = Path(__file__).resolve().parent
CONFIG_LOCAL = "vps_connection.local.json"
LEGACY_CONFIG_LOCAL = "renovar_session_vps.local.json"
_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
_SERVICE = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.@-]{0,127}\.service")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_FINGERPRINT = re.compile(r"(?:(?:ssh-[a-zA-Z0-9-]+|ecdsa-sha2-[a-zA-Z0-9-]+) +[0-9]{2,5} +)?(?:SHA256:)?[A-Za-z0-9+/]{43}=?")
_RESERVED_SERVICES = {name + '.service' for name in ('ssh', 'sshd', 'nginx', 'apache2', 'cron',
                       'dbus', 'systemd', 'systemd-journald', 'networking', 'storypulse-backups', 'storypulse-filebrowser')}


@dataclass(frozen=True)
class Conexion:
    winscp_exe: Path
    ppk_file: Path | None
    vps_host: str
    vps_user: str
    ssh_hostkey: str
    ppk_passphrase: str = field(repr=False)
    puerto: int = 22
    remote_dir: str = ""
    service_name: str = ""
    remote_owner: str = ""
    auth_mode: str = "key"
    ssh_password: str = field(default="", repr=False)
    remote_python: str = ""


def _text(value, field_name, *, empty=True, maximum=4096):
    if (not isinstance(value, str) or len(value) > maximum
            or any(c in value for c in "\0\r\n") or (not empty and not value.strip())):
        raise RuntimeError("Revisá el campo " + field_name + " en la configuración del VPS.")
    return value


def _script_value(value: str) -> str:
    return _text(value, "parámetro WinSCP")


def comillas_winscp(value: str) -> str:
    return '"' + _script_value(value).replace('"', '""') + '"'


def _posix_path(value, label, *, empty=False):
    value = _text(value, label, empty=empty)
    if empty and not value:
        return ""
    _script_value(value)
    path = PurePosixPath(value)
    if not path.is_absolute() or str(path) == "/" or ".." in path.parts or value.startswith("//"):
        raise RuntimeError("El campo " + label + " debe ser una ruta absoluta segura.")
    return str(path)


def _host(value):
    value = _text(value, "vps_host", empty=False, maximum=253).strip()
    plain = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        return str(ipaddress.ip_address(plain))
    except ValueError:
        if ":" in plain or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", plain):
            raise RuntimeError("El servidor debe ser un hostname, una dirección IPv4 o IPv6 válida.")
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in plain.split(".")):
            raise RuntimeError("El nombre del servidor no es válido.")
        return plain


def _hostkey(value):
    value = _text(value, "ssh_hostkey", empty=False).strip()
    if not all(_FINGERPRINT.fullmatch(pin.strip()) for pin in value.split(";")):
        raise RuntimeError("Indicá una huella SSH SHA-256 verificada; no se admite aceptar cualquier servidor.")
    return value


def _local_path(value, base, label):
    value = _text(value, label, empty=False)
    value = os.path.expanduser(value)
    _script_value(value)
    path = Path(value)
    return path if path.is_absolute() else base / path


def _find_winscp(value, base):
    if value:
        candidates = [_local_path(value, base, "winscp_exe")]
    else:
        candidates = [Path(p) for p in (shutil.which("WinSCP.com"), shutil.which("WinSCP.exe")) if p]
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            parent = os.environ.get(variable)
            if parent:
                for suffix in ("WinSCP/WinSCP.com", "Programs/WinSCP/WinSCP.com"):
                    candidates.append(Path(parent) / suffix)
        candidates.append(base / "WinSCP.com")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("No se encontró WinSCP. Instalalo o indicá la ruta de WinSCP.com en winscp_exe.")


def _secret(data, field_name, env_field, default_env, *, prompt=None):
    value = _text(data.get(field_name, ""), field_name)
    env_name = _text(data.get(env_field, default_env), env_field)
    if env_name and not _ENV.fullmatch(env_name):
        raise RuntimeError("El nombre de la variable " + env_field + " no es válido.")
    if not value and env_name:
        value = _text(os.environ.get(env_name, ""), field_name)
    if not value and prompt:
        value = _text(getpass.getpass(prompt), field_name)
    return value


def cargar_conexion(base: Path = BASE, config_path: Path | str | None = None) -> Conexion:
    base = Path(base).resolve()
    if config_path is not None:
        path = Path(config_path)
        if not path.is_absolute():
            path = base / path
    else:
        path = base / CONFIG_LOCAL
        if not path.is_file() and (base / LEGACY_CONFIG_LOCAL).is_file():
            path = base / LEGACY_CONFIG_LOCAL
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise RuntimeError("Falta una configuración válida del VPS. Copiá vps_connection.example.json como vps_connection.local.json y completala.") from None
    if not isinstance(data, dict):
        raise RuntimeError("La configuración del VPS debe ser un objeto JSON.")
    allowed = {"winscp_exe", "ppk_file", "vps_host", "vps_user", "ssh_hostkey", "ppk_passphrase",
               "ppk_passphrase_env", "puerto", "remote_dir", "service_name", "remote_owner", "auth_mode",
               "password", "password_env", "remote_python"}
    if set(data) - allowed:
        raise RuntimeError("La configuración del VPS contiene campos desconocidos.")
    mode = _text(data.get("auth_mode", "key"), "auth_mode")
    if mode not in {"key", "password"}:
        raise RuntimeError("auth_mode debe ser key (llave) o password (contraseña).")
    user = _text(data.get("vps_user"), "vps_user", empty=False)
    owner = _text(data.get("remote_owner", ""), "remote_owner") or user
    if not _USER.fullmatch(user) or not _USER.fullmatch(owner):
        raise RuntimeError("El usuario SSH o propietario remoto no es válido.")
    service = _text(data.get("service_name"), "service_name", empty=False)
    if not _SERVICE.fullmatch(service) or service in _RESERVED_SERVICES:
        raise RuntimeError("Indicá en service_name el servicio del bot, terminado en .service.")
    host = _host(data.get("vps_host"))
    pin = _hostkey(data.get("ssh_hostkey"))
    port = data.get("puerto", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise RuntimeError("El puerto SSH debe estar entre 1 y 65535.")
    remote_value = _text(data.get("remote_dir", ""), "remote_dir")
    remote = _posix_path(remote_value or f"/home/{owner}/StoryPulse-Private", "remote_dir")
    python = _posix_path(data.get("remote_python", ""), "remote_python", empty=True)
    executable = _find_winscp(_text(data.get("winscp_exe", ""), "winscp_exe"), base)
    key = None; passphrase = password = ""
    if mode == "key":
        key = _local_path(data.get("ppk_file"), base, "ppk_file")
        if not key.is_file():
            raise RuntimeError("No se encontró la llave SSH indicada en ppk_file.")
        passphrase = _secret(data, "ppk_passphrase", "ppk_passphrase_env", "STORYPULSE_PPK_PASSPHRASE")
        if not passphrase:
            try:
                with key.open("r", encoding="utf-8", errors="replace") as stream:
                    header = stream.read(4096)
            except OSError:
                raise RuntimeError("No se pudo leer la llave SSH.") from None
            if re.search(r"(?im)^Encryption:\s*(?!none\s*$)\S+", header):
                passphrase = _text(getpass.getpass("Contraseña de la llave SSH (oculta): "), "ppk_passphrase")
                if not passphrase:
                    raise RuntimeError("La llave cifrada necesita su contraseña.")
    else:
        password = _secret(data, "password", "password_env", "STORYPULSE_VPS_PASSWORD", prompt="Contraseña SSH del VPS (oculta): ")
        if not password:
            raise RuntimeError("No se ingresó una contraseña SSH.")
    return Conexion(executable, key, host, user, pin, passphrase, port, remote, service, owner,
                    mode, password, python)


def _protect_directory(path: Path):
    if os.name != "nt":
        os.chmod(path, 0o700)
        return
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    try:
        identity = subprocess.run([str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
                                  capture_output=True, text=True, timeout=15, check=True)
        sid = next(value for row in csv.reader(identity.stdout.splitlines()) for value in row
                   if re.fullmatch(r"S-1-[0-9-]+", value))
        subprocess.run([str(system / "icacls.exe"), str(path), "/inheritance:r", "/grant:r",
                        "*" + sid + ":(OI)(CI)F", "/grant:r", "*S-1-5-18:(OI)(CI)F"],
                       capture_output=True, text=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError, StopIteration):
        raise RuntimeError("No se pudieron proteger los archivos temporales de autenticación. No se inició la conexión.") from None


@contextmanager
def private_tempdir(prefix="storypulse_vps_"):
    with tempfile.TemporaryDirectory(prefix=prefix) as directory:
        path = Path(directory)
        _protect_directory(path)
        yield path


def _write_secret(folder: Path, value: str):
    _protect_directory(folder)
    path = folder / "credencial.txt"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
        stream.write(_text(value, "credencial SSH"))
    return path


def crear_apertura_winscp(conexion: Conexion, folder: Path) -> str:
    host = _host(conexion.vps_host)
    host = "[" + host + "]" if ":" in host else host
    pin = _hostkey(conexion.ssh_hostkey)
    user = _text(conexion.vps_user, "vps_user", empty=False)
    if not _USER.fullmatch(user) or type(conexion.puerto) is not int or not 1 <= conexion.puerto <= 65535:
        raise RuntimeError("Usuario o puerto SSH inválido.")
    opening = f"open sftp://{host}:{conexion.puerto}/ -username={comillas_winscp(user)}"
    opening += " -hostkey=" + comillas_winscp(pin) + " -timeout=30"
    if conexion.auth_mode == "key":
        if conexion.ppk_file is None:
            raise RuntimeError("Falta la llave SSH de esta conexión.")
        opening += " -privatekey=" + comillas_winscp(str(conexion.ppk_file))
        if conexion.ppk_passphrase:
            secret = _write_secret(Path(folder), conexion.ppk_passphrase)
            opening += " -passphrase=" + comillas_winscp(str(secret)) + " -passwordsfromfiles"
    elif conexion.auth_mode == "password":
        if not conexion.ssh_password:
            raise RuntimeError("Falta la contraseña SSH de esta conexión.")
        secret = _write_secret(Path(folder), conexion.ssh_password)
        opening += " -password=" + comillas_winscp(str(secret)) + " -passwordsfromfiles"
    else:
        raise RuntimeError("Método de autenticación inválido.")
    opening += " -rawsettings AuthGSSAPI=0 TryAgent=0 ExitCode1IsError=1"
    return opening


def comando_python_remoto(conexion: Conexion, program: str) -> str:
    root = _posix_path(conexion.remote_dir, "remote_dir")
    if not isinstance(program, str) or "\0" in program:
        raise RuntimeError("El programa remoto no es válido.")
    quote = shlex.quote
    checks = " && ".join("test -f " + quote(str(PurePosixPath(root) / name))
                         for name in ("bot.py", "instagram_sessions.py", "database.py"))
    prefix = "cd -- " + quote(root) + " && " + checks + " && "
    suffix = " -B -c " + quote(program)
    if conexion.remote_python:
        selected = _posix_path(conexion.remote_python, "remote_python")
        return "(" + prefix + "test -x " + quote(selected) + " && " + quote(selected) + suffix + ")"
    candidates = [str(PurePosixPath(root) / ".venv/bin/python"), str(PurePosixPath(root) / "venv/bin/python"), "/usr/bin/python3"]
    branches = []
    for i, candidate in enumerate(candidates):
        branches.append(("if" if i == 0 else "elif") + " test -x " + quote(candidate)
                        + "; then STORYPULSE_SESSION_PYTHON=" + quote(candidate) + "; ")
    return "(" + prefix + "".join(branches) + 'else false; fi && "$STORYPULSE_SESSION_PYTHON"' + suffix + ")"


def ejecutar_winscp(conexion: Conexion, lineas, *, base: Path = BASE, timeout=180):
    with private_tempdir() as folder:
        opening = crear_apertura_winscp(conexion, folder)
        lines = list(lineas)
        if any(not isinstance(line, str) or "\0" in line or "\r" in line or "\n" in line for line in lines):
            raise RuntimeError("Una instrucción de transferencia no es válida.")
        script = folder / "conexion.txt"
        env = dict(os.environ)
        bound = []
        prefix = "STORYPULSE_SCRIPT_" + uuid.uuid4().hex.upper() + "_"
        for index, command in enumerate(["option batch abort", "option confirm off", opening, *lines, "exit"]):
            pieces = []
            for part_index, start in enumerate(range(0, len(command), 8192)):
                name = f"{prefix}{index}_{part_index}"
                env[name] = command[start:start + 8192]
                pieces.append("%" + name + "%")
            bound.append("".join(pieces))
        script.write_text("\n".join(bound) + "\n", encoding="utf-8-sig")
        args = [str(conexion.winscp_exe)]
        if conexion.winscp_exe.suffix.lower() == ".exe":
            args.append("/console")
        args.extend(["/ini=nul", "/nointeractiveinput", f"/script={script}"])
        try:
            return subprocess.run(args, cwd=str(base), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise RuntimeError("La conexión al VPS agotó el tiempo; verificá su estado antes de reintentar.") from None
        except OSError:
            raise RuntimeError("No se pudo iniciar WinSCP. Revisá la ruta configurada.") from None


def redactar_salida(salida: str, conexion: Conexion | None = None) -> str:
    """Return fixed diagnostics, never cookies, shell payloads or client output."""
    lower = str(salida).casefold()
    if any(word in lower for word in ("host key", "hostkey", "fingerprint", "huella")):
        return "WinSCP no confirmó la identidad del servidor. Revisá ssh_hostkey."
    if any(word in lower for word in ("authentication failed", "access denied", "autenticación", "autenticacion")):
        return "WinSCP rechazó la autenticación. Revisá el método y las credenciales."
    if any(word in lower for word in ("timed out", "timeout", "connection refused", "network error")):
        return "No se pudo establecer la conexión. Revisá servidor, puerto y conectividad."
    return "El VPS no confirmó la operación. Revisá la ruta del proyecto, el intérprete y los permisos del servicio."

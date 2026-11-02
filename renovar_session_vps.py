#!/usr/bin/env python3
# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Renovar una cuenta manualmente y desplegar sólo su JSON mediante WinSCP."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath

BASE = Path(__file__).resolve().parent
from vps_connection import (
    Conexion, cargar_conexion, comillas_winscp,
    comando_python_remoto, ejecutar_winscp,
)

CONFIG_LOCAL = "vps_connection.local.json"


@dataclass(frozen=True)
class SesionLocal:
    archivo: Path
    id: str
    username: str
    usuario_id: str | None = None


@dataclass(frozen=True)
class PerfilLocal:
    carpeta: Path
    navegador: str
    username: str


def leer_json(ruta: Path):
    try:
        return json.loads(ruta.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"No se pudo leer {ruta.name} como JSON.") from exc


def id_instagram(ruta: Path) -> str | None:
    try:
        data = leer_json(ruta)
        cookies = data.get("cookies", []) if isinstance(data, dict) else []
        if not isinstance(cookies, list):
            return None
        return next((str(c["value"]) for c in cookies if isinstance(c, dict)
                     and c.get("name") == "ds_user_id" and str(c.get("value", "")).isdigit()
                     and str(c.get("domain", "")).lstrip(".").lower() in
                     ("instagram.com", "www.instagram.com")), None)
    except RuntimeError:
        return None


def listar_sesiones(base: Path = BASE) -> list[SesionLocal]:
    carpeta = base / "sesiones_instagram"
    configuradas = {}
    config_path = base / "sesiones_instagram.json"
    if config_path.is_file():
        config = leer_json(config_path)
        if not isinstance(config, dict) or not isinstance(config.get("sesiones"), list):
            raise RuntimeError("sesiones_instagram.json no tiene una lista de sesiones válida.")
        for fila in config["sesiones"]:
            if isinstance(fila, dict) and isinstance(fila.get("archivo"), str):
                ruta = Path(fila["archivo"])
                ruta = ruta if ruta.is_absolute() else base / ruta
                configuradas[os.path.normcase(str(ruta.resolve()))] = fila
    resultado = []
    for ruta in sorted(carpeta.glob("*.json"), key=lambda p: p.name.casefold()):
        if not ruta.is_file() or not ruta.resolve().is_relative_to(carpeta.resolve()):
            continue
        if not re.fullmatch(r"[a-zA-Z0-9._]{1,30}", ruta.stem):
            continue
        fila = configuradas.get(os.path.normcase(str(ruta.resolve())), {})
        identificador = str(fila.get("id") or ruta.stem).lower()
        username = str(fila.get("username") or ruta.stem).lstrip("@").lower()
        if not re.fullmatch(r"[a-z0-9._]{1,30}", identificador) or not re.fullmatch(r"[a-z0-9._]{1,30}", username):
            raise RuntimeError(f"La cuenta configurada para {ruta.name} tiene un nombre inválido.")
        resultado.append(SesionLocal(ruta.resolve(), identificador, username, id_instagram(ruta)))
    return resultado


def listar_perfiles(base: Path = BASE) -> list[PerfilLocal]:
    carpeta = base / "perfiles_instagram"
    resultado = []
    if not carpeta.is_dir():
        return resultado
    for ruta in sorted(carpeta.iterdir(), key=lambda p: p.name.casefold()):
        if not ruta.is_dir() or not ruta.resolve().is_relative_to(carpeta.resolve()):
            continue
        nombre = ruta.name.casefold()
        navegador = "edge"
        for sufijo, motor in (("_chromium", "chromium"), ("_chrome", "chrome"),
                             ("_msedge", "edge"), ("_edge", "edge")):
            if nombre.endswith(sufijo):
                nombre = nombre[:-len(sufijo)]
                navegador = motor
                break
        resultado.append(PerfilLocal(ruta.resolve(), navegador, nombre))
    return resultado


def candidatos_perfil(sesion: SesionLocal, perfiles: list[PerfilLocal]) -> tuple[list[PerfilLocal], bool]:
    nombres = {sesion.username, sesion.id, sesion.archivo.stem.casefold()}
    exactos = [p for p in perfiles if p.username in nombres]
    if exactos:
        return exactos, True
    ordenados = sorted(perfiles, key=lambda p: max(SequenceMatcher(None, n, p.username).ratio()
                                                 for n in nombres), reverse=True)
    return ordenados, False


def elegir(opciones, describir, titulo):
    if not opciones:
        raise RuntimeError(f"No hay opciones disponibles: {titulo}.")
    print(f"\n{titulo}")
    for numero, opcion in enumerate(opciones, 1):
        print(f"  {numero}. {describir(opcion)}")
    print("  0. Cancelar")
    while True:
        valor = input("Elegí un número: ").strip()
        if valor == "0":
            raise KeyboardInterrupt
        if valor.isdigit() and 1 <= int(valor) <= len(opciones):
            return opciones[int(valor) - 1]
        print("Ingresá uno de los números de la lista.")


def elegir_perfil(sesion, perfiles):
    candidatos, exacto = candidatos_perfil(sesion, perfiles)
    if exacto and len(candidatos) == 1:
        return candidatos[0]
    titulo = "Elegí el perfil de navegador para esta sesión"
    if not exacto:
        print("No hay una coincidencia exacta. Los perfiles se muestran por similitud; elegí el correcto.")
    return elegir(candidatos, lambda p: f"{p.carpeta.name} ({p.navegador})", titulo)


def validar_sesion(ruta: Path, usuario_id: str | None = None) -> None:
    data = leer_json(ruta)
    cookies = data.get("cookies", []) if isinstance(data, dict) else []
    cookies = cookies if isinstance(cookies, list) else []
    cookie = next((c for c in cookies if isinstance(c, dict) and c.get("name") == "sessionid"
                   and c.get("value") and str(c.get("domain", "")).lstrip(".").lower() in
                   ("instagram.com", "www.instagram.com")), None)
    if cookie is None:
        raise RuntimeError(f"{ruta.name} no contiene una sesión de Instagram válida. No se subirá.")
    vencimiento = cookie.get("expires", -1)
    if isinstance(vencimiento, (int, float)) and 0 < vencimiento <= time.time():
        raise RuntimeError("La sesión recién exportada ya está vencida. No se subirá.")
    if usuario_id and id_instagram(ruta) != usuario_id:
        raise RuntimeError("La sesión exportada pertenece a otra cuenta. No se subirá.")


def huella_archivo(ruta: Path):
    return ruta.stat().st_mtime_ns, hashlib.sha256(ruta.read_bytes()).hexdigest()


def renovar_local(sesion: SesionLocal, perfil: PerfilLocal, base: Path = BASE, espera: int = 600) -> None:
    anterior = huella_archivo(sesion.archivo)
    backup = base / ".renovacion_backup" / sesion.archivo.name
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(sesion.archivo, backup)
    comando = [sys.executable, str(base / "crear_session.py"), "--cuenta", sesion.id,
               "--navegador", perfil.navegador, "--perfil", str(perfil.carpeta),
               "--archivo-sesion", str(sesion.archivo), "--espera", str(espera),
               "--subida-automatica"]
    if sesion.usuario_id:
        comando.extend(["--usuario-id", sesion.usuario_id])
    print(f"\nAbriendo {perfil.carpeta.name}. Iniciá sesión y completá la verificación manualmente.")
    resultado = subprocess.run(comando, cwd=str(base))
    if resultado.returncode != 0:
        raise RuntimeError("No terminó la renovación local. El VPS no fue modificado.")
    if huella_archivo(sesion.archivo) == anterior:
        raise RuntimeError("El JSON no fue regenerado en esta ejecución. El VPS no fue modificado.")
    validar_sesion(sesion.archivo, sesion.usuario_id)
    print(f"Sesión local validada: {sesion.archivo.name}. Continúa la subida automática.")


REGISTRO_REMOTO = r'''
def preparar_registro_remoto(destino, identificador, username):
    import json, sys
    from pathlib import Path
    base = destino.parent.parent.resolve()
    sys.path.insert(0, str(base))
    import instagram_sessions as sesiones
    ruta = sesiones.ruta_configuracion()
    existentes = sesiones.listar_sesiones(incluir_deshabilitadas=True)
    for anterior in existentes:
        mismo_archivo = anterior.archivo.resolve() == destino.resolve()
        if anterior.id == identificador and not mismo_archivo:
            raise RuntimeError('Ese ID ya está registrado con otro archivo en el VPS.')
        if mismo_archivo and anterior.id != identificador:
            raise RuntimeError('Ese archivo ya está registrado con otro ID en el VPS.')
    config = json.loads(ruta.read_text(encoding='utf-8-sig')) if ruta.exists() else {
        'version': 1, 'sesiones': [{'id': 'principal', 'username': None, 'archivo': None, 'habilitada': True}]}
    fila = {'id': identificador, 'username': username,
            'archivo': destino.resolve().relative_to(base).as_posix(), 'habilitada': True}
    for indice, anterior in enumerate(config['sesiones']):
        if anterior['id'].strip().lower() == identificador:
            config['sesiones'][indice] = {**anterior, **fila}
            break
    else:
        config['sesiones'].append(fila)
    return sesiones, ruta, config
'''


DEPLOY_REMOTO = REGISTRO_REMOTO + r'''
import hashlib, json, os, pwd, shutil, subprocess, time
from pathlib import Path
d = json.loads(__import__("base64").b64decode(DATOS))
temporal, destino, respaldo = (Path(d[k]) for k in ("temporal", "destino", "respaldo"))
cuenta = pwd.getpwnam(d["owner"])
prefijo = [] if os.geteuid() == 0 else ["sudo", "-n"]
def servicio(accion, comprobar=True):
    argumentos = ["systemctl", accion]
    if accion == "is-active": argumentos.append("--quiet")
    return subprocess.run(prefijo + argumentos + [d["servicio"]], check=comprobar)
detenido = instalado = correcto = anterior = False
config_path = config_backup = None
config_existia = config_instalada = False
try:
    contenido = temporal.read_bytes()
    if hashlib.sha256(contenido).hexdigest() != d["sha256"]:
        raise RuntimeError("El JSON transferido no coincide con el archivo local.")
    estado = json.loads(contenido)
    cookies = estado.get("cookies", []) if isinstance(estado, dict) else []
    if not isinstance(cookies, list): cookies = []
    ig = [c for c in cookies if isinstance(c, dict) and str(c.get("domain", "")).lstrip(".").lower() in ("instagram.com", "www.instagram.com")]
    cookie = next((c for c in ig if c.get("name") == "sessionid" and c.get("value")), None)
    if cookie is None: raise RuntimeError("La transferencia no contiene sessionid de Instagram.")
    if isinstance(cookie.get("expires"), (int, float)) and 0 < cookie["expires"] <= time.time():
        raise RuntimeError("La sesión recibida está vencida.")
    uid = next((str(c.get("value")) for c in ig if c.get("name") == "ds_user_id"), None)
    if d["usuario_id"] and uid != d["usuario_id"]: raise RuntimeError("La transferencia pertenece a otra cuenta.")
    if d.get("nueva") and (destino.exists() or destino.is_symlink()):
        raise RuntimeError("El JSON de esa cuenta ya existe en el VPS. Usá RENOVAR_SESSION.bat.")
    os.chown(temporal, cuenta.pw_uid, cuenta.pw_gid)
    os.chmod(temporal, 0o600)
    os.chown(destino.parent, cuenta.pw_uid, cuenta.pw_gid)
    os.chmod(destino.parent, destino.parent.stat().st_mode | 0o700)
    servicio("stop")
    detenido = True
    if d.get("registrar"):
        sesiones, config_path, config = preparar_registro_remoto(destino, d['sesion_id'], d['username'])
        config_existia = config_path.is_file()
        config_backup = config_path.parent / ('.' + config_path.name + respaldo.name + '.tmp')
        if config_existia:
            shutil.copy2(config_path, config_backup)
            os.chmod(config_backup, 0o600)
    anterior = destino.is_file()
    if anterior:
        shutil.copy2(destino, respaldo)
        os.chown(respaldo, cuenta.pw_uid, cuenta.pw_gid)
        os.chmod(respaldo, 0o600)
    os.replace(temporal, destino)
    instalado = True
    if d.get("registrar"):
        config_instalada = True
        sesiones.escribir_json_atomico(config_path, config)
        os.chown(config_path, cuenta.pw_uid, cuenta.pw_gid)
        os.chmod(config_path, 0o600)
        registrada = next((s for s in sesiones.listar_sesiones() if s.id == d['sesion_id']), None)
        if registrada is None or registrada.archivo.resolve() != destino.resolve():
            raise RuntimeError('No se confirmó el registro de la sesión nueva en el VPS.')
    servicio("restart")
    time.sleep(3)
    servicio("is-active")
    correcto = True
except BaseException:
    if detenido: servicio("stop", comprobar=False)
    if config_instalada:
        if config_existia and config_backup.is_file():
            os.replace(config_backup, config_path)
        elif not config_existia:
            config_path.unlink(missing_ok=True)
    if instalado and anterior and respaldo.is_file():
        os.replace(respaldo, destino)
    elif instalado and not anterior:
        destino.unlink(missing_ok=True)
    if detenido: servicio("restart", comprobar=False)
    raise
finally:
    temporal.unlink(missing_ok=True)
    if correcto: respaldo.unlink(missing_ok=True)
    if correcto and config_backup is not None: config_backup.unlink(missing_ok=True)
if correcto: print(d["marca"], flush=True)
'''


def crear_script_transferencia(conexion: Conexion, sesion: SesionLocal, frase_file: Path | None, identificador: str,
                               *, nueva: bool = False, registrar: bool = False):
    carpeta = PurePosixPath(conexion.remote_dir) / "sesiones_instagram"
    destino = carpeta / sesion.archivo.name
    temporal = carpeta / f".{sesion.archivo.name}.renovar.{identificador}.tmp"
    respaldo = carpeta / f".{sesion.archivo.name}.antes.{identificador}.tmp"
    marca = f"STORYPULSE_RENOVADA_{identificador}"
    datos = {"temporal": str(temporal), "destino": str(destino), "respaldo": str(respaldo),
             "owner": conexion.remote_owner, "servicio": conexion.service_name,
             "sha256": huella_archivo(sesion.archivo)[1], "usuario_id": sesion.usuario_id, "marca": marca}
    if nueva:
        datos["nueva"] = True
    if registrar:
        datos.update(registrar=True, sesion_id=sesion.id, username=sesion.username)
    payload = base64.b64encode(json.dumps(datos).encode()).decode("ascii")
    codigo = "DATOS=" + repr(payload) + "\n" + DEPLOY_REMOTO
    fuente = base64.b64encode(codigo.encode()).decode("ascii")
    comando = comando_python_remoto(conexion, "import base64;exec(base64.b64decode(" + repr(fuente) + "))")
    preparar = "from pathlib import Path; p=Path(" + repr(str(carpeta)) + "); "
    preparar += "assert not p.is_symlink(), 'La carpeta de sesiones no puede ser un enlace'; "
    preparar += "p.mkdir(mode=0o700, parents=False, exist_ok=True)"
    lineas = ["call " + comando_python_remoto(conexion, preparar),
              f"put -transfer=binary -resumesupport=off -permissions=600 {comillas_winscp(str(sesion.archivo))} {comillas_winscp(str(temporal))}",
              "call " + comando]
    return "\n".join(lineas) + "\n", marca


def transferir_al_vps(conexion: Conexion, sesion: SesionLocal, base: Path = BASE,
                     *, nueva: bool = False, registrar: bool = False) -> None:
    validar_sesion(sesion.archivo, sesion.usuario_id)
    script, marca = crear_script_transferencia(conexion, sesion, None, uuid.uuid4().hex,
                                               nueva=nueva, registrar=registrar)
    print(f"\nSubiendo {sesion.archivo.name} y comprobando {conexion.service_name}...")
    try:
        resultado = ejecutar_winscp(conexion, script.splitlines(), base=base, timeout=180)
    except subprocess.TimeoutExpired:
        raise RuntimeError("La conexión al VPS agotó el tiempo. Revisá el servicio antes de reintentar.") from None
    salida = (resultado.stdout or "") + "\n" + (resultado.stderr or "")
    confirmado = any(linea.strip() == marca for linea in salida.splitlines())
    if resultado.returncode != 0 or not confirmado:
        raise RuntimeError("No se confirmó la actualización del VPS. Revisá la conexión, la ruta remota, "
                           "el nombre del servicio y los permisos de sudo. El JSON quedó guardado localmente.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Renovar una de las sesiones y subirla al VPS.")
    modos = parser.add_mutually_exclusive_group()
    modos.add_argument("--listar", action="store_true", help="Mostrar JSON y perfiles sin abrir navegador ni conectar al VPS.")
    modos.add_argument("--solo-subir", action="store_true",
                       help="Subir el JSON local elegido y reiniciar el servicio, sin volver a abrir Instagram.")
    parser.add_argument("--cuenta", help="Elegir directamente una cuenta o el nombre de su JSON.")
    parser.add_argument("--espera", type=int, default=600, help="Segundos para completar el inicio de sesión manual.")
    parser.add_argument("--config", type=Path, help="Archivo privado de conexión compartido; por defecto vps_connection.local.json.")
    args = parser.parse_args(argv)
    if args.espera <= 0: parser.error("--espera debe ser mayor que cero.")
    sesiones = listar_sesiones(BASE)
    perfiles = listar_perfiles(BASE)
    print("=" * 68)
    print("STORYPULSE - RENOVAR UNA SESIÓN Y ACTUALIZAR EL VPS")
    print("=" * 68)
    if not sesiones:
        raise RuntimeError("No hay JSON de sesiones en la carpeta sesiones_instagram.")
    if args.listar:
        for numero, sesion in enumerate(sesiones, 1):
            opciones, exacto = candidatos_perfil(sesion, perfiles)
            nombres = ", ".join(p.carpeta.name for p in opciones) if exacto else "elegir por similitud"
            print(f"{numero}. {sesion.archivo.name} -> {nombres}")
        return 0
    if args.cuenta:
        nombre = args.cuenta.strip().lstrip("@").removesuffix(".json").casefold()
        coincidencias = [s for s in sesiones if nombre in (s.id, s.username, s.archivo.stem.casefold())]
        if len(coincidencias) != 1:
            raise RuntimeError("La cuenta indicada no identifica un único JSON de la lista.")
        sesion = coincidencias[0]
    else:
        titulo = "¿Qué sesión guardada deseás subir?" if args.solo_subir else "¿Qué sesión deseás renovar?"
        sesion = elegir(sesiones, lambda s: s.archivo.name, titulo)
    conexion = cargar_conexion(BASE, args.config)
    print(f"\nSesión: {sesion.archivo.name}")
    print(f"Destino: {conexion.remote_dir}/sesiones_instagram/{sesion.archivo.name}")
    if args.solo_subir:
        print("Se usará el JSON ya guardado localmente.")
    else:
        perfil = elegir_perfil(sesion, perfiles)
        print(f"Perfil: {perfil.carpeta.name} ({perfil.navegador})")
        renovar_local(sesion, perfil, BASE, args.espera)
    try:
        transferir_al_vps(conexion, sesion, BASE)
    except (RuntimeError, OSError):
        print("\nPodés reintentar la subida de esta sesión sin volver a iniciar sesión:")
        if args.config:
            print("Reejecutá el mismo comando conservando --config y agregá:")
            print(f"--solo-subir --cuenta {sesion.archivo.stem}")
        else:
            print(f"RENOVAR_SESSION.bat --solo-subir --cuenta {sesion.archivo.stem}")
        raise
    print(f"\nSESIÓN ACTUALIZADA: {sesion.archivo.name}")
    print(f"JSON reemplazado: {conexion.remote_dir}/sesiones_instagram/{sesion.archivo.name}")
    print(f"Servicio reiniciado y comprobado: {conexion.service_name} = active")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nRenovación cancelada.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nPROCESO INTERRUMPIDO: {exc}")
        sys.exit(1)

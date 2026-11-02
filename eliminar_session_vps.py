# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Eliminar una sesión local y del VPS usando la conexión WinSCP existente."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from eliminar_session_core import Proyecto, guardar_json, leer_json, ruta_segura
from renovar_session_vps import cargar_conexion, elegir
from vps_connection import comando_python_remoto, ejecutar_winscp

BASE = Path(__file__).resolve().parent

REMOTO = r'''
import base64, json, os, re, subprocess, sys, time
from pathlib import Path
payload = json.loads(base64.b64decode(PAYLOAD))
try:
    base = Path(payload['base']).resolve()
    sys.path.insert(0, str(base))
    import instagram_sessions as sesiones
    import database as database
    proyecto = Proyecto(base, sesiones.ruta_configuracion(), sesiones.ruta_registro(),
        sesiones._ruta_env('INSTAGRAM_STORAGE_STATE', 'instagram_state.json'), database.DB_FILE)
    accion = payload['accion']
    servicio = payload['servicio']
    if not re.fullmatch(r'[a-zA-Z0-9_.@-]+\.service', servicio):
        raise RuntimeError('El nombre del servicio es inválido.')
    prefijo = [] if os.geteuid() == 0 else ['sudo', '-n']
    def systemctl(*args):
        r = subprocess.run(prefijo + ['systemctl'] + list(args) + [servicio],
            capture_output=True, text=True, timeout=100)
        if r.returncode:
            raise RuntimeError('No se pudo completar la operación del servicio: ' + args[0])
        return r.stdout.strip()
    def activo():
        return systemctl('show', '--property=ActiveState', '--value')
    def detener():
        systemctl('stop')
        if activo() not in ('inactive', 'failed'):
            raise RuntimeError('El servicio no se detuvo completamente.')
    def iniciar():
        systemctl('start')
        time.sleep(3)
        if activo() != 'active':
            raise RuntimeError('El servicio no quedó activo.')
    if accion == 'inventario':
        resultado = {'sesiones': proyecto.inventario(), 'pendientes': proyecto.pendientes(),
            'controles': [p.stem.removeprefix('control-') for p in proyecto.root.glob('control-*.json')],
            'servicio': activo()}
    elif accion == 'resumen':
        resultado = proyecto.resumen(payload['id'])
    else:
        txid = payload['tx']
        proyecto.tx(txid)  # Validar antes de construir cualquier ruta de control.
        control = ruta_segura(base, proyecto.root / ('control-' + txid + '.json'))
        if accion == 'preparar':
            preview = proyecto.resumen(payload['id'])
            if not preview['presente']:
                resultado = {'fase': 'ausente'}
            else:
                if proyecto.pendientes() and txid not in proyecto.pendientes():
                    raise RuntimeError('Hay otra eliminación pendiente en el VPS.')
                if control.exists():
                    info = leer_json(control)
                    if info['id'] != payload['id']:
                        raise RuntimeError('La operación pendiente pertenece a otra sesión.')
                else:
                    if preview['token'] != payload['token']:
                        raise RuntimeError('El VPS cambió desde la confirmación; volvé a ejecutar el BAT.')
                    if systemctl('show', '--property=LoadState', '--value') != 'loaded':
                        raise RuntimeError('El servicio no está instalado.')
                    info = {'id': payload['id'], 'activa_antes': activo() == 'active'}
                    proyecto.crear_root()
                    guardar_json(control, info)  # Durable incluso si se corta SSH al detener el bot.
                detener()
                state = proyecto.preparar(payload['id'], txid, payload['token'], info)
                resultado = {'fase': state['fase']}
        elif accion == 'aplicar':
            state = proyecto.estado(txid)
            if state['fase'] == 'ausente' and not control.exists():
                resultado = {'fase': 'ausente'}
            else:
                detener()
                state = proyecto.aplicar(txid)
                resultado = {'fase': state['fase']}
        elif accion == 'verificar':
            state = proyecto.estado(txid)
            resultado = {'fase': state['fase']}
            if state['fase'] != 'ausente':
                resultado['resumen'] = proyecto.verificar(txid)
        elif accion == 'activar':
            if control.exists():
                info = leer_json(control)
                if info['activa_antes']:
                    iniciar()
            resultado = {'servicio': activo()}
        elif accion == 'restaurar':
            state = proyecto.estado(txid)
            if control.exists() or state['fase'] != 'ausente':
                info = leer_json(control) if control.exists() else state.get('extra', {})
                detener()
                proyecto.restaurar(txid)
                if info.get('activa_antes'):
                    iniciar()
                proyecto.finalizar(txid, restaurada=True)
                control.unlink(missing_ok=True)
            resultado = {'fase': 'restaurada', 'servicio': activo()}
        elif accion == 'finalizar':
            proyecto.finalizar(txid)
            control.unlink(missing_ok=True)
            resultado = {'fase': 'confirmada', 'servicio': activo()}
        else:
            raise RuntimeError('Operación desconocida.')
    respuesta = {'ok': True, 'resultado': resultado}
except Exception as error:
    # No devolver traceback ni respuestas de Instagram o contenidos de archivos.
    respuesta = {'ok': False, 'error': str(error) if isinstance(error, RuntimeError)
        else 'La operación no pudo completarse (' + type(error).__name__ + ').'}
print('SP_ELIMINAR_JSON:' + base64.b64encode(json.dumps(respuesta, ensure_ascii=False).encode()).decode(), flush=True)
'''


class VPS:
    def __init__(self, conexion, base=BASE):
        self.conexion = conexion
        self.base = Path(base)
        self.core = (self.base / "eliminar_session_core.py").read_text(encoding="utf-8-sig")
        target = {name: getattr(conexion, name) for name in
                  ("vps_host", "puerto", "vps_user", "ssh_hostkey", "remote_dir", "service_name", "remote_owner")}
        self.identidad = hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()

    def pedir(self, accion, **params):
        c = self.conexion
        datos = {"base": c.remote_dir, "servicio": c.service_name, "accion": accion, **params}
        payload = base64.b64encode(json.dumps(datos).encode()).decode()
        source = self.core + "\nPAYLOAD=" + repr(payload) + "\n" + REMOTO
        encoded = base64.b64encode(source.encode()).decode()
        program = "import base64;exec(base64.b64decode(" + repr(encoded) + "))"
        command = comando_python_remoto(c, program)
        try:
            r = ejecutar_winscp(c, ["call " + command], base=self.base, timeout=180)
        except subprocess.TimeoutExpired:
            raise RuntimeError("La conexión agotó el tiempo. La operación se recuperará al ejecutar otra vez el BAT.") from None
        lines = (r.stdout or "").splitlines()
        encoded_result = next((line.strip().split(":", 1)[1] for line in reversed(lines)
                               if line.strip().startswith("SP_ELIMINAR_JSON:")), None)
        if encoded_result is None:
            raise RuntimeError("No se recibió confirmación del VPS. No se mostrará la eliminación como exitosa.")
        result = json.loads(base64.b64decode(encoded_result))
        if not result.get("ok"):
            raise RuntimeError(result.get("error", "La operación del VPS falló."))
        if r.returncode:
            raise RuntimeError("WinSCP no confirmó el cierre correcto de la operación; debe recuperarse.")
        return result["resultado"]


def proyecto_local(base=BASE):
    import instagram_sessions as sesiones
    import database as database
    return Proyecto(base, sesiones.ruta_configuracion(), sesiones.ruta_registro(),
        sesiones._ruta_env("INSTAGRAM_STORAGE_STATE", "instagram_state.json"), database.DB_FILE)


def comprobar_local_en_reposo(base=BASE):
    if os.name != "nt":
        return
    script = r'''$busy = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.Name -match '^(python|pythonw|py)(\.exe)?$' -and $_.CommandLine -match
        '(?i)(^|[\s"\\/])(bot|crear_session|crear_session_vps|renovar_session_vps)\.py([\s"]|$)'
    }); if ($busy.Count -gt 0) { exit 10 }'''
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, timeout=30)
    if r.returncode == 10:
        raise RuntimeError("Hay un bot local o una creación/renovación de sesión abierta. Cerrala antes de eliminar.")
    if r.returncode:
        raise RuntimeError("No se pudo comprobar que el proyecto local esté en reposo.")


def unir_inventarios(locales, remotas):
    merged = {}
    for location, rows in (("local", locales), ("vps", remotas)):
        for row in rows:
            merged.setdefault(row["id"], {"id": row["id"]})[location] = row
    for row in merged.values():
        a, b = row.get("local"), row.get("vps")
        if a and b:
            for key in ("uid", "username"):
                if a.get(key) and b.get(key) and a[key] != b[key]:
                    row["conflicto"] = True
    return sorted(merged.values(), key=lambda r: r["id"])


def describir(row):
    data = row.get("local") or row["vps"]
    name = "@" + data["username"] if data.get("username") else row["id"]
    where = ", ".join(label for key, label in (("local", "local"), ("vps", "VPS")) if key in row)
    suffix = " - CONFLICTO: requiere revisión" if row.get("conflicto") else ""
    return f"{name} [{row['id']}] ({where}){suffix}"


def mostrar_resumen(location, preview):
    print(f"\n{location}:")
    if not preview["presente"]:
        print("  La sesión no está registrada y no tiene referencias.")
        return
    data = preview["resumen"]
    print(f"  Se retirará el archivo: {preview['target']['archivo'] or 'sin archivo; sólo referencias'}")
    print(f"  Respaldos: {data['respaldos']} perfiles. Preferida: {len(data['preferidas'])} perfiles.")
    print(f"  Perfiles que quedarán sin acceso: {len(data['sin_acceso'])}.")
    print(f"  Programaciones a actualizar: {data['programaciones_actualizadas']}; a pausar: {len(data['programaciones_pausadas'])}.")
    print(f"  Perfiles de navegador exclusivos a retirar: {len(data['perfiles_navegador'])}.")
    if data["perfiles_compartidos_conservados"]:
        print("  Se conservarán los perfiles de navegador compartidos o ambiguos.")
    for row in data["preferidas"]:
        print(f"  @{row['perfil']}: nueva preferida = {row['nueva'] or 'sin sesión disponible'}")


def recuperar(proyecto, remoto, archivo):
    data = leer_json(archivo)
    if not data.get("destino") or data["destino"] != remoto.identidad:
        raise RuntimeError("El diario pertenece a otra conexión o a una versión anterior. "
                           "Se conservó sin actuar: usá la configuración y la utilidad originales para recuperarlo.")
    txid = data["tx"]
    proyecto.tx(txid)
    if data["decision"] == "confirmar":
        remoto.pedir("activar", tx=txid)
        remoto.pedir("finalizar", tx=txid)
        proyecto.finalizar(txid)
    else:
        remoto.pedir("restaurar", tx=txid)
        proyecto.restaurar(txid)
        proyecto.finalizar(txid, restaurada=True)
    archivo.unlink(missing_ok=True)
    return data["decision"]


def eliminar(proyecto, remoto, ident, previews, comprobar_reposo=lambda: None):
    txid = uuid.uuid4().hex
    proyecto.crear_root()
    archivo = ruta_segura(proyecto.base, proyecto.root / ("coord-" + txid + ".json"))
    data = {"tx": txid, "id": ident, "decision": "restaurar", "destino": remoto.identidad}
    guardar_json(archivo, data)
    try:
        comprobar_reposo()
        print("\nPreparando recuperación y deteniendo temporalmente el bot del VPS...")
        remoto.pedir("preparar", id=ident, tx=txid, token=previews["vps"]["token"])
        if previews["local"]["presente"]:
            proyecto.preparar(ident, txid, previews["local"]["token"])
        comprobar_reposo()
        print("Retirando la sesión y limpiando vínculos y programaciones...")
        remoto.pedir("aplicar", tx=txid)
        if previews["local"]["presente"]:
            proyecto.aplicar(txid)
        remoto.pedir("verificar", tx=txid)
        if previews["local"]["presente"]:
            proyecto.verificar(txid)
        print("Comprobando el servicio...")
        estado_servicio = remoto.pedir("activar", tx=txid)
        data["decision"] = "confirmar"
        guardar_json(archivo, data)
        remoto.pedir("finalizar", tx=txid)
        proyecto.finalizar(txid)
        archivo.unlink()
        return estado_servicio
    except BaseException:
        print("La operación no terminó. Intentando recuperar el estado anterior...")
        try:
            decision = recuperar(proyecto, remoto, archivo)
            print("Se restauró el estado anterior; la sesión se conserva." if decision != "confirmar" else
                  "Eliminación confirmada; terminó la limpieza de respaldos temporales.")
        except BaseException:
            print("RECUPERACIÓN PENDIENTE. Se conservaron el diario y las copias temporales.")
            print("Ejecutá nuevamente ELIMINAR_SESSION.bat conservando el mismo --config, si lo usaste, para completar la recuperación.")
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Eliminar una sesión local, del VPS y de sus respaldos.")
    parser.add_argument("--cuenta", help="ID, username o nombre del JSON. Siempre se pide confirmación.")
    modos = parser.add_mutually_exclusive_group()
    modos.add_argument("--listar", action="store_true", help="Consultar sesiones locales y remotas sin modificar nada.")
    modos.add_argument("--simular", action="store_true", help="Mostrar qué se eliminaría, sin modificar nada.")
    parser.add_argument("--config", type=Path, help="Archivo privado de conexión compartido; por defecto vps_connection.local.json.")
    args = parser.parse_args(argv)
    proyecto = proyecto_local()
    remoto = VPS(cargar_conexion(BASE, args.config))
    comprobar_local_en_reposo()
    journals = sorted(proyecto.root.glob("coord-*.json"))
    if journals:
        if args.listar or args.simular:
            raise RuntimeError("Hay una operación pendiente. Ejecutá el BAT sin --listar ni --simular, conservando --config, para recuperarla.")
        for journal in journals:
            print("Recuperando una operación interrumpida antes de continuar...")
            recuperar(proyecto, remoto, ruta_segura(proyecto.base, journal))
        print("Recuperación completada. Volvé a ejecutar el BAT para seleccionar otra sesión.")
        return 0
    remote_inventory = remoto.pedir("inventario")
    if proyecto.pendientes() or remote_inventory["pendientes"] or remote_inventory["controles"]:
        raise RuntimeError("Hay una operación pendiente sin su diario local. Conservá las copias y revisá la recuperación antes de eliminar otra sesión.")
    rows = unir_inventarios(proyecto.inventario(), remote_inventory["sesiones"])
    print("STORYPULSE - ELIMINAR UNA SESIÓN LOCAL Y DEL VPS")
    if args.listar:
        for i, row in enumerate(rows, 1):
            print(f"{i}. {describir(row)}")
        return 0
    if args.cuenta:
        value = args.cuenta.strip().lstrip("@").removesuffix(".json").lower()
        matches = [r for r in rows if value == r["id"] or any(value in
            (s.get("username"), Path(s["archivo"]).stem.lower() if s.get("archivo") else None)
            for s in (r.get("local", {}), r.get("vps", {})))]
        if len(matches) != 1:
            raise RuntimeError("El nombre no identifica una única sesión.")
        chosen = matches[0]
    else:
        chosen = elegir(rows, describir, "¿Qué sesión deseás eliminar por completo?")
    if chosen.get("conflicto"):
        raise RuntimeError("Ese ID corresponde a cuentas distintas en local y VPS. No se eliminará ninguna.")
    ident = chosen["id"]
    previews = {"local": proyecto.resumen(ident), "vps": remoto.pedir("resumen", id=ident)}
    print("\nSesión elegida: " + describir(chosen))
    for key, label in (("local", "Equipo local"), ("vps", "VPS")):
        mostrar_resumen(label, previews[key])
    if args.simular:
        print("\nSIMULACIÓN: no se modificó ningún archivo ni se reinició el servicio.")
        return 0
    print("\nLos perfiles consultados, grupos e historias descargadas se conservan.")
    if input("Escribí ELIMINAR para confirmar, o Enter para cancelar: ").strip() != "ELIMINAR":
        raise KeyboardInterrupt
    last = any(p.get("presente") and p["resumen"]["sesiones_habilitadas_restantes"] == 0
               for p in previews.values())
    if last:
        print("ATENCIÓN: uno de los equipos quedará sin sesiones habilitadas. Sus programaciones se pausarán.")
        if input("Escribí SIN SESIONES para continuar, o Enter para cancelar: ").strip() != "SIN SESIONES":
            raise KeyboardInterrupt
    estado = eliminar(proyecto, remoto, ident, previews, comprobar_local_en_reposo)
    print(f"\nSESIÓN ELIMINADA: {ident}. Limpieza local y remota comprobada.")
    print("Servicio del VPS: " + estado["servicio"])
    print("Se eliminaron las copias temporales de recuperación.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nEliminación cancelada.")
        sys.exit(130)
    except Exception as error:
        print("\nPROCESO INTERRUMPIDO: " + str(error))
        sys.exit(1)

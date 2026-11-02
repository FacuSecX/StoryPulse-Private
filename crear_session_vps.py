#!/usr/bin/env python3
# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Crear una cuenta nueva con Chromium, subir su JSON y registrarla en el VPS."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from renovar_session_vps import (
    SesionLocal, cargar_conexion, id_instagram, leer_json,
    transferir_al_vps, validar_sesion,
)

BASE = Path(__file__).resolve().parent


def normalizar_cuenta(valor: str) -> str:
    cuenta = valor.strip().removeprefix("@").lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._]{0,29}", cuenta):
        raise RuntimeError("Ingresá el username de Instagram: debe comenzar con una letra o número "
                           "y usar hasta 30 letras, números, puntos o guiones bajos.")
    if Path(cuenta).is_reserved() or cuenta.endswith("."):
        raise RuntimeError("Ese nombre no se puede usar como archivo de sesión en Windows.")
    return cuenta


def comprobar_cuenta_nueva(cuenta: str, base: Path = BASE) -> Path:
    archivo = base / "sesiones_instagram" / f"{cuenta}.json"
    if archivo.exists() or archivo.is_symlink():
        raise RuntimeError(f"La sesión {cuenta} ya existe. Usá RENOVAR_SESSION.bat para renovarla.")
    config_path = base / "sesiones_instagram.json"
    if config_path.exists():
        config = leer_json(config_path)
        if not isinstance(config, dict) or config.get("version") != 1 or not isinstance(config.get("sesiones"), list):
            raise RuntimeError("sesiones_instagram.json no tiene una configuración válida.")
        for fila in config["sesiones"]:
            if not isinstance(fila, dict):
                raise RuntimeError("sesiones_instagram.json contiene una fila inválida.")
            nombres = {str(fila.get("id") or "").strip().lower(),
                       str(fila.get("username") or "").strip().lstrip("@").lower()}
            ruta = fila.get("archivo")
            misma_ruta = False
            if isinstance(ruta, str):
                ruta = Path(ruta)
                ruta = ruta if ruta.is_absolute() else base / ruta
                misma_ruta = ruta.resolve() == archivo.resolve()
            if cuenta in nombres or misma_ruta:
                raise RuntimeError(f"La cuenta {cuenta} ya está registrada. Usá RENOVAR_SESSION.bat.")
    return archivo


def pedir_cuenta(base: Path = BASE, *, solo_subir: bool = False) -> str:
    print("Ingresá el username de Instagram de la cuenta. Escribí 0 para cancelar.")
    while True:
        valor = input("Nombre de sesión o cuenta: ").strip()
        if valor == "0":
            raise KeyboardInterrupt
        try:
            cuenta = normalizar_cuenta(valor)
            if not solo_subir:
                comprobar_cuenta_nueva(cuenta, base)
            return cuenta
        except RuntimeError as error:
            print(error)


def sesion_guardada(cuenta: str, base: Path = BASE) -> SesionLocal:
    archivo = (base / "sesiones_instagram" / f"{cuenta}.json").resolve()
    validar_sesion(archivo)
    usuario_id = id_instagram(archivo)
    if not usuario_id:
        raise RuntimeError("El JSON no permite confirmar el ID de la cuenta de Instagram. No se subirá.")
    return SesionLocal(archivo, cuenta, cuenta, usuario_id)


def crear_local(cuenta: str, base: Path = BASE, espera: int = 600) -> SesionLocal:
    archivo = comprobar_cuenta_nueva(cuenta, base).resolve()
    comando = [sys.executable, "-X", "utf8", "-B", str(base / "crear_session.py"),
               "--cuenta", cuenta, "--navegador", "chromium", "--archivo-sesion", str(archivo),
               "--espera", str(espera), "--subida-automatica"]
    print(f"\nAbriendo Chromium para @{cuenta}. Iniciá sesión y completá la verificación manualmente.")
    resultado = subprocess.run(comando, cwd=str(base))
    if resultado.returncode != 0:
        raise RuntimeError("No terminó la creación de la sesión local. El VPS no fue modificado.")
    return sesion_guardada(cuenta, base)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Crear una sesión nueva con Chromium y subirla al VPS.")
    parser.add_argument("--cuenta", help="Username de Instagram; sin este parámetro se pide por pantalla.")
    parser.add_argument("--solo-subir", action="store_true", help="Reintentar la subida al VPS con el JSON ya creado.")
    parser.add_argument("--espera", type=int, default=600, help="Segundos para completar el inicio de sesión manual.")
    parser.add_argument("--config", type=Path, help="Archivo privado de conexión compartido; por defecto vps_connection.local.json.")
    args = parser.parse_args(argv)
    if args.espera <= 0:
        parser.error("--espera debe ser mayor que cero.")
    print("=" * 68)
    print("STORYPULSE - CREAR UNA SESIÓN NUEVA Y AGREGARLA AL VPS")
    print("=" * 68)
    cuenta = normalizar_cuenta(args.cuenta) if args.cuenta else pedir_cuenta(BASE, solo_subir=args.solo_subir)
    if not args.solo_subir:
        comprobar_cuenta_nueva(cuenta, BASE)
    conexion = cargar_conexion(BASE, args.config)
    print(f"\nCuenta: @{cuenta}")
    print(f"Destino: {conexion.remote_dir}/sesiones_instagram/{cuenta}.json")
    sesion = sesion_guardada(cuenta, BASE) if args.solo_subir else crear_local(cuenta, BASE, args.espera)
    try:
        transferir_al_vps(conexion, sesion, BASE, nueva=not args.solo_subir, registrar=True)
    except (RuntimeError, OSError):
        print("\nLa sesión quedó guardada localmente. Podés reintentar la subida sin volver a iniciar sesión:")
        if args.config:
            print("Reejecutá el mismo comando conservando --config y agregá:")
            print(f"--solo-subir --cuenta {cuenta}")
        else:
            print(f"CREAR_SESSION.bat --solo-subir --cuenta {cuenta}")
        raise
    print(f"\nSESIÓN NUEVA CREADA Y SUBIDA: {sesion.archivo.name}")
    print(f"JSON subido: {conexion.remote_dir}/sesiones_instagram/{sesion.archivo.name}")
    print("Sesión incluida en sesiones_instagram.json del VPS, conservando las demás cuentas.")
    print(f"Servicio reiniciado y comprobado: {conexion.service_name} = active")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nCreación cancelada.")
        sys.exit(130)
    except Exception as error:
        print(f"\nPROCESO INTERRUMPIDO: {error}")
        sys.exit(1)

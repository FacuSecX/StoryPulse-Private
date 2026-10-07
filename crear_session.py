#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# StoryPulse v2.0
# Created by FacuSecX https://github.com/FacuSecX/StoryPulse-Private

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from instagram_sessions import (
    SesionInstagram, listar_sesiones, registrar_sesion, escribir_json_atomico,
    validar_pagina_instagram, ErrorSesionInstagram,
)

BASE = Path(__file__).resolve().parent


def _identidades(data, resultado: dict[str, str]) -> None:
    if isinstance(data, dict):
        username = str(data.get("username") or "").strip().lower()
        if re.fullmatch(r"[a-z0-9._]{1,30}", username):
            for clave in ("id", "pk", "pk_id", "user_id"):
                uid = str(data.get(clave) or "")
                if uid.isdigit():
                    resultado[uid] = username
        for value in data.values():
            _identidades(value, resultado)
    elif isinstance(data, list):
        for value in data:
            _identidades(value, resultado)


def _abrir_navegador(p, perfil: Path, navegador: str, *, perfil_exacto: bool = False):
    opciones = {
        # Opciones básicas compartidas por los perfiles independientes.
        "headless": False,
        "viewport": {"width": 1400, "height": 900},
    }
    if navegador == "edge":
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(perfil), channel="msedge", **opciones,
            )
            print("Navegador: Microsoft Edge")
            return context
        except Exception:
            if perfil_exacto:
                raise RuntimeError("No se pudo abrir el perfil de Edge seleccionado. Cerrá el navegador que lo esté usando y reintentá.")
            print("No se pudo iniciar Edge. Se intentará Chromium con un perfil independiente.")
            navegador = "chromium"
    # Mantener separados los perfiles de motores distintos; no borrar el anterior.
    if not perfil_exacto:
        perfil = perfil.with_name(perfil.name + "_" + navegador)
    if navegador == "chrome":
        opciones["channel"] = "chrome"
    context = p.chromium.launch_persistent_context(user_data_dir=str(perfil), **opciones)
    print(f"Navegador: {navegador}")
    return context


def _esperar_inicio_manual(page, context, timeout_seconds: int = 600) -> list[dict]:
    """Esperar con Playwright activo, sin bloquear sus eventos con input()."""
    limite = time.monotonic() + timeout_seconds
    estable_desde = None
    cookie_anterior = None
    aviso_anterior = None
    while time.monotonic() < limite:
        if page.is_closed():
            raise RuntimeError("Se cerró el navegador antes de completar el inicio de sesión. No se reemplazó el JSON anterior.")
        estado = "login"
        cookies = context.cookies("https://www.instagram.com/")
        cookie = next((c.get("value") for c in cookies
                       if c.get("name") == "sessionid" and c.get("value")), None)
        try:
            validar_pagina_instagram(page)
        except ErrorSesionInstagram as error:
            estado = error.tipo
        else:
            url = urlsplit(page.url)
            formulario = page.locator('input[name="username"]').is_visible()
            if (url.scheme == "https" and url.hostname in ("instagram.com", "www.instagram.com")
                    and cookie and not formulario):
                estado = "autenticada"

        # Un sessionid puede existir aunque el CAPTCHA o el doble factor sigan pendientes.
        # Esperar una página normal estable antes de sustituir el archivo anterior.
        if estado == "autenticada":
            ahora = time.monotonic()
            if cookie != cookie_anterior or estable_desde is None:
                estable_desde = ahora
                cookie_anterior = cookie
            elif ahora - estable_desde >= 2:
                return cookies
        else:
            estable_desde = None
            cookie_anterior = None
            if estado != aviso_anterior:
                if estado == "verificacion":
                    print("Instagram solicita una verificación. Completala MANUALMENTE en el navegador.")
                else:
                    print("Esperando el inicio de sesión. Usá el navegador; no hace falta presionar ENTER.")
                aviso_anterior = estado
        # Igual que en el creador anterior: las esperas de Playwright despachan eventos.
        page.wait_for_timeout(500)
    raise RuntimeError("No terminó el inicio de sesión o la verificación dentro del tiempo disponible. "
                       "No se reemplazó el JSON anterior. Podés ampliar el tiempo con --espera 1200.")


def _guardar_sesion_verificada(context, sesion: SesionInstagram) -> None:
    try:
        estado = context.storage_state(indexed_db=True)
    except TypeError:
        estado = context.storage_state()
    cookies = estado.get("cookies", []) if isinstance(estado, dict) else []
    if not isinstance(cookies, list):
        cookies = []
    if not any(isinstance(c, dict) and c.get("name") == "sessionid" and c.get("value")
               and str(c.get("domain", "")).lstrip(".").lower() in
               ("instagram.com", "www.instagram.com") for c in cookies):
        raise RuntimeError("La exportación no contiene una sesión de Instagram válida. Se conservó el JSON anterior.")
    escribir_json_atomico(sesion.archivo, estado)


def main() -> None:
    parser = argparse.ArgumentParser(description="Exportar manualmente una sesión de Instagram.")
    parser.add_argument("--cuenta", help="Username de la cuenta autenticada; sin @.")
    parser.add_argument("--navegador", choices=("edge", "chrome", "chromium"), default="edge",
                        help="Navegador para iniciar sesión manualmente (por defecto: edge).")
    parser.add_argument("--espera", type=int, default=600, metavar="SEGUNDOS",
                        help="Tiempo para completar el login y las verificaciones (por defecto: 600 segundos).")
    parser.add_argument("--perfil", type=Path, help="Perfil de navegador existente; se utiliza esa carpeta exacta.")
    parser.add_argument("--archivo-sesion", type=Path, help="JSON exacto a renovar, aunque su nombre sea un alias.")
    parser.add_argument("--usuario-id", help="ID de Instagram de la sesión anterior, para confirmar que se renovó la misma cuenta.")
    args = parser.parse_args()
    cuenta = args.cuenta.strip().lstrip("@").lower() if args.cuenta else None
    if cuenta and not re.fullmatch(r"[a-z0-9._]{1,30}", cuenta):
        parser.error("--cuenta debe ser un username válido de Instagram.")
    if args.espera <= 0:
        parser.error("--espera debe ser mayor que cero.")
    if args.usuario_id and not args.usuario_id.isdigit():
        parser.error("--usuario-id debe ser numérico.")
    if args.perfil and not args.perfil.is_dir():
        parser.error("--perfil debe ser una carpeta existente.")

    sesiones = listar_sesiones(incluir_deshabilitadas=True)
    identificador = cuenta or "principal"
    existente = next((s for s in sesiones if s.id == identificador), None)
    if existente:
        state = existente.archivo
    elif cuenta:
        state = BASE / "sesiones_instagram" / f"{cuenta}.json"
    else:
        state = BASE / "instagram_state.json"
    if args.archivo_sesion:
        state = (args.archivo_sesion if args.archivo_sesion.is_absolute() else BASE / args.archivo_sesion).resolve()
        if state.suffix.lower() != ".json":
            parser.error("--archivo-sesion debe terminar en .json.")
    perfil = BASE / "perfiles_instagram" / cuenta if cuenta else BASE / "perfil_instagram_playwright"
    if args.perfil:
        perfil = args.perfil.resolve()
    identidad: dict[str, str] = {}

    print("=" * 65)
    print(f" EXPORTAR SESIÓN WEB DE INSTAGRAM: {identificador}")
    print("=" * 65)
    print("Iniciá sesión MANUALMENTE y resolvé cualquier verificación en el navegador.")
    if cuenta:
        print(f"Este navegador debe quedar autenticado como @{cuenta}.")
    print("Si Instagram queda cargando la verificación, no exportes hasta poder entrar normalmente.")

    with sync_playwright() as p:
        if args.perfil:
            context = _abrir_navegador(p, perfil, args.navegador, perfil_exacto=True)
        else:
            context = _abrir_navegador(p, perfil, args.navegador)
        try:
            page = context.pages[0] if context.pages else context.new_page()

            def fallo_solicitud(request):
                url = urlsplit(request.url)
                failure = str(request.failure or "")
                match = re.search(r"(?:net::)?ERR_[A-Z0-9_]+", failure)
                codigo = match.group(0) if match else "error de red"
                if "ERR_ABORTED" in codigo:
                    return
                # Nunca mostrar parámetros de autenticación/reCAPTCHA ni cookies.
                print(f"⚠️ No se pudo cargar un recurso de {url.hostname or 'la página'}: {codigo}")

            def recibir_solicitud_terminada(request):
                try:
                    response = request.response()
                    if (response and urlsplit(response.url).hostname in ("instagram.com", "www.instagram.com")
                            and "json" in response.headers.get("content-type", "")):
                        _identidades(response.json(), identidad)
                except Exception:
                    pass

            # Leer cuerpos terminados evita esperar una descarga dentro de un evento response.
            page.on("requestfinished", recibir_solicitud_terminada)
            page.on("requestfailed", fallo_solicitud)
            try:
                page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightTimeoutError:
                print("La página todavía está cargando; se continúa esperando en el navegador.")
            page.wait_for_timeout(2000)
            cookies = _esperar_inicio_manual(page, context, args.espera)
            uid = next((str(c.get("value")) for c in cookies if c.get("name") == "ds_user_id"), "")
            actual = identidad.get(uid)
            if args.usuario_id and uid != args.usuario_id:
                raise RuntimeError("El perfil abrió otra cuenta de Instagram o no permitió confirmar su identidad. Se conservó el JSON anterior.")
            if actual and cuenta and actual != cuenta and not args.usuario_id:
                raise RuntimeError(f"El navegador está autenticado como @{actual}, no como @{cuenta}. Cerrá sesión y usá la cuenta correcta.")
            sesion = SesionInstagram(identificador, actual or cuenta or (existente.username if existente else None), state)
            validar_pagina_instagram(page)
            _guardar_sesion_verificada(context, sesion)
            registrar_sesion(sesion.id, sesion.username, sesion.archivo)
            print(f"✅ SESIÓN EXPORTADA: {state}")
            print("Copiá el archivo y sesiones_instagram.json al VPS conservando sus carpetas.")
            print("Para agregar otra cuenta, ejecutá: python crear_session.py --cuenta OTRO_USERNAME")
        finally:
            context.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("Inicio de sesión cancelado.")
    except RuntimeError as error:
        raise SystemExit(f"❌ {error}")

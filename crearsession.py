#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Bot Telegram StoryPulse v1.0
# https://github.com/FacuSecX/


from pathlib import Path
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

BASE = Path(__file__).resolve().parent
PROFILE = BASE / "perfil_instagram_playwright"
STATE = BASE / "instagram_state.json"

print("=" * 65)
print(" EXPORTAR SESIÓN WEB DE INSTAGRAM")
print("=" * 65)
print()
print("Se abrirá Instagram en un navegador independiente.")
print("Iniciá sesión MANUALMENTE.")
print()

with sync_playwright() as p:
    try:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            channel="msedge",
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        print("Navegador: Microsoft Edge")
    except Exception:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            viewport={"width": 1400, "height": 900},
        )
        print("Navegador: Chromium")

    page = context.pages[0] if context.pages else context.new_page()

    page.goto(
        "https://www.instagram.com/",
        wait_until="domcontentloaded",
        timeout=60_000,
    )

    print()
    print("1. Iniciá sesión en Instagram.")
    print("2. Esperá hasta ver tu cuenta normalmente.")
    print("3. Volvé a esta consola.")
    print()

    input("Cuando ya estés logueado, presioná ENTER... ")

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        pass
    except Exception:
        pass

    page.wait_for_timeout(3_000)

    cookies = context.cookies(
        "https://www.instagram.com/"
    )

    nombres = {
        cookie.get("name")
        for cookie in cookies
    }

    if "sessionid" not in nombres:
        print()
        print("❌ No se detectó sessionid.")
        input("ENTER para cerrar...")
        context.close()
        raise SystemExit(1)

    try:
        context.storage_state(
            path=str(STATE),
            indexed_db=True,
        )
    except TypeError:
        context.storage_state(
            path=str(STATE)
        )

    print()
    print("✅ SESIÓN EXPORTADA")
    print(f"Archivo: {STATE}")
    print()
    print("NO compartas instagram_state.json.")
    print("Copialo solamente a tu VPS.")
    print()

    input("ENTER para cerrar...")
    context.close()

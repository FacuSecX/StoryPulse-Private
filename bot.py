# Bot Telegram StoryPulse v1.0
# https://github.com/FacuSecX/



from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, time as dt_time, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import database as db
from history import (
    PerfilNoEncontrado,
    PerfilPrivado,
    SinHistoriasDisponibles,
    comprobar_perfil_accesible,
    comprobar_sesion_local,
    descargar_historias,
    limpiar_username,
    resolver_user_id,
)
from publicaciones import descargar_publicaciones

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID_ENV = os.getenv("TELEGRAM_CHAT_ID", "").strip()
HISTORYS_DIR = Path(
    os.getenv("HISTORYS_DIR", "/historys")
).expanduser()

_instagram_state_env = os.getenv(
    "INSTAGRAM_STORAGE_STATE",
    "instagram_state.json",
).strip()
INSTAGRAM_STATE_PATH = Path(_instagram_state_env).expanduser()
if not INSTAGRAM_STATE_PATH.is_absolute():
    INSTAGRAM_STATE_PATH = BASE_DIR / INSTAGRAM_STATE_PATH

PANEL_URL = os.getenv(
    "STORYPULSE_PANEL_URL",
    "https://tupanel.com/",
).strip()
TZ = ZoneInfo(
    os.getenv(
        "STORYPULSE_TIMEZONE",
        "America/Argentina/Buenos_Aires",
    )
)

if not BOT_TOKEN:
    raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en .env")

if not CHAT_ID_ENV:
    raise RuntimeError("Falta TELEGRAM_CHAT_ID en .env")

AUTHORIZED_CHAT_ID = int(CHAT_ID_ENV)

CUENTAS_FILE = BASE_DIR / "cuentas.json"
CUENTAS_LOCK = threading.RLock()
IG_LOCK = asyncio.Lock()
STORY_PROCESS_LOCK = asyncio.Lock()

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("storypulse-web")
logging.getLogger("httpx").setLevel(logging.WARNING)

STATE = "state"
ADD_NAME = "add_name"
ADD_USERNAME = "add_username"
SCHED_USERNAME = "sched_username"
SCHED_COUNT = "sched_count"
SCHED_TIMES = "sched_times"
SCHED_INTERVAL_HOURS = "sched_interval_hours"
SCHED_INTERVAL_MINUTE = "sched_interval_minute"


def autorizado(update: Update) -> bool:
    chat = update.effective_chat
    user = update.effective_user

    return bool(
        chat
        and user
        and int(chat.id) == AUTHORIZED_CHAT_ID
        and int(user.id) == AUTHORIZED_CHAT_ID
    )


def esc(texto) -> str:
    return html.escape(str(texto), quote=False)


def _formatear_antiguedad_segundos(segundos: float) -> str:
    segundos = max(0, int(segundos))
    dias, resto = divmod(segundos, 86_400)
    horas, resto = divmod(resto, 3_600)
    minutos, _ = divmod(resto, 60)

    partes: list[str] = []

    if dias:
        partes.append(
            f"{dias} día" if dias == 1 else f"{dias} días"
        )
    if horas:
        partes.append(
            f"{horas} hora" if horas == 1 else f"{horas} horas"
        )
    if minutos and len(partes) < 2:
        partes.append(
            f"{minutos} min"
        )

    if not partes:
        return "menos de 1 min"

    return " ".join(partes[:2])


def _antiguedad_archivo_sesion() -> str:
    try:
        modificado = INSTAGRAM_STATE_PATH.stat().st_mtime
    except OSError:
        return "no disponible"

    return _formatear_antiguedad_segundos(
        time.time() - modificado
    )


def _cookie_storage_state(nombre: str) -> str | None:
    try:
        data = json.loads(
            INSTAGRAM_STATE_PATH.read_text(encoding="utf-8")
        )
    except Exception:
        return None

    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        return None

    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        if str(cookie.get("name", "")) != nombre:
            continue
        valor = str(cookie.get("value", "")).strip()
        if valor:
            return valor

    return None


def _username_de_objeto_por_id(
    objeto,
    user_id: str | None,
) -> str | None:
    if not user_id:
        return None

    objetivo = str(user_id)
    pila = [objeto]
    revisados = 0

    while pila and revisados < 20_000:
        actual = pila.pop()
        revisados += 1

        if isinstance(actual, dict):
            username = actual.get("username")
            if username:
                ids = []
                for clave in (
                    "id",
                    "pk",
                    "user_id",
                    "strong_id__",
                ):
                    valor = actual.get(clave)
                    if valor is not None:
                        ids.append(str(valor))

                if objetivo in ids:
                    try:
                        return limpiar_username(str(username))
                    except ValueError:
                        pass

            pila.extend(actual.values())

        elif isinstance(actual, list):
            pila.extend(actual)

    return None


def _username_de_html_por_id(
    html_pagina: str,
    user_id: str | None,
) -> str | None:
    if not user_id:
        return None

    uid = re.escape(str(user_id))
    patrones = [
        rf'"(?:id|pk|user_id)"\s*:\s*"?{uid}"?.{{0,2200}}?"username"\s*:\s*"([A-Za-z0-9._]+)"',
        rf'"username"\s*:\s*"([A-Za-z0-9._]+)".{{0,2200}}?"(?:id|pk|user_id)"\s*:\s*"?{uid}"?',
    ]

    for patron in patrones:
        match = re.search(
            patron,
            html_pagina,
            re.I | re.S,
        )
        if not match:
            continue
        try:
            return limpiar_username(match.group(1))
        except ValueError:
            continue

    return None


def _comprobar_sesion_web_real() -> dict[str, object]:
    """
    Comprueba la sesión contra la portada/feed de Instagram.

    """
    comprobar_sesion_local()

    if not INSTAGRAM_STATE_PATH.exists():
        raise FileNotFoundError(
            f"No existe {INSTAGRAM_STATE_PATH}."
        )

    ds_user_id = _cookie_storage_state("ds_user_id")
    usernames: list[str] = []
    estados_http_problematicos: list[int] = []

   
    
    try:
        estado_completo = json.loads(
            INSTAGRAM_STATE_PATH.read_text(encoding="utf-8")
        )
        username_estado = _username_de_objeto_por_id(
            estado_completo,
            ds_user_id,
        )
        if username_estado:
            usernames.append(username_estado)
    except Exception:
        pass

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=str(INSTAGRAM_STATE_PATH),
            viewport={"width": 1365, "height": 900},
            locale="es-AR",
            timezone_id=str(getattr(TZ, "key", "America/Argentina/Buenos_Aires")),
        )

        try:
            page = context.new_page()

            def procesar_response(response) -> None:
                try:
                    if "instagram.com" not in response.url.lower():
                        return

                    if response.status in (400, 401, 403, 429):
                        estados_http_problematicos.append(
                            int(response.status)
                        )

                    ctype = (
                        response.headers.get("content-type", "")
                        or ""
                    ).lower()
                    if "json" not in ctype:
                        return

                    data = response.json()
                    username = _username_de_objeto_por_id(
                        data,
                        ds_user_id,
                    )
                    if username and username not in usernames:
                        usernames.append(username)
                except Exception:
                    return

            page.on("response", procesar_response)

            response = page.goto(
                "https://www.instagram.com/",
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            if response is None:
                raise RuntimeError(
                    "Instagram no devolvió respuesta al abrir el feed."
                )

            estado_http = int(response.status)
            if estado_http >= 400:
                raise RuntimeError(
                    f"Instagram respondió HTTP {estado_http} al abrir el feed."
                )

            page.wait_for_timeout(3_500)

            url_final = page.url.lower()
            rutas_bloqueo = (
                "/accounts/login",
                "/challenge/",
                "/checkpoint/",
                "/auth_platform/",
                "/accounts/confirm_email",
                "/accounts/confirm_phone",
            )

            if "/accounts/login" in url_final:
                raise RuntimeError(
                    "Instagram redirigió al login. La sesión web ya no es válida."
                )

            if any(ruta in url_final for ruta in rutas_bloqueo[1:]):
                raise RuntimeError(
                    "Instagram abrió una verificación/challenge. "
                    "La sesión requiere intervención manual."
                )

            try:
                texto_pagina = page.locator("body").inner_text(
                    timeout=5_000
                ).casefold()
            except Exception:
                texto_pagina = ""

            indicadores_bloqueo = (
                "no soy un robot",
                "i'm not a robot",
                "confirm it's you",
                "confirma que eres tú",
                "confirma que sos vos",
                "código de seguridad",
                "security code",
                "actividad sospechosa",
                "suspicious activity",
                "suspendimos tu cuenta",
                "we suspended your account",
            )

            if any(
                indicador in texto_pagina
                for indicador in indicadores_bloqueo
            ):
                raise RuntimeError(
                    "Instagram mostró una verificación de seguridad/CAPTCHA. "
                    "La sesión requiere intervención manual."
                )

            if not usernames:
                try:
                    username_html = _username_de_html_por_id(
                        page.content(),
                        ds_user_id,
                    )
                    if username_html:
                        usernames.append(username_html)
                except Exception:
                    pass

            # Un 400/401/403/429 en una respuesta interna durante la carga es
            # relevante para el diagnóstico, aunque la navegación principal haya
            # devuelto 200. No usamos 404 porque Instagram genera algunos recursos
            # opcionales con 404 sin invalidar la sesión.
            
            internos = sorted(set(estados_http_problematicos))
            if any(codigo in (400, 401, 403, 429) for codigo in internos):
                raise RuntimeError(
                    "Instagram devolvió una respuesta interna de autenticación "
                    f"HTTP {', '.join(map(str, internos))}."
                )

            return {
                "ok": True,
                "http_status": estado_http,
                "username": usernames[0] if usernames else None,
                "user_id": ds_user_id,
                "url_final": page.url,
                "antiguedad_archivo": _antiguedad_archivo_sesion(),
            }

        finally:
            context.close()
            browser.close()


def registrar_mensaje_limpiable(
    chat_id: int,
    message_id: int,
) -> None:
    try:
        db.registrar_mensaje_chat_limpiable(
            int(chat_id),
            int(message_id),
        )
    except Exception:
        logger.exception(
            "No se pudo registrar message_id=%s para LIMPIAR CHAT",
            message_id,
        )


async def enviar_texto_bot(
    context: ContextTypes.DEFAULT_TYPE,
    *args,
    **kwargs,
):
    """
    Envía texto y registra su message_id para LIMPIAR CHAT.
    Nunca se usa para fotos/videos.
    """
    mensaje = await context.bot.send_message(
        *args,
        **kwargs,
    )

    try:
        registrar_mensaje_limpiable(
            int(mensaje.chat.id),
            int(mensaje.message_id),
        )
    except Exception:
        logger.exception(
            "No se pudo registrar texto saliente de Telegram."
        )

    return mensaje


async def responder_texto(
    update: Update,
    *args,
    **kwargs,
):
    """
    reply_text rastreable.
    """
    mensaje_origen = update.effective_message

    if mensaje_origen is None:
        return None

    mensaje = await mensaje_origen.reply_text(
        *args,
        **kwargs,
    )

    try:
        registrar_mensaje_limpiable(
            int(mensaje.chat.id),
            int(mensaje.message_id),
        )
    except Exception:
        logger.exception(
            "No se pudo registrar reply_text de Telegram."
        )

    return mensaje


def registrar_texto_entrante(
    update: Update,
) -> None:
    """
    Registra únicamente mensajes con texto recibidos del usuario.
    Fotos/videos, incluso con caption, quedan fuera de LIMPIAR CHAT.
    """
    mensaje = update.effective_message

    if (
        mensaje is None
        or mensaje.text is None
    ):
        return

    try:
        registrar_mensaje_limpiable(
            int(mensaje.chat.id),
            int(mensaje.message_id),
        )
    except Exception:
        logger.exception(
            "No se pudo registrar texto entrante."
        )


def diagnosticar_error(error: Exception) -> tuple[str, str, str]:
    """
    Devuelve:
      (icono, tipo_legible, recomendacion)

    Se basa únicamente en el error que ya ocurrió.
    No realiza ninguna consulta adicional a Instagram.
    """
    texto = str(error)
    bajo = texto.lower()

    if (
        "429" in bajo
        or "rate limit" in bajo
        or "too many requests" in bajo
        or "feedback_required" in bajo
    ):
        return (
            "🚦",
            "RATE LIMIT / límite temporal de Instagram",
            (
                "No fuerces nuevas consultas inmediatamente. "
                "Dejá que la próxima programación vuelva a intentar."
            ),
        )

    if (
        "401" in bajo
        or "403" in bajo
        or "redirigió al login" in bajo
        or "redirected to login" in bajo
        or "sesión web ya no es válida" in bajo
        or "rechazó la sesión" in bajo
        or "sessionid" in bajo
        or "login" in bajo
    ):
        return (
            "🔐",
            "SESIÓN DE INSTAGRAM",
            (
                "La sesión puede haber caducado o sido invalidada. "
                "Si se repite, exportá un instagram_state.json nuevo."
            ),
        )

    if (
        "timeout" in bajo
        or "timed out" in bajo
        or "tiempo de espera" in bajo
    ):
        return (
            "⏱",
            "TIMEOUT / respuesta lenta",
            (
                "Instagram o la red tardaron demasiado. "
                "La próxima revisión puede volver a intentarlo."
            ),
        )

    if (
        "reels_media" in bajo
        or "query_hash" in bajo
        or "no devolvió json" in bajo
        or "json válido" in bajo
    ):
        return (
            "🧩",
            "RESPUESTA DE INSTAGRAM CAMBIÓ",
            (
                "Instagram respondió con una estructura inesperada. "
                "Puede requerir actualizar el endpoint/query_hash."
            ),
        )

    if (
        "cdn" in bajo
        or "image/jpeg" in bajo
        or "video/mp4" in bajo
    ):
        return (
            "🖼",
            "DESCARGA DE MULTIMEDIA",
            (
                "Instagram respondió a la Story, pero falló la descarga "
                "del archivo multimedia."
            ),
        )

    if (
        "instagram_state.json" in bajo
        or isinstance(error, FileNotFoundError)
    ):
        return (
            "📄",
            "ARCHIVO DE SESIÓN",
            (
                "Revisá que instagram_state.json exista, tenga permisos "
                "y corresponda a esta cuenta."
            ),
        )

    return (
        "❌",
        "ERROR INESPERADO",
        "Revisá el detalle técnico mostrado abajo.",
    )


def mensaje_error_instagram(
    username: str,
    error: Exception,
    *,
    automatico: bool,
) -> str:
    icono, tipo, recomendacion = diagnosticar_error(
        error
    )

    detalle = str(error).strip() or repr(error)

    # Evitar que por accidente un token termine visible en Telegram.
    if BOT_TOKEN:
        detalle = detalle.replace(
            BOT_TOKEN,
            "[TOKEN OCULTO]",
        )

    # El mensaje de Telegram no necesita un traceback completo.
    detalle = detalle[:1200]

    ahora = datetime.now(TZ).strftime(
        "%d/%m/%Y %H:%M:%S"
    )

    origen = (
        "Revisión automática"
        if automatico
        else "Revisión manual"
    )

    return (
        f"{icono} <b>ERROR STORYPULSE</b>\n\n"
        f"Cuenta: <b>@{esc(username)}</b>\n"
        f"Origen: {esc(origen)}\n"
        f"Hora: {esc(ahora)}\n"
        f"Tipo: <b>{esc(tipo)}</b>\n\n"
        f"<b>Detalle:</b>\n"
        f"<code>{esc(detalle)}</code>\n\n"
        f"💡 {esc(recomendacion)}"
    )


async def avisar_error_chat(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    username: str,
    error: Exception,
    *,
    automatico: bool,
) -> None:
    """
    El aviso de error nunca debe provocar otro fallo del motor.
    Si Telegram tampoco responde, queda registrado en journalctl.
    """
    try:
        await enviar_texto_bot(context,
            chat_id=int(chat_id),
            text=mensaje_error_instagram(
                username,
                error,
                automatico=automatico,
            ),
            parse_mode="HTML",
            disable_notification=False,
        )
    except Exception:
        logger.exception(
            "No se pudo enviar al chat el aviso de error de @%s",
            username,
        )


def clave_orden_alfabetico(valor: str) -> str:
    """
    Clave A -> Z estable para nombres visibles.

    Ignora mayúsculas/minúsculas y trata letras acentuadas
    como su equivalente base: Á como A, É como E, etc.
    """
    normalizado = unicodedata.normalize(
        "NFKD",
        str(valor),
    )

    sin_acentos = "".join(
        caracter
        for caracter in normalizado
        if not unicodedata.combining(
            caracter
        )
    )

    return sin_acentos.casefold()


def cargar_cuentas() -> list[dict]:
    with CUENTAS_LOCK:
        if not CUENTAS_FILE.exists():
            CUENTAS_FILE.write_text("[]\n", encoding="utf-8")

        data = json.loads(
            CUENTAS_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(data, list):
            raise RuntimeError("cuentas.json debe contener una lista.")

        resultado = []

        for item in data:
            if not isinstance(item, dict):
                continue

            try:
                username = limpiar_username(
                    item["username"]
                )
            except Exception:
                continue

            nombre = str(
                item.get("nombre") or username
            ).strip()

            user_id = item.get("user_id")

            if user_id not in (None, ""):
                try:
                    user_id = int(user_id)
                except Exception:
                    user_id = None

            resultado.append(
                {
                    "nombre": nombre,
                    "username": username,
                    "user_id": user_id,
                }
            )

        return sorted(
            resultado,
            key=lambda cuenta: (
                clave_orden_alfabetico(
                    cuenta["nombre"]
                ),
                clave_orden_alfabetico(
                    cuenta["username"]
                ),
            ),
        )


def guardar_cuentas(cuentas: list[dict]) -> None:
    with CUENTAS_LOCK:
        cuentas_ordenadas = sorted(
            cuentas,
            key=lambda cuenta: (
                clave_orden_alfabetico(
                    cuenta.get(
                        "nombre",
                        cuenta.get(
                            "username",
                            "",
                        ),
                    )
                ),
                clave_orden_alfabetico(
                    cuenta.get(
                        "username",
                        "",
                    )
                ),
            ),
        )

        temporal = CUENTAS_FILE.with_suffix(".tmp")
        temporal.write_text(
            json.dumps(
                cuentas_ordenadas,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporal, CUENTAS_FILE)


def buscar_cuenta(username: str):
    username = limpiar_username(username)

    for cuenta in cargar_cuentas():
        if cuenta["username"].casefold() == username.casefold():
            return cuenta

    return None


def actualizar_user_id(username: str, user_id: int) -> None:
    cuentas = cargar_cuentas()
    cambio = False

    for cuenta in cuentas:
        if cuenta["username"].casefold() == username.casefold():
            if cuenta.get("user_id") != int(user_id):
                cuenta["user_id"] = int(user_id)
                cambio = True
            break

    if cambio:
        guardar_cuentas(cuentas)


def menu_principal() -> InlineKeyboardMarkup:
    # Menú principal orientado al usuario: una opción por fila,
    # textos en mayúsculas y estilos visuales fáciles de distinguir.
    # Telegram solo permite fondos primary/azul, success/verde,
    # danger/rojo o neutro; rosa y naranja se señalan con emoji.
    filas = [
        [
            InlineKeyboardButton(
                "👤 REVISAR HISTORIAS",
                callback_data="stories_menu",
                style="primary",
            )
        ],
        [
            InlineKeyboardButton(
                "📥 DESCARGAR PUBLICACIONES",
                callback_data="publications_menu",
                style="success",
            )
        ],
        [
            InlineKeyboardButton(
                "👥 GESTIONAR CUENTAS",
                callback_data="manage",
                style="primary",
            )
        ],
        [
            InlineKeyboardButton(
                "🩷 PROGRAMAR REVISIÓN",
                callback_data="schedules",
            )
        ],
        [
            InlineKeyboardButton(
                "📋 VER PROGRAMACIONES",
                callback_data="sched_list",
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 ELIMINAR TODAS LAS PROGRAMACIONES",
                callback_data="sched_delete_all",
                style="danger",
            )
        ],
        [
            InlineKeyboardButton(
                "🟧 ESTADO",
                callback_data="status",
            )
        ],
        [
            InlineKeyboardButton(
                "🌐 ABRIR PANEL STORYPULSE",
                url=PANEL_URL,
                style="primary",
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 BORRAR MULTIMEDIA",
                callback_data="media_delete",
                style="danger",
            )
        ],
        [
            InlineKeyboardButton(
                "🧹 LIMPIAR CHAT",
                callback_data="chat_clean",
            )
        ],
    ]

    return InlineKeyboardMarkup(filas)


def menu_revisar_historias() -> InlineKeyboardMarkup:
    """Cuentas configuradas A -> Z en grilla de dos columnas."""
    filas = []
    botones = []

    for cuenta in cargar_cuentas():
        botones.append(
            InlineKeyboardButton(
                f"👤 {cuenta['nombre']}",
                callback_data=f"review:{cuenta['username']}",
            )
        )

    for posicion in range(0, len(botones), 2):
        filas.append(botones[posicion:posicion + 2])

    filas.append(
        [
            InlineKeyboardButton(
                "‹ Menú principal",
                callback_data="menu",
            )
        ]
    )

    return InlineKeyboardMarkup(filas)


def menu_publicaciones() -> InlineKeyboardMarkup:
    """
    Cuentas A -> Z en grilla de dos columnas para descargar
    publicaciones recientes. cargar_cuentas() ya devuelve el orden
    alfabético global del proyecto.
    """
    filas = []
    botones = []

    for cuenta in cargar_cuentas():
        botones.append(
            InlineKeyboardButton(
                f"🖼 {cuenta['nombre']}",
                callback_data=(
                    f"publications:{cuenta['username']}"
                ),
            )
        )

    for posicion in range(
        0,
        len(botones),
        2,
    ):
        filas.append(
            botones[
                posicion:posicion + 2
            ]
        )

    filas.append(
        [
            InlineKeyboardButton(
                "‹ Menú principal",
                callback_data="menu",
            )
        ]
    )

    return InlineKeyboardMarkup(filas)


def menu_estado() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 ACTUALIZAR ESTADO",
                    callback_data="status",
                    style="primary",
                )
            ],
            [
                InlineKeyboardButton(
                    "‹ MENÚ PRINCIPAL",
                    callback_data="menu",
                )
            ],
        ]
    )


def menu_gestion() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "➕ Agregar cuenta",
                    callback_data="add",
                )
            ],
            [
                InlineKeyboardButton(
                    "➖ Quitar cuenta",
                    callback_data="remove",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 Ver cuentas",
                    callback_data="list_accounts",
                )
            ],
            [
                InlineKeyboardButton(
                    "♻️ Reiniciar antirepetición",
                    callback_data="dedupe_reset_menu",
                    style="danger",
                )
            ],
            [
                InlineKeyboardButton(
                    "‹ Menú principal",
                    callback_data="menu",
                )
            ],
        ]
    )



def menu_reiniciar_antirepeticion() -> InlineKeyboardMarkup:
    """Cuentas A -> Z, dos por fila, para reiniciar un perfil concreto."""
    filas = []
    botones = []

    for cuenta in cargar_cuentas():
        botones.append(
            InlineKeyboardButton(
                f"♻️ {cuenta['nombre']}",
                callback_data=(
                    f"dedupe_reset_select:{cuenta['username']}"
                ),
            )
        )

    for posicion in range(0, len(botones), 2):
        filas.append(
            botones[posicion:posicion + 2]
        )

    filas.append(
        [
            InlineKeyboardButton(
                "‹ Volver",
                callback_data="manage",
            )
        ]
    )

    return InlineKeyboardMarkup(filas)


def usernames_con_programacion_activa() -> set[str]:
    """
    Usernames que ya tienen una programación ACTIVA.

    Las programaciones pausadas no bloquean la cuenta:
    pueden volver a configurarse desde Programar revisión.
    """
    activas: set[str] = set()

    for row in db.listar_programaciones(
        AUTHORIZED_CHAT_ID
    ):
        if bool(row["activa"]):
            activas.add(
                str(
                    row["username"]
                ).casefold()
            )

    return activas


def cuentas_disponibles_para_programar():
    """
    Devuelve sólo las cuentas que todavía no tienen
    una programación activa.
    """
    activas = usernames_con_programacion_activa()

    return [
        cuenta
        for cuenta in cargar_cuentas()
        if str(
            cuenta["username"]
        ).casefold() not in activas
    ]


def menu_programaciones() -> InlineKeyboardMarkup:
    filas = []
    botones = []

    for cuenta in cuentas_disponibles_para_programar():
        botones.append(
            InlineKeyboardButton(
                f"⏰ {cuenta['nombre']}",
                callback_data=f"sched:{cuenta['username']}",
            )
        )

    # Cuentas disponibles en grilla de dos columnas.
    for posicion in range(
        0,
        len(botones),
        2,
    ):
        filas.append(
            botones[
                posicion:posicion + 2
            ]
        )

    filas.append(
        [
            InlineKeyboardButton(
                "📋 Ver programaciones",
                callback_data="sched_list",
            )
        ]
    )
    filas.append(
        [
            InlineKeyboardButton(
                "‹ Menú principal",
                callback_data="menu",
            )
        ]
    )

    return InlineKeyboardMarkup(filas)


def nombre_visible_programacion(username: str) -> str:
    """
    Devuelve el nombre amigable guardado en cuentas.json.
    Si la cuenta ya no está en la lista fija, usa @username.
    """
    cuenta = buscar_cuenta(
        username
    )

    if cuenta is not None:
        nombre = str(
            cuenta.get("nombre", "")
        ).strip()

        if nombre:
            return nombre

    return f"@{username}"


def menu_lista_programaciones(
    rows,
) -> InlineKeyboardMarkup:
    """
    Lista compacta de programaciones.

    - Orden alfabético A -> Z por nombre visible.
    - Dos cuentas por fila.
    - Los controles aparecen recién dentro del detalle.
    """
    filas = []
    botones = []

    # Ordenar por el nombre visible de la cuenta.
    # casefold() evita diferencias entre mayúsculas/minúsculas.
    rows_ordenadas = sorted(
        rows,
        key=lambda row: (
            clave_orden_alfabetico(
                nombre_visible_programacion(
                    str(row["username"])
                )
            )
        ),
    )

    for row in rows_ordenadas:
        username = str(
            row["username"]
        )
        nombre = nombre_visible_programacion(
            username
        )
        estado = (
            "✅"
            if bool(row["activa"])
            else "⏸"
        )

        botones.append(
            InlineKeyboardButton(
                f"{estado} {nombre}",
                callback_data=(
                    f"sched_detail:{username}"
                ),
            )
        )

    # Grilla de dos columnas.
    for posicion in range(
        0,
        len(botones),
        2,
    ):
        filas.append(
            botones[
                posicion:posicion + 2
            ]
        )

    filas.append(
        [
            InlineKeyboardButton(
                "‹ Volver",
                callback_data="schedules",
            )
        ]
    )

    return InlineKeyboardMarkup(
        filas
    )


def texto_detalle_programacion(
    row,
) -> str:
    username = str(
        row["username"]
    )
    nombre = nombre_visible_programacion(
        username
    )
    activa = bool(
        row["activa"]
    )
    multimedia = db.notificacion_activada(
        row
    )

    lineas = [
        "📋 <b>Programación</b>",
        "",
        f"Cuenta: <b>{esc(nombre)}</b>",
        f"Instagram: <b>@{esc(username)}</b>",
        "",
    ]

    if (
        db.tipo_programacion(row)
        == "intervalo"
    ):
        horas = (
            db.intervalo_horas_de(
                row
            )
            or 0
        )
        frecuencia = (
            "Cada 1 hora"
            if horas == 1
            else f"Cada {horas} horas"
        )

        inicio_intervalo = db.inicio_intervalo_de(
            row
        )
        minuto_intervalo = (
            inicio_intervalo.astimezone(TZ).minute
            if inicio_intervalo is not None
            else None
        )

        lineas.extend(
            [
                "Tipo: ⏱ <b>Intervalo de tiempo</b>",
                f"Frecuencia: <b>{esc(frecuencia)}</b>",
            ]
        )

        if minuto_intervalo is not None:
            lineas.append(
                "Minuto de la hora: "
                f"<b>:{minuto_intervalo:02d}</b>"
            )

        if activa:
            lineas.append(
                "Próxima revisión: "
                f"<b>{esc(texto_proxima_intervalo(row))} hs</b>"
            )
        else:
            lineas.append(
                "Próxima revisión: <b>pausada</b>"
            )
    else:
        horarios = db.horarios_de(
            row
        )
        horarios_texto = (
            " · ".join(horarios)
            if horarios
            else "sin horarios"
        )

        lineas.extend(
            [
                "Tipo: 📅 <b>Revisiones por día</b>",
                f"Horarios: <b>{esc(horarios_texto)}</b>",
            ]
        )

    lineas.extend(
        [
            "",
            "Estado: "
            + (
                "✅ <b>Activa</b>"
                if activa
                else "⏸ <b>Pausada</b>"
            ),
            "Multimedia: "
            + (
                "🔔 <b>Activada</b>"
                if multimedia
                else "🔕 <b>Desactivada</b>"
            ),
        ]
    )

    return "\n".join(
        lineas
    )


def menu_detalle_programacion(
    row,
) -> InlineKeyboardMarkup:
    username = str(
        row["username"]
    )
    activa = bool(
        row["activa"]
    )
    multimedia = db.notificacion_activada(
        row
    )

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    (
                        "⏸ Pausar"
                        if activa
                        else "▶️ Reanudar"
                    ),
                    callback_data=(
                        f"sched_toggle:{username}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    (
                        "🔕 Desactivar multimedia"
                        if multimedia
                        else "🔔 Activar multimedia"
                    ),
                    callback_data=(
                        f"sched_notify:{username}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 Eliminar programación",
                    callback_data=(
                        f"sched_delete:{username}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "‹ Volver a programaciones",
                    callback_data="sched_list",
                )
            ],
        ]
    )


def menu_tipo_programacion(
    username: str,
) -> InlineKeyboardMarkup:
    """
    Permite elegir entre el sistema clásico de horarios fijos
    y la nueva modalidad de intervalos.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📅 Revisiones por día",
                    callback_data=(
                        f"sched_mode_daily:{username}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "⏱ Intervalo de tiempo",
                    callback_data=(
                        f"sched_mode_interval:{username}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "‹ Volver",
                    callback_data="schedules",
                )
            ],
        ]
    )


def menu_intervalos(
    username: str,
) -> InlineKeyboardMarkup:
    """
    Intervalos disponibles: de 1 a 12 horas.
    """
    botones = []

    for horas in range(1, 13):
        etiqueta = (
            "Cada 1 hora"
            if horas == 1
            else f"Cada {horas} horas"
        )

        botones.append(
            InlineKeyboardButton(
                etiqueta,
                callback_data=(
                    f"sched_interval:{username}:{horas}"
                ),
            )
        )

    filas = []

    for posicion in range(
        0,
        len(botones),
        2,
    ):
        filas.append(
            botones[
                posicion:posicion + 2
            ]
        )

    filas.append(
        [
            InlineKeyboardButton(
                "‹ Volver",
                callback_data=f"sched:{username}",
            )
        ]
    )

    return InlineKeyboardMarkup(
        filas
    )


def menu_cantidad(username: str) -> InlineKeyboardMarkup:
    filas = []

    for n in range(1, 7):
        filas.append(
            [
                InlineKeyboardButton(
                    f"{n} revisión{'es' if n != 1 else ''} por día",
                    callback_data=f"sched_count:{username}:{n}",
                )
            ]
        )

    filas.append(
        [
            InlineKeyboardButton(
                "‹ Volver",
                callback_data=f"sched:{username}",
            )
        ]
    )

    return InlineKeyboardMarkup(filas)


def parse_hhmm(texto: str):
    try:
        partes = texto.strip().split(":")
        if len(partes) != 2:
            return None

        h = int(partes[0])
        m = int(partes[1])

        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None

        return f"{h:02d}:{m:02d}"
    except Exception:
        return None


def nombre_job(
    chat_id: int,
    username: str,
    sufijo: str,
) -> str:
    return (
        f"story:{chat_id}:{username}:{sufijo}"
    )


def eliminar_jobs_usuario(
    application: Application,
    chat_id: int,
    username: str,
) -> None:
    prefix = f"story:{chat_id}:{username}:"

    for job in application.job_queue.jobs():
        if (
            job.name
            and job.name.startswith(prefix)
        ):
            job.schedule_removal()


def registrar_jobs_programacion(
    application: Application,
    chat_id: int,
    username: str,
    horarios: list[str],
) -> None:
    """
    Modalidad clásica: uno o varios horarios fijos por día.
    """
    eliminar_jobs_usuario(
        application,
        chat_id,
        username,
    )

    for hhmm in horarios:
        h, m = map(
            int,
            hhmm.split(":"),
        )

        application.job_queue.run_daily(
            ejecucion_programada,
            time=dt_time(
                hour=h,
                minute=m,
                tzinfo=TZ,
            ),
            data={
                "chat_id": int(chat_id),
                "username": username,
            },
            name=nombre_job(
                chat_id,
                username,
                f"daily:{hhmm}",
            ),
            chat_id=int(chat_id),
        )


def proxima_revision_intervalo(
    row,
) -> datetime | None:
    """
    Próximo punto de la cadencia guardada, convertido a la zona
    horaria configurada para StoryPulse.
    """
    proxima = db.proxima_ejecucion_intervalo(
        row
    )

    if proxima is None:
        return None

    return proxima.astimezone(
        TZ
    )


def registrar_job_intervalo(
    application: Application,
    row,
) -> None:
    """
    Registra una programación "cada N horas".

    La cadencia usa el minuto de la hora elegido por el usuario.
    Por ejemplo, cada 2 horas en el minuto 46 mantiene siempre
    revisiones como 15:46, 17:46, 19:46...

    Si StoryPulse se reinicia, se calcula el próximo punto futuro
    conservando la cadencia original.
    """
    chat_id = int(
        row["chat_id"]
    )
    username = str(
        row["username"]
    )
    horas = db.intervalo_horas_de(
        row
    )
    proxima = db.proxima_ejecucion_intervalo(
        row
    )

    eliminar_jobs_usuario(
        application,
        chat_id,
        username,
    )

    if horas is None or proxima is None:
        raise RuntimeError(
            f"Programación por intervalo inválida para @{username}"
        )

    application.job_queue.run_repeating(
        ejecucion_programada,
        interval=timedelta(
            hours=horas
        ),
        first=proxima,
        data={
            "chat_id": chat_id,
            "username": username,
        },
        name=nombre_job(
            chat_id,
            username,
            f"interval:{horas}h",
        ),
        chat_id=chat_id,
    )


def registrar_programacion_guardada(
    application: Application,
    row,
) -> None:
    """
    Restaura/registra el job correcto según el tipo guardado.
    """
    if (
        db.tipo_programacion(row)
        == "intervalo"
    ):
        registrar_job_intervalo(
            application,
            row,
        )
        return

    registrar_jobs_programacion(
        application,
        int(row["chat_id"]),
        str(row["username"]),
        db.horarios_de(row),
    )


def texto_proxima_intervalo(
    row,
) -> str:
    proxima = proxima_revision_intervalo(
        row
    )

    if proxima is None:
        return "sin calcular"

    return proxima.strftime(
        "%d/%m/%Y %H:%M"
    )


def ruta_para_historia(username: str, historia) -> Path:
    carpeta = HISTORYS_DIR / username / "historys"
    carpeta.mkdir(parents=True, exist_ok=True)

    fecha = (
        historia.tomada_en.astimezone(TZ)
        if historia.tomada_en
        else datetime.now(TZ)
    ).strftime("%d-%m-%Y")

    extension = historia.extension or (
        "mp4" if historia.es_video else "jpg"
    )

    base = f"{username}-{fecha}"

    numero = 1

    while True:
        sufijo = "" if numero == 1 else f"-{numero}"
        destino = carpeta / f"{base}{sufijo}.{extension}"

        if not destino.exists():
            destino.write_bytes(historia.contenido)
            return destino

        numero += 1


async def enviar_archivo(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    username: str,
    historia,
    destino: Path,
) -> None:
    bio = BytesIO(historia.contenido)
    bio.name = destino.name

    hora_subida = ""

    if historia.tomada_en is not None:
        try:
            subida_local = historia.tomada_en.astimezone(TZ)
            dias_semana = (
                "lunes",
                "martes",
                "miércoles",
                "jueves",
                "viernes",
                "sábado",
                "domingo",
            )
            meses = (
                "enero",
                "febrero",
                "marzo",
                "abril",
                "mayo",
                "junio",
                "julio",
                "agosto",
                "septiembre",
                "octubre",
                "noviembre",
                "diciembre",
            )
            dia_semana = dias_semana[subida_local.weekday()]
            mes = meses[subida_local.month - 1]
            hora_subida = (
                f"\n🕒 Subida: {dia_semana} {subida_local.day} "
                f"{mes} a las {subida_local.strftime('%H:%M')} hs"
            )
        except Exception:
            logger.exception(
                "No se pudo convertir la hora de Story %s",
                historia.story_pk,
            )

    caption = (
        f"📸 @{username}"
        f"{hora_subida}\n"
        f"Story ID: {historia.story_pk}"
    )

    if historia.es_video:
        return await context.bot.send_video(
            chat_id=chat_id,
            video=bio,
            caption=caption,
            supports_streaming=True,
            disable_notification=True,
        )

    return await context.bot.send_photo(
        chat_id=chat_id,
        photo=bio,
        caption=caption,
        disable_notification=True,
    )


async def revisar_usuario(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    username: str,
    *,
    manual: bool,
) -> int:
    """
    Revisión manual:
      - siempre guarda en /historys/USERNAME/historys;
      - siempre muestra foto/video en Telegram.

    Revisión automática con notificaciones ON:
      - guarda en /historys/USERNAME/historys;
      - manda una alerta sonora;
      - muestra las fotos/videos.

    Revisión automática con notificaciones OFF:
      - guarda en /historys/USERNAME/historys;
      - NO muestra fotos/videos;
      - manda un único aviso silencioso.
    """
    # Una misma cuenta debe usar siempre la misma clave en la antirepetición,
    # tanto si llega desde un botón manual como desde una programación antigua.
    username = limpiar_username(username)

    cuenta = buscar_cuenta(username)
    user_id = (
        int(cuenta["user_id"])
        if cuenta and cuenta.get("user_id")
        else None
    )

    async with IG_LOCK:
        if user_id is None:
            user_id = await asyncio.to_thread(
                resolver_user_id,
                username,
            )
            actualizar_user_id(
                username,
                user_id,
            )

        historias = await asyncio.to_thread(
            descargar_historias,
            username,
            user_id,
        )

    # La comprobación y el registro del Story ID deben pertenecer a una única
    # sección crítica. Así una revisión manual y una automática no pueden ver
    # simultáneamente el mismo ID como "nuevo" antes de que una lo registre.
    async with STORY_PROCESS_LOCK:
        nuevas = []
        ids_vistos_en_respuesta: set[str] = set()

        for historia in historias:
            story_pk = str(historia.story_pk)

            # Defensa ante un mismo elemento repetido dentro de una respuesta.
            if story_pk in ids_vistos_en_respuesta:
                continue
            ids_vistos_en_respuesta.add(story_pk)

            if db.historia_ya_enviada(
                chat_id,
                username,
                story_pk,
            ):
                continue

            nuevas.append(historia)

        if not nuevas:
            if manual:
                await enviar_texto_bot(context,
                    chat_id=chat_id,
                    text=(
                        f"ℹ️ @{username}: no hay Stories nuevas."
                    ),
                    disable_notification=True,
                )
            return 0

        notificaciones_activadas = True

        if not manual:
            programacion = db.obtener_programacion(
                chat_id,
                username,
            )

            if programacion is not None:
                notificaciones_activadas = (
                    db.notificacion_activada(
                        programacion
                    )
                )

        # --------------------------------------------------------
        # AUTOMÁTICO + NOTIFICACIONES DESACTIVADAS
        # --------------------------------------------------------
        if (
            not manual
            and not notificaciones_activadas
        ):
            procesadas = 0

            for historia in nuevas:
                # Se vuelve a comprobar inmediatamente antes de procesar.
                # Dentro de STORY_PROCESS_LOCK nadie puede intercalar otro envío.
                if db.historia_ya_enviada(
                    chat_id,
                    username,
                    historia.story_pk,
                ):
                    continue

                # El archivo se conserva para el panel.
                ruta_para_historia(
                    username,
                    historia,
                )

                db.registrar_historia(
                    chat_id,
                    username,
                    historia.story_pk,
                )

                procesadas += 1

            if procesadas:
                try:
                    await enviar_texto_bot(context,
                        chat_id=chat_id,
                        text=(
                            f"📥 @{username} subió "
                            f"{procesadas} Story(s) nueva(s).\n"
                            "Se guardaron en el servidor."
                        ),
                        disable_notification=True,
                    )
                except TelegramError:
                    logger.exception(
                        "No se pudo enviar el aviso silencioso de @%s",
                        username,
                    )

            return procesadas

        # --------------------------------------------------------
        # MANUAL O AUTOMÁTICO + NOTIFICACIONES ACTIVADAS
        # --------------------------------------------------------
        # El aviso automático se envía sólo después de haber fijado, bajo el
        # mismo lock, cuáles son realmente las Stories nuevas.
        if not manual:
            await enviar_texto_bot(context,
                chat_id=chat_id,
                text=(
                    f"🔔 @{username}: "
                    f"{len(nuevas)} Story(s) nueva(s)."
                ),
                disable_notification=False,
            )

        procesadas = 0

        for historia in nuevas:
            # Segunda comprobación defensiva inmediatamente antes del envío.
            if db.historia_ya_enviada(
                chat_id,
                username,
                historia.story_pk,
            ):
                continue

            destino = ruta_para_historia(
                username,
                historia,
            )

            try:
                mensaje = await enviar_archivo(
                    context,
                    chat_id,
                    username,
                    historia,
                    destino,
                )
            except TelegramError:
                logger.exception(
                    "Telegram no pudo enviar Story %s de @%s",
                    historia.story_pk,
                    username,
                )
                continue

            # Se registra el message_id para el botón BORRAR MULTIMEDIA.
            try:
                db.registrar_multimedia_telegram(
                    chat_id,
                    int(mensaje.message_id),
                )
                db.registrar_multimedia_protegida_chat(
                    chat_id,
                    int(mensaje.message_id),
                )
                logger.info(
                    "Multimedia Telegram registrada: chat_id=%s message_id=%s Story=%s",
                    chat_id,
                    mensaje.message_id,
                    historia.story_pk,
                )
            except Exception:
                logger.exception(
                    "No se pudo registrar multimedia Telegram %s",
                    getattr(mensaje, "message_id", "?"),
                )

            # Sólo se marca como procesada después de que Telegram confirmó
            # correctamente el envío. Una falla de Telegram no bloquea una
            # Story nueva para el próximo intento.
            db.registrar_historia(
                chat_id,
                username,
                historia.story_pk,
            )

            procesadas += 1

        return procesadas


async def ejecucion_programada(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not context.job:
        return

    data = context.job.data or {}
    chat_id = int(data["chat_id"])
    username = str(data["username"])

    row = db.obtener_programacion(
        chat_id,
        username,
    )

    if row is None or not bool(row["activa"]):
        return

    try:
        await revisar_usuario(
            context,
            chat_id,
            username,
            manual=False,
        )
    except SinHistoriasDisponibles:
        logger.info("@%s sin Stories.", username)
    except Exception as error:
        logger.exception(
            "Error en revisión automática de @%s",
            username,
        )

        await avisar_error_chat(
            context,
            chat_id,
            username,
            error,
            automatico=True,
        )


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not autorizado(update):
        return

    registrar_texto_entrante(
        update
    )

    context.user_data.clear()

    await responder_texto(update,
        "📱 StoryPulse Web Private\n\n"
        "Seleccioná una opción:",
        reply_markup=menu_principal(),
    )


async def callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query

    if not query:
        return

    await query.answer()

    if not autorizado(update):
        return

    # El callback edita un mensaje ya existente. Lo registramos acá
    # para que incluso un menú creado por la versión anterior pueda
    # ser eliminado por LIMPIAR CHAT.
    if query.message is not None:
        registrar_mensaje_limpiable(
            int(query.message.chat.id),
            int(query.message.message_id),
        )

    data = query.data or ""

    if data == "menu":
        context.user_data.clear()
        await query.edit_message_text(
            "📱 StoryPulse Web Private\n\n"
            "Seleccioná una opción:",
            reply_markup=menu_principal(),
        )
        return

    if data == "stories_menu":
        context.user_data.clear()

        cuentas = cargar_cuentas()
        texto = (
            "👤 <b>Revisar historias</b>\n\n"
            "Elegí una cuenta:"
            if cuentas
            else
            "ℹ️ No hay cuentas configuradas."
        )

        await query.edit_message_text(
            texto,
            parse_mode="HTML",
            reply_markup=menu_revisar_historias(),
        )
        return

    if data == "publications_menu":
        context.user_data.clear()

        cuentas = cargar_cuentas()
        texto = (
            "📥 <b>Descargar publicaciones</b>\n\n"
            "Elegí una cuenta:"
            if cuentas
            else
            "ℹ️ No hay cuentas configuradas."
        )

        await query.edit_message_text(
            texto,
            parse_mode="HTML",
            reply_markup=menu_publicaciones(),
        )
        return

    if data.startswith("publications:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        if buscar_cuenta(username) is None:
            await query.edit_message_text(
                f"ℹ️ @{username} ya no está en las cuentas fijas.",
                reply_markup=menu_publicaciones(),
            )
            return

        await query.edit_message_text(
            f"⏳ Descargando publicaciones de @{username}...\n\n"
            "Preparando la revisión completa del perfil.\n"
            "El progreso se actualizará automáticamente cada 10 segundos."
        )

        progreso_lock = threading.Lock()
        progreso_estado = {
            "etapa": "iniciando",
            "total_perfil": None,
            "posts_encontrados": 0,
            "posts_procesados": 0,
            "publicaciones_nuevas": 0,
            "ya_descargadas": 0,
            "fallidas": 0,
            "archivos_guardados": 0,
            "consultas_graphql": 0,
        }
        inicio_progreso = time.monotonic()

        def recibir_progreso_publicaciones(datos: dict) -> None:
            with progreso_lock:
                datos = dict(datos)

                if datos.get("posts_encontrados") is not None:
                    datos["posts_encontrados"] = max(
                        int(progreso_estado.get("posts_encontrados") or 0),
                        int(datos.get("posts_encontrados") or 0),
                    )

                if datos.get("total_perfil") is not None:
                    anterior = progreso_estado.get("total_perfil")
                    actual = int(datos["total_perfil"])
                    datos["total_perfil"] = (
                        actual
                        if anterior is None
                        else max(int(anterior), actual)
                    )

                progreso_estado.update(datos)

        def texto_progreso_publicaciones() -> str:
            with progreso_lock:
                estado = dict(progreso_estado)

            transcurrido = max(
                0,
                int(time.monotonic() - inicio_progreso),
            )
            minutos, segundos = divmod(transcurrido, 60)
            tiempo_texto = (
                f"{minutos} min {segundos:02d} s"
                if minutos
                else f"{segundos} s"
            )

            etiquetas = {
                "iniciando": "Preparando navegador y sesión",
                "leyendo_perfil": "Leyendo el perfil",
                "recorriendo": "Recorriendo publicaciones del perfil",
                "procesando": "Descargando y guardando contenido",
                "finalizando": "Finalizando y guardando estado",
                "terminado": "Finalizando",
            }
            etapa = etiquetas.get(
                str(estado.get("etapa") or ""),
                "Procesando",
            )

            total = estado.get("total_perfil")
            encontrados = int(estado.get("posts_encontrados") or 0)
            procesados = int(estado.get("posts_procesados") or 0)
            nuevos = int(estado.get("publicaciones_nuevas") or 0)
            archivos = int(estado.get("archivos_guardados") or 0)
            fallidas = int(estado.get("fallidas") or 0)

            progreso_posts = (
                f"{encontrados}/{int(total)}"
                if total is not None
                else str(encontrados)
            )

            return (
                f"⏳ Descargando publicaciones de @{username}...\n\n"
                f"🟢 Estado: {etapa}\n"
                f"🕒 Tiempo: {tiempo_texto}\n"
                f"🔎 Posts localizados: {progreso_posts}\n"
                f"⚙️ Posts procesados: {procesados}\n"
                f"🆕 Publicaciones nuevas: {nuevos}\n"
                f"📁 Archivos guardados: {archivos}\n"
                f"⚠️ Fallidas/pendientes: {fallidas}\n\n"
                "Actualización automática cada 10 segundos."
            )

        async def refrescar_progreso_publicaciones() -> None:
            while True:
                await asyncio.sleep(10)
                try:
                    await query.edit_message_text(
                        texto_progreso_publicaciones()
                    )
                except TelegramError as error:
                    if "not modified" not in str(error).lower():
                        logger.warning(
                            "No se pudo refrescar el progreso de @%s: %s",
                            username,
                            error,
                        )

        tarea_progreso = asyncio.create_task(
            refrescar_progreso_publicaciones()
        )

        try:
            try:
                # Usa el mismo instagram_state.json que Stories. El lock evita
                # que dos operaciones de Instagram escriban el state a la vez.
                async with IG_LOCK:
                    resultado = await asyncio.to_thread(
                        descargar_publicaciones,
                        username,
                        AUTHORIZED_CHAT_ID,
                        progress_callback=recibir_progreso_publicaciones,
                    )
            finally:
                tarea_progreso.cancel()
                try:
                    await tarea_progreso
                except asyncio.CancelledError:
                    pass

            estado_sync = (
                "✅ completa"
                if resultado.sincronizacion_completa
                else "⚠️ incompleta"
            )
            corte = (
                "\nCorte por antirepetición: sí"
                if resultado.corte_por_antirepeticion
                else ""
            )

            if resultado.publicaciones_nuevas:
                texto = (
                    f"✅ <b>@{esc(username)}</b>\n\n"
                    f"Publicaciones del perfil: "
                    f"{resultado.publicaciones_totales_perfil if resultado.publicaciones_totales_perfil is not None else 'no detectado'}\n"
                    f"Posts recorridos: "
                    f"{resultado.publicaciones_detectadas}\n"
                    f"Publicaciones nuevas: "
                    f"{resultado.publicaciones_nuevas}\n"
                    f"Archivos guardados: "
                    f"{resultado.archivos_nuevos}\n"
                    f"Ya descargadas: "
                    f"{resultado.publicaciones_ya_descargadas}\n"
                    f"Fallidas/pendientes: "
                    f"{resultado.publicaciones_fallidas}\n"
                    f"Consultas GraphQL: "
                    f"{resultado.consultas_graphql}\n"
                    f"Sincronización histórica: {estado_sync}"
                    f"{corte}\n\n"
                    f"📁 <code>{esc(str(resultado.carpeta))}</code>"
                )
            else:
                texto = (
                    f"ℹ️ <b>@{esc(username)}</b>: "
                    "no hay publicaciones nuevas.\n\n"
                    f"Publicaciones del perfil: "
                    f"{resultado.publicaciones_totales_perfil if resultado.publicaciones_totales_perfil is not None else 'no detectado'}\n"
                    f"Posts recorridos: "
                    f"{resultado.publicaciones_detectadas}\n"
                    f"Ya descargadas: "
                    f"{resultado.publicaciones_ya_descargadas}\n"
                    f"Fallidas/pendientes: "
                    f"{resultado.publicaciones_fallidas}\n"
                    f"Consultas GraphQL: "
                    f"{resultado.consultas_graphql}\n"
                    f"Sincronización histórica: {estado_sync}"
                    f"{corte}\n\n"
                    f"📁 <code>{esc(str(resultado.carpeta))}</code>"
                )

            await query.edit_message_text(
                texto,
                parse_mode="HTML",
                reply_markup=menu_publicaciones(),
            )

        except Exception as error:
            logger.exception(
                "Error descargando publicaciones de @%s",
                username,
            )

            icono, tipo, recomendacion = diagnosticar_error(
                error
            )
            detalle = str(error).strip() or repr(error)
            if BOT_TOKEN:
                detalle = detalle.replace(
                    BOT_TOKEN,
                    "[TOKEN OCULTO]",
                )
            detalle = detalle[:900]

            await query.edit_message_text(
                (
                    f"{icono} <b>ERROR EN PUBLICACIONES</b>\n\n"
                    f"Cuenta: <b>@{esc(username)}</b>\n"
                    f"Tipo: <b>{esc(tipo)}</b>\n\n"
                    f"<code>{esc(detalle)}</code>\n\n"
                    f"💡 {esc(recomendacion)}"
                ),
                parse_mode="HTML",
                reply_markup=menu_publicaciones(),
            )

        return

    if data == "manage":
        context.user_data.clear()
        await query.edit_message_text(
            "👤 Gestión de cuentas",
            reply_markup=menu_gestion(),
        )
        return

    if data == "dedupe_reset_menu":
        context.user_data.clear()
        cuentas = cargar_cuentas()
        texto = (
            "♻️ <b>Reiniciar antirepetición</b>\n\n"
            "Elegí una cuenta. Se eliminarán solamente los IDs "
            "registrados de Stories y publicaciones de ese perfil."
            if cuentas
            else
            "ℹ️ No hay cuentas configuradas."
        )
        await query.edit_message_text(
            texto,
            parse_mode="HTML",
            reply_markup=menu_reiniciar_antirepeticion(),
        )
        return

    if data.startswith("dedupe_reset_select:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )
        cuenta = buscar_cuenta(username)
        if cuenta is None:
            await query.edit_message_text(
                f"ℹ️ @{username} ya no está en las cuentas configuradas.",
                reply_markup=menu_reiniciar_antirepeticion(),
            )
            return

        await query.edit_message_text(
            (
                f"⚠️ <b>Reiniciar antirepetición de @{esc(username)}</b>\n\n"
                "Esto olvidará únicamente los IDs registrados de este perfil:\n"
                "• Stories procesadas\n"
                "• Publicaciones descargadas\n\n"
                "No borra archivos, cuenta, programación ni sesión.\n\n"
                "La próxima descarga de publicaciones volverá a recorrer "
                "el historial completo disponible."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "♻️ Sí, reiniciar este perfil",
                            callback_data=f"dedupe_reset_confirm:{username}",
                            style="danger",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Cancelar",
                            callback_data="dedupe_reset_menu",
                        )
                    ],
                ]
            ),
        )
        return

    if data.startswith("dedupe_reset_confirm:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )
        cuenta = buscar_cuenta(username)
        if cuenta is None:
            await query.edit_message_text(
                f"ℹ️ @{username} ya no está en las cuentas configuradas.",
                reply_markup=menu_reiniciar_antirepeticion(),
            )
            return

        async with STORY_PROCESS_LOCK:
            eliminados = db.limpiar_antirepeticion_perfil(
                AUTHORIZED_CHAT_ID,
                username,
            )

        await query.edit_message_text(
            (
                f"✅ <b>@{esc(username)}</b> reiniciada.\n\n"
                f"IDs de Stories eliminados: "
                f"{eliminados['historias']}\n"
                f"IDs de publicaciones eliminados: "
                f"{eliminados['publicaciones']}\n\n"
                "Los archivos existentes no fueron eliminados.\n"
                "La próxima descarga podrá procesar nuevamente este perfil."
            ),
            parse_mode="HTML",
            reply_markup=menu_reiniciar_antirepeticion(),
        )
        return

    if data == "add":
        context.user_data.clear()
        context.user_data[STATE] = ADD_NAME

        await query.edit_message_text(
            "➕ Agregar cuenta\n\n"
            "Escribí el nombre visible.\n"
            "Ejemplo: Leo Messi"
        )
        return

    if data == "list_accounts":
        cuentas = cargar_cuentas()

        if not cuentas:
            texto = "ℹ️ No hay cuentas configuradas."
        else:
            lineas = ["📋 <b>Cuentas</b>", ""]

            for cuenta in cuentas:
                uid = cuenta.get("user_id") or "sin resolver"
                lineas.append(
                    f"• {esc(cuenta['nombre'])} — "
                    f"@{esc(cuenta['username'])} — ID {uid}"
                )

            texto = "\n".join(lineas)

        await query.edit_message_text(
            texto,
            parse_mode="HTML",
            reply_markup=menu_gestion(),
        )
        return

    if data == "remove":
        cuentas = cargar_cuentas()

        filas = [
            [
                InlineKeyboardButton(
                    f"🗑 {c['nombre']}",
                    callback_data=f"remove_do:{c['username']}",
                )
            ]
            for c in cuentas
        ]

        filas.append(
            [
                InlineKeyboardButton(
                    "‹ Volver",
                    callback_data="manage",
                )
            ]
        )

        await query.edit_message_text(
            "➖ Elegí la cuenta a quitar:",
            reply_markup=InlineKeyboardMarkup(filas),
        )
        return

    if data.startswith("remove_do:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        cuentas = [
            c
            for c in cargar_cuentas()
            if c["username"].casefold()
            != username.casefold()
        ]

        guardar_cuentas(cuentas)

        db.eliminar_programacion(
            AUTHORIZED_CHAT_ID,
            username,
        )

        eliminar_jobs_usuario(
            context.application,
            AUTHORIZED_CHAT_ID,
            username,
        )

        await query.edit_message_text(
            f"✅ @{username} eliminada.",
            reply_markup=menu_gestion(),
        )
        return

    if data.startswith("review:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        await query.edit_message_text(
            f"⏳ Revisando @{username}..."
        )

        try:
            cantidad = await revisar_usuario(
                context,
                AUTHORIZED_CHAT_ID,
                username,
                manual=True,
            )

            await enviar_texto_bot(context,
                chat_id=AUTHORIZED_CHAT_ID,
                text=(
                    f"✅ @{username}: "
                    f"{cantidad} Story(s) nueva(s) procesada(s)."
                ),
                reply_markup=menu_principal(),
                disable_notification=True,
            )

        except SinHistoriasDisponibles:
            await enviar_texto_bot(context,
                chat_id=AUTHORIZED_CHAT_ID,
                text=f"ℹ️ @{username} no tiene Stories visibles.",
                reply_markup=menu_principal(),
                disable_notification=True,
            )

        except Exception as error:
            logger.exception(
                "Error revisando @%s",
                username,
            )

            await avisar_error_chat(
                context,
                AUTHORIZED_CHAT_ID,
                username,
                error,
                automatico=False,
            )

            await enviar_texto_bot(context,
                chat_id=AUTHORIZED_CHAT_ID,
                text="Menú:",
                reply_markup=menu_principal(),
                disable_notification=True,
            )

        return

    if data == "schedules":
        context.user_data.clear()

        disponibles = (
            cuentas_disponibles_para_programar()
        )

        if disponibles:
            texto = (
                "⚙️ <b>Programar revisión</b>\n\n"
                "Elegí una cuenta:"
            )
        else:
            texto = (
                "✅ <b>No hay cuentas disponibles</b>\n\n"
                "Todas las cuentas agregadas ya tienen "
                "una programación activa.\n\n"
                "Podés verlas, pausarlas o eliminarlas "
                "desde <b>Ver programaciones</b>."
            )

        await query.edit_message_text(
            texto,
            parse_mode="HTML",
            reply_markup=menu_programaciones(),
        )
        return

    if data.startswith("sched:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        existente = db.obtener_programacion(
            AUTHORIZED_CHAT_ID,
            username,
        )

        if (
            existente is not None
            and bool(existente["activa"])
        ):
            context.user_data.clear()

            await query.edit_message_text(
                (
                    f"ℹ️ <b>@{esc(username)}</b> ya tiene "
                    "una programación activa.\n\n"
                    "No se creó una programación duplicada."
                ),
                parse_mode="HTML",
                reply_markup=menu_programaciones(),
            )
            return

        context.user_data.clear()
        context.user_data[SCHED_USERNAME] = username

        await query.edit_message_text(
            f"⏰ @{username}\n\n"
            "¿Cómo querés programar las revisiones?",
            reply_markup=menu_tipo_programacion(
                username
            ),
        )
        return

    if data.startswith("sched_mode_daily:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        context.user_data.clear()
        context.user_data[SCHED_USERNAME] = username

        await query.edit_message_text(
            f"📅 @{username}\n\n"
            "¿Cuántas revisiones por día?",
            reply_markup=menu_cantidad(
                username
            ),
        )
        return

    if data.startswith("sched_mode_interval:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        context.user_data.clear()
        context.user_data[SCHED_USERNAME] = username

        await query.edit_message_text(
            f"⏱ @{username}\n\n"
            "Elegí cada cuánto tiempo revisar.\n\n"
            "Después vas a elegir el minuto exacto de la hora "
            "en que querés hacer las revisiones.",
            reply_markup=menu_intervalos(
                username
            ),
        )
        return

    if data.startswith("sched_interval:"):
        _, username, horas = data.split(
            ":",
            2,
        )
        username = limpiar_username(
            username
        )
        horas = int(
            horas
        )

        if not 1 <= horas <= 12:
            await query.edit_message_text(
                "❌ Intervalo inválido.",
                reply_markup=menu_programaciones(),
            )
            return

        context.user_data.clear()
        context.user_data[STATE] = SCHED_INTERVAL_MINUTE
        context.user_data[SCHED_USERNAME] = username
        context.user_data[SCHED_INTERVAL_HOURS] = horas

        texto_intervalo = (
            "Cada 1 hora"
            if horas == 1
            else f"Cada {horas} horas"
        )

        await query.edit_message_text(
            (
                f"⏱ @{username}\n\n"
                f"Intervalo: {texto_intervalo}\n\n"
                "¿En qué minuto de la hora querés hacer la revisión?\n\n"
                "Escribí un número del 0 al 59.\n"
                "Ejemplo: 46"
            )
        )
        return

    if data.startswith("sched_count:"):
        _, username, cantidad = data.split(":", 2)
        username = limpiar_username(username)
        cantidad = int(cantidad)

        context.user_data.clear()
        context.user_data[STATE] = SCHED_TIMES
        context.user_data[SCHED_USERNAME] = username
        context.user_data[SCHED_COUNT] = cantidad
        context.user_data["times"] = []

        await query.edit_message_text(
            f"⏰ @{username}\n\n"
            f"Escribí el horario 1 de {cantidad}.\n"
            "Formato HH:MM, hora de Argentina.\n"
            "Ejemplo: 21:30"
        )
        return

    if data == "sched_list":
        rows = db.listar_programaciones(
            AUTHORIZED_CHAT_ID
        )

        if not rows:
            await query.edit_message_text(
                "ℹ️ No hay programaciones.",
                reply_markup=menu_programaciones(),
            )
            return

        await query.edit_message_text(
            (
                "📋 <b>Programaciones</b>\n\n"
                "Elegí una cuenta para ver su programación:"
            ),
            parse_mode="HTML",
            reply_markup=menu_lista_programaciones(
                rows
            ),
        )
        return

    if data == "sched_delete_all":
        rows = db.listar_programaciones(
            AUTHORIZED_CHAT_ID
        )

        if not rows:
            await query.edit_message_text(
                "ℹ️ No hay programaciones para eliminar.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "‹ Volver al menú",
                                callback_data="menu",
                            )
                        ]
                    ]
                ),
            )
            return

        activas = sum(
            1
            for row in rows
            if bool(row["activa"])
        )
        total = len(rows)

        texto = (
            "⚠️ <b>ELIMINAR TODAS LAS PROGRAMACIONES</b>\n\n"
            f"Existen <b>{activas}</b> programaciones activas."
        )

        if total != activas:
            texto += (
                f"\nProgramaciones guardadas en total: <b>{total}</b>."
            )

        texto += (
            "\n\n¿Estás seguro de que querés borrarlas todas?\n\n"
            "Esta acción eliminará también cualquier programación pausada."
        )

        await query.edit_message_text(
            texto,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗑 SÍ, ELIMINAR TODAS",
                            callback_data="sched_delete_all_confirm",
                            style="danger",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Cancelar",
                            callback_data="menu",
                        )
                    ],
                ]
            ),
        )
        return

    if data == "sched_delete_all_confirm":
        rows = db.listar_programaciones(
            AUTHORIZED_CHAT_ID
        )

        if not rows:
            await query.edit_message_text(
                "ℹ️ No hay programaciones para eliminar.",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "‹ Volver al menú",
                                callback_data="menu",
                            )
                        ]
                    ]
                ),
            )
            return

        activas = sum(
            1
            for row in rows
            if bool(row["activa"])
        )
        total = len(rows)

        for row in rows:
            username = str(row["username"])

            eliminar_jobs_usuario(
                context.application,
                AUTHORIZED_CHAT_ID,
                username,
            )

            db.eliminar_programacion(
                AUTHORIZED_CHAT_ID,
                username,
            )

        context.user_data.clear()

        await query.edit_message_text(
            (
                "✅ <b>Todas las programaciones fueron eliminadas.</b>\n\n"
                f"Programaciones eliminadas: <b>{total}</b>\n"
                f"Programaciones activas canceladas: <b>{activas}</b>"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "‹ Volver al menú",
                            callback_data="menu",
                        )
                    ]
                ]
            ),
        )
        return

    if data.startswith("sched_detail:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        row = db.obtener_programacion(
            AUTHORIZED_CHAT_ID,
            username,
        )

        if row is None:
            rows = db.listar_programaciones(
                AUTHORIZED_CHAT_ID
            )

            if rows:
                await query.edit_message_text(
                    (
                        "ℹ️ Esa programación ya no existe.\n\n"
                        "Elegí otra cuenta:"
                    ),
                    reply_markup=menu_lista_programaciones(
                        rows
                    ),
                )
            else:
                await query.edit_message_text(
                    "ℹ️ No hay programaciones.",
                    reply_markup=menu_programaciones(),
                )
            return

        await query.edit_message_text(
            texto_detalle_programacion(
                row
            ),
            parse_mode="HTML",
            reply_markup=menu_detalle_programacion(
                row
            ),
        )
        return

    if data.startswith("sched_toggle:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        row = db.obtener_programacion(
            AUTHORIZED_CHAT_ID,
            username,
        )

        if row is None:
            await query.edit_message_text(
                "❌ La programación ya no existe.",
                reply_markup=menu_programaciones(),
            )
            return

        nueva = not bool(
            row["activa"]
        )

        db.cambiar_estado_programacion(
            AUTHORIZED_CHAT_ID,
            username,
            nueva,
        )

        if nueva:
            row_actualizada = (
                db.obtener_programacion(
                    AUTHORIZED_CHAT_ID,
                    username,
                )
                or row
            )

            registrar_programacion_guardada(
                context.application,
                row_actualizada,
            )
        else:
            eliminar_jobs_usuario(
                context.application,
                AUTHORIZED_CHAT_ID,
                username,
            )

        row_actualizada = db.obtener_programacion(
            AUTHORIZED_CHAT_ID,
            username,
        )

        if row_actualizada is None:
            await query.edit_message_text(
                "❌ La programación ya no existe.",
                reply_markup=menu_programaciones(),
            )
            return

        await query.edit_message_text(
            texto_detalle_programacion(
                row_actualizada
            ),
            parse_mode="HTML",
            reply_markup=menu_detalle_programacion(
                row_actualizada
            ),
        )
        return

    if data.startswith("sched_notify:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        row = db.obtener_programacion(
            AUTHORIZED_CHAT_ID,
            username,
        )

        if row is None:
            await query.edit_message_text(
                "❌ La programación ya no existe.",
                reply_markup=menu_programaciones(),
            )
            return

        nueva = not db.notificacion_activada(
            row
        )

        db.cambiar_notificacion_programacion(
            AUTHORIZED_CHAT_ID,
            username,
            nueva,
        )

        row_actualizada = db.obtener_programacion(
            AUTHORIZED_CHAT_ID,
            username,
        )

        if row_actualizada is None:
            await query.edit_message_text(
                "❌ La programación ya no existe.",
                reply_markup=menu_programaciones(),
            )
            return

        await query.edit_message_text(
            texto_detalle_programacion(
                row_actualizada
            ),
            parse_mode="HTML",
            reply_markup=menu_detalle_programacion(
                row_actualizada
            ),
        )
        return

    if data.startswith("sched_delete:"):
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        db.eliminar_programacion(
            AUTHORIZED_CHAT_ID,
            username,
        )

        eliminar_jobs_usuario(
            context.application,
            AUTHORIZED_CHAT_ID,
            username,
        )

        rows = db.listar_programaciones(
            AUTHORIZED_CHAT_ID
        )

        if rows:
            await query.edit_message_text(
                (
                    f"✅ Programación de @{esc(username)} eliminada.\n\n"
                    "📋 <b>Programaciones</b>\n\n"
                    "Elegí una cuenta para ver su programación:"
                ),
                parse_mode="HTML",
                reply_markup=menu_lista_programaciones(
                    rows
                ),
            )
        else:
            await query.edit_message_text(
                (
                    f"✅ Programación de @{esc(username)} eliminada.\n\n"
                    "ℹ️ No quedan programaciones."
                ),
                parse_mode="HTML",
                reply_markup=menu_programaciones(),
            )
        return

    if data == "chat_clean":
        chat_id = int(
            update.effective_chat.id
        )

        protegidos = db.ids_multimedia_protegida_chat(
            chat_id
        )

        mensaje_actual_id = (
            int(query.message.message_id)
            if query.message is not None
            else 0
        )

        await query.edit_message_text(
            (
                "🧹 <b>LIMPIAR CHAT COMPLETO</b>\n\n"
                "Se hará una limpieza general del chat:\n\n"
                "🗑 textos\n"
                "🗑 comandos /start\n"
                "🗑 avisos\n"
                "🗑 mensajes de error\n"
                "🗑 menús actuales y anteriores\n\n"
                f"📸 Multimedia protegida conocida: <b>{len(protegidos)}</b>\n\n"
                "✅ Fotos/videos protegidos NO se tocarán.\n"
                "✅ /historys NO se toca.\n"
                "✅ La antirepetición NO se toca.\n\n"
                "Telegram sólo permite borrar mensajes recientes "
                "(normalmente hasta 48 horas).\n\n"
                "¿Continuar?"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🧹 Sí, limpiar TODO menos multimedia",
                            callback_data="chat_clean_confirm",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Cancelar",
                            callback_data="menu",
                        )
                    ],
                ]
            ),
        )
        return

    if data == "chat_clean_confirm":
        chat_id = int(
            update.effective_chat.id
        )

        if query.message is None:
            await enviar_texto_bot(
                context,
                chat_id=chat_id,
                text="❌ No pude determinar el mensaje actual del chat.",
                disable_notification=True,
            )
            return

        max_message_id = int(
            query.message.message_id
        )

        # Protegemos toda la multimedia conocida.
        protegidos = db.ids_multimedia_protegida_chat(
            chat_id
        )

        # También nos aseguramos de proteger toda la multimedia que
        # StoryPulse ya había registrado en versiones anteriores.
        protegidos.update(
            db.multimedia_reciente_telegram(
                chat_id,
                limite=100,
                horas=48,
            )
        )

        # El chat de este bot es pequeño, pero ponemos un techo alto
        # para evitar recorrer cantidades absurdas de IDs.
        MAX_IDS_A_RECORRER = 5000

        min_message_id = max(
            1,
            max_message_id - MAX_IDS_A_RECORRER + 1,
        )

        candidatos = [
            message_id
            for message_id in range(
                min_message_id,
                max_message_id + 1,
            )
            if message_id not in protegidos
        ]

        # Procesamos desde los IDs más recientes hacia atrás.
        candidatos.sort(
            reverse=True
        )

        borrados_estimados = 0
        ya_inexistentes = 0
        fallidos = 0
        detenidos_por_antiguedad = False

        async def borrar_individual(
            message_id: int,
        ) -> str:
            try:
                resultado = await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=int(message_id),
                )

                if resultado:
                    return "borrado"

                return "fallido"

            except TelegramError as error:
                detalle = str(error).lower()

                if (
                    "message to delete not found" in detalle
                    or "message not found" in detalle
                ):
                    return "inexistente"

                if (
                    "message can't be deleted" in detalle
                    or "message cannot be deleted" in detalle
                    or "48 hours" in detalle
                ):
                    return "antiguo"

                logger.warning(
                    "LIMPIAR CHAT GENERAL: no se pudo borrar "
                    "chat_id=%s message_id=%s: %s",
                    chat_id,
                    message_id,
                    error,
                )
                return "fallido"

        # Lotes de 100. Telegram puede omitir IDs inexistentes.
        # Si un lote falla, bajamos a borrado individual para ese lote.
        for inicio in range(
            0,
            len(candidatos),
            100,
        ):
            lote_desc = candidatos[
                inicio:inicio + 100
            ]

            if not lote_desc:
                continue

            lote = sorted(
                lote_desc
            )

            try:
                resultado = await context.bot.delete_messages(
                    chat_id=chat_id,
                    message_ids=lote,
                )

                if resultado:
                    # Telegram considera exitoso el lote aunque algunos
                    # IDs ya no existan. Para el objetivo de limpieza
                    # nos sirve como procesado.
                    borrados_estimados += len(lote)
                    continue

            except TelegramError as error_lote:
                logger.info(
                    "Lote de limpieza rechazado; "
                    "se intentará individualmente. "
                    "IDs %s-%s: %s",
                    min(lote),
                    max(lote),
                    error_lote,
                )

            antiguos_en_lote = 0
            borrados_en_lote = 0

            for message_id in lote_desc:
                estado = await borrar_individual(
                    message_id
                )

                if estado == "borrado":
                    borrados_estimados += 1
                    borrados_en_lote += 1

                elif estado == "inexistente":
                    ya_inexistentes += 1

                elif estado == "antiguo":
                    antiguos_en_lote += 1

                else:
                    fallidos += 1

                await asyncio.sleep(0.035)

            # IDs menores son más antiguos. Si llegamos a un bloque
            # completo donde prácticamente todo ya supera el límite
            # de Telegram y no borramos nada, no tiene sentido seguir.
            if (
                borrados_en_lote == 0
                and antiguos_en_lote >= max(
                    20,
                    int(len(lote_desc) * 0.80),
                )
            ):
                detenidos_por_antiguedad = True
                break

        # La tabla V2.3 ya no es necesaria para descubrir menús viejos,
        # pero limpiamos sus registros vencidos para mantener la DB sana.
        db.purgar_registros_chat_limpiables_vencidos(
            chat_id,
            horas=48,
        )

        texto_final = (
            "🧹 <b>Chat limpiado</b>\n\n"
            "✅ Se procesó la limpieza general del chat.\n"
            f"📸 Multimedia protegida: <b>{len(protegidos)}</b>\n"
            f"⚠️ Fallos puntuales: <b>{fallidos}</b>"
        )

        if detenidos_por_antiguedad:
            texto_final += (
                "\n\nℹ️ Se alcanzaron mensajes demasiado antiguos "
                "para que Telegram permita borrarlos."
            )

        texto_final += (
            "\n\n📸 Las fotos/videos protegidos quedaron intactos."
        )

        await enviar_texto_bot(
            context,
            chat_id=chat_id,
            text=texto_final,
            parse_mode="HTML",
            disable_notification=True,
        )
        return

    if data == "media_delete":
        chat_id = int(update.effective_chat.id)

        message_ids = db.multimedia_reciente_telegram(
            chat_id,
            limite=100,
            horas=48,
        )

        cantidad = len(message_ids)

        await query.edit_message_text(
            (
                "🗑 <b>BORRAR MULTIMEDIA</b>\n\n"
                f"Fotos/videos recientes registrados: <b>{cantidad}</b>\n\n"
                "Se borrarán como máximo 100 archivos multimedia "
                "enviados por este bot durante las últimas 48 horas.\n\n"
                "✅ Los archivos guardados en /historys NO se borran.\n"
                "✅ La antirepetición NO se borra.\n"
                "✅ El panel NO se modifica.\n\n"
                "¿Continuar?"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗑 Sí, borrar multimedia",
                            callback_data="media_delete_confirm",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Cancelar",
                            callback_data="menu",
                        )
                    ],
                ]
            ),
        )
        return

    if data == "media_delete_confirm":
        chat_id = int(update.effective_chat.id)

        message_ids = db.multimedia_reciente_telegram(
            chat_id,
            limite=100,
            horas=48,
        )

        if not message_ids:
            await query.edit_message_text(
                "ℹ️ No hay multimedia reciente registrada.",
                reply_markup=menu_principal(),
            )
            return

        # Avisamos inmediatamente para que Telegram no parezca congelado.
        try:
            await query.edit_message_text(
                (
                    "🗑 <b>Borrando multimedia...</b>\n\n"
                    f"Archivos a procesar: <b>{len(message_ids)}</b>"
                ),
                parse_mode="HTML",
            )
        except TelegramError:
            pass

        eliminados: list[int] = []
        ya_no_existen: list[int] = []
        fallidos: list[tuple[int, str]] = []

        # Se hace uno por uno para saber exactamente qué ID aceptó
        # Telegram y no depender de una respuesta global del lote.
        for posicion, message_id in enumerate(
            message_ids,
            start=1,
        ):
            try:
                resultado = await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=int(message_id),
                )

                if resultado:
                    eliminados.append(
                        int(message_id)
                    )
                    logger.info(
                        "Multimedia Telegram borrada: chat_id=%s message_id=%s",
                        chat_id,
                        message_id,
                    )
                else:
                    fallidos.append(
                        (
                            int(message_id),
                            "Telegram devolvió False",
                        )
                    )

            except TelegramError as error:
                detalle = str(error)
                detalle_lower = detalle.lower()

                # Si Telegram dice que el mensaje ya no existe,
                # limpiamos también el registro local porque no hay
                # nada más que borrar.
                if (
                    "message to delete not found" in detalle_lower
                    or "message not found" in detalle_lower
                ):
                    ya_no_existen.append(
                        int(message_id)
                    )
                    logger.info(
                        "Multimedia ya inexistente: chat_id=%s message_id=%s",
                        chat_id,
                        message_id,
                    )
                else:
                    fallidos.append(
                        (
                            int(message_id),
                            detalle[:250],
                        )
                    )
                    logger.warning(
                        "No se pudo borrar multimedia Telegram "
                        "chat_id=%s message_id=%s: %s",
                        chat_id,
                        message_id,
                        detalle,
                    )

            # Evita disparar muchas operaciones seguidas cuando hay
            # decenas de mensajes registrados.
            if posicion < len(message_ids):
                await asyncio.sleep(0.08)

        limpiar_ids = [
            *eliminados,
            *ya_no_existen,
        ]

        if limpiar_ids:
            db.eliminar_registros_multimedia_telegram(
                chat_id,
                limpiar_ids,
            )

        texto = (
            "✅ <b>Limpieza de multimedia terminada</b>\n\n"
            f"🗑 Borradas: <b>{len(eliminados)}</b>\n"
            f"ℹ️ Ya no existían: <b>{len(ya_no_existen)}</b>\n"
            f"⚠️ No se pudieron borrar: <b>{len(fallidos)}</b>\n\n"
            "💾 Los archivos de /historys siguen intactos."
        )

        if fallidos:
            # Mostrar sólo el primer error evita llenar el chat.
            primer_id, primer_error = fallidos[0]
            texto += (
                "\n\n<b>Primer error:</b>\n"
                f"<code>ID {primer_id}: "
                f"{esc(primer_error)}</code>"
            )

        await enviar_texto_bot(context,
            chat_id=chat_id,
            text=texto,
            parse_mode="HTML",
            reply_markup=menu_principal(),
            disable_notification=True,
        )
        return

    if data == "status":
        await query.edit_message_text(
            "⏳ <b>Verificando sesión web...</b>",
            parse_mode="HTML",
        )

        cuentas = cargar_cuentas()
        programaciones = db.listar_programaciones(
            AUTHORIZED_CHAT_ID
        )

        try:
            async with IG_LOCK:
                sesion_web = await asyncio.to_thread(
                    _comprobar_sesion_web_real
                )

            username_sesion = sesion_web.get("username")
            if username_sesion:
                cuenta_linea = (
                    f"Sesión autenticada como: "
                    f"<b>@{esc(username_sesion)}</b>\n"
                )
            else:
                cuenta_linea = (
                    "Sesión autenticada como: "
                    "<i>no se pudo detectar el username</i>\n"
                )

            texto_estado = (
                "ℹ️ <b>ESTADO</b>\n\n"
                "Sesión web: ✅ <b>cargada</b>\n"
                f"{cuenta_linea}"
                "Acceso al feed: ✅ <b>normal</b> "
                f"(HTTP {int(sesion_web.get('http_status', 200))})\n"
                "Estado de seguridad: ✅ "
                "<b>sin challenge/CAPTCHA detectado</b>\n"
                "Sesión guardada/actualizada hace: "
                f"<b>{esc(sesion_web.get('antiguedad_archivo', 'no disponible'))}</b>\n\n"
                f"Cuentas: <b>{len(cuentas)}</b>\n"
                f"Programaciones: <b>{len(programaciones)}</b>\n"
                f"Historias: <code>{esc(str(HISTORYS_DIR))}</code>"
            )

        except Exception as error:
            logger.warning(
                "Verificación de sesión web falló: %s",
                error,
            )
            texto_estado = (
                "ℹ️ <b>ESTADO</b>\n\n"
                "Sesión web: ❌ <b>requiere atención</b>\n"
                "Acceso al feed: ❌ <b>falló la verificación</b>\n"
                f"Detalle: <code>{esc(str(error)[:500])}</code>\n"
                "Sesión guardada/actualizada hace: "
                f"<b>{esc(_antiguedad_archivo_sesion())}</b>\n\n"
                f"Cuentas: <b>{len(cuentas)}</b>\n"
                f"Programaciones: <b>{len(programaciones)}</b>\n"
                f"Historias: <code>{esc(str(HISTORYS_DIR))}</code>"
            )

        await query.edit_message_text(
            texto_estado,
            parse_mode="HTML",
            reply_markup=menu_estado(),
        )
        return


async def registrar_multimedia_entrante(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Protege fotos/videos/documentos multimedia que el usuario mande
    al chat para que LIMPIAR CHAT nunca los toque.
    """
    if not autorizado(update):
        return

    mensaje = update.effective_message

    if mensaje is None:
        return

    try:
        db.registrar_multimedia_protegida_chat(
            int(mensaje.chat.id),
            int(mensaje.message_id),
        )
        logger.info(
            "Multimedia entrante protegida: chat_id=%s message_id=%s",
            mensaje.chat.id,
            mensaje.message_id,
        )
    except Exception:
        logger.exception(
            "No se pudo registrar multimedia entrante protegida."
        )


async def recibir_texto(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not autorizado(update):
        return

    registrar_texto_entrante(
        update
    )

    texto = (
        update.effective_message.text or ""
    ).strip()

    estado = context.user_data.get(STATE)

    if estado == ADD_NAME:
        if not texto:
            return

        context.user_data["new_name"] = texto[:48]
        context.user_data[STATE] = ADD_USERNAME

        await responder_texto(update,
            "Ahora escribí el username de Instagram.\n"
            "Ejemplo: leo_messi"
        )
        return

    if estado == ADD_USERNAME:
        try:
            username = limpiar_username(texto)
        except ValueError as error:
            await responder_texto(update,
                f"❌ {error}"
            )
            return

        if buscar_cuenta(username):
            context.user_data.clear()

            await responder_texto(update,
                f"ℹ️ @{username} ya existe.",
                reply_markup=menu_principal(),
            )
            return

        await responder_texto(update,
            f"⏳ Verificando acceso a @{username}..."
        )

        try:
            async with IG_LOCK:
                perfil = await asyncio.to_thread(
                    comprobar_perfil_accesible,
                    username,
                )

                # Criterio estricto para perfiles privados:
                # sólo se agrega si la propia comprobación de acceso confirma
                # explícitamente following=True. Si devuelve False o None,
                # se considera que la sesión no tiene acceso suficiente.
                if (
                    bool(perfil.get("is_private"))
                    and perfil.get("following") is not True
                ):
                    raise PerfilPrivado(
                        f"La sesión autenticada no sigue a @{username}."
                    )

            user_id = int(perfil["user_id"])
            username = limpiar_username(
                str(perfil.get("username") or username)
            )

        except PerfilPrivado:
            logger.info(
                "No se agregó @%s: perfil privado sin acceso.",
                username,
            )
            context.user_data.clear()

            await responder_texto(
                update,
                (
                    f"🔒 La cuenta que querés añadir, @{username}, "
                    "es privada.\n\n"
                    "La sesión de Instagram no tiene acceso a ese perfil, "
                    "por lo tanto no fue agregada."
                ),
                reply_markup=menu_gestion(),
            )
            return

        except RuntimeError as error:
            detalle = str(error)

            if (
                "No pude confirmar si el perfil es público o privado"
                in detalle
            ):
                logger.info(
                    "No se agregó @%s: perfil privado o sin acceso confirmado.",
                    username,
                )
                context.user_data.clear()

                await responder_texto(
                    update,
                    (
                        f"🔒 No se puede añadir @{username}.\n\n"
                        "El perfil es privado o la sesión de Instagram "
                        "no tiene acceso a esa cuenta.\n\n"
                        "La cuenta no fue agregada."
                    ),
                    reply_markup=menu_gestion(),
                )
                return

            logger.exception(
                "Error verificando acceso al agregar @%s",
                username,
            )

            await avisar_error_chat(
                context,
                AUTHORIZED_CHAT_ID,
                username,
                error,
                automatico=False,
            )
            return

        except Exception as error:
            logger.exception(
                "Error verificando acceso al agregar @%s",
                username,
            )

            await avisar_error_chat(
                context,
                AUTHORIZED_CHAT_ID,
                username,
                error,
                automatico=False,
            )
            return

        cuentas = cargar_cuentas()
        cuentas.append(
            {
                "nombre": context.user_data.get(
                    "new_name",
                    username,
                ),
                "username": username,
                "user_id": int(user_id),
            }
        )
        guardar_cuentas(cuentas)
        context.user_data.clear()

        await responder_texto(update,
            f"✅ @{username} agregada.\n"
            f"Instagram ID: {user_id}",
            reply_markup=menu_principal(),
        )
        return

    if estado == SCHED_INTERVAL_MINUTE:
        try:
            minuto = int(texto)
        except ValueError:
            minuto = -1

        if not 0 <= minuto <= 59:
            await responder_texto(
                update,
                "❌ Minuto inválido. Escribí un número del 0 al 59.\n"
                "Ejemplo: 46",
            )
            return

        username = str(
            context.user_data[SCHED_USERNAME]
        )
        horas = int(
            context.user_data[SCHED_INTERVAL_HOURS]
        )

        # Evita que dos programaciones activas compartan el mismo minuto.
        # Se revisan tanto los intervalos como los horarios fijos existentes.
        # Las programaciones pausadas no reservan ningún minuto.
        conflicto_username: str | None = None
        conflicto_detalle: str | None = None

        for row_existente in db.listar_programaciones(
            AUTHORIZED_CHAT_ID
        ):
            if not bool(row_existente["activa"]):
                continue

            username_existente = str(
                row_existente["username"]
            )

            if (
                username_existente.casefold()
                == username.casefold()
            ):
                continue

            if db.tipo_programacion(row_existente) == "intervalo":
                inicio_existente = db.inicio_intervalo_de(
                    row_existente
                )

                if inicio_existente is None:
                    continue

                minuto_existente = inicio_existente.astimezone(
                    TZ
                ).minute

                if minuto_existente == minuto:
                    horas_existentes = (
                        db.intervalo_horas_de(row_existente)
                        or 0
                    )
                    conflicto_username = username_existente
                    conflicto_detalle = (
                        "cada 1 hora"
                        if horas_existentes == 1
                        else f"cada {horas_existentes} horas"
                    )
                    break

            else:
                for horario_existente in db.horarios_de(
                    row_existente
                ):
                    try:
                        _, minuto_texto = horario_existente.split(
                            ":",
                            1,
                        )
                        minuto_existente = int(minuto_texto)
                    except (ValueError, AttributeError):
                        continue

                    if minuto_existente == minuto:
                        conflicto_username = username_existente
                        conflicto_detalle = (
                            f"horario fijo {horario_existente}"
                        )
                        break

                if conflicto_username is not None:
                    break

        if conflicto_username is not None:
            await responder_texto(
                update,
                (
                    f"❌ El minuto :{minuto:02d} ya está ocupado por "
                    f"@{conflicto_username}.\n"
                    f"Programación activa: {conflicto_detalle}.\n\n"
                    "Elegí otro minuto del 0 al 59."
                ),
            )
            return

        # El ancla conserva la hora actual y sustituye solamente el minuto.
        # La DB suma el intervalo completo desde esta ancla, por lo que:
        # 13:45 + intervalo 1 h + minuto 46 -> 14:46
        # 13:45 + intervalo 2 h + minuto 46 -> 15:46
        ahora_local = datetime.now(TZ)
        inicio_local = ahora_local.replace(
            minute=minuto,
            second=0,
            microsecond=0,
        )

        db.guardar_programacion_intervalo(
            AUTHORIZED_CHAT_ID,
            username,
            horas,
            inicio_iso=inicio_local.astimezone(
                timezone.utc
            ).isoformat(),
        )

        row_guardada = db.obtener_programacion(
            AUTHORIZED_CHAT_ID,
            username,
        )

        if row_guardada is None:
            raise RuntimeError(
                "No se pudo recuperar la programación por intervalo."
            )

        registrar_job_intervalo(
            context.application,
            row_guardada,
        )

        context.user_data.clear()

        notif = db.notificacion_activada(
            row_guardada
        )

        texto_intervalo = (
            "Cada 1 hora"
            if horas == 1
            else f"Cada {horas} horas"
        )

        await responder_texto(
            update,
            (
                f"✅ Programación guardada para @{username}\n\n"
                f"⏱ Intervalo: {texto_intervalo}\n"
                f"🕒 Minuto de la hora: :{minuto:02d}\n"
                f"⏭ Próxima revisión: "
                f"{texto_proxima_intervalo(row_guardada)} hs\n"
                f"Multimedia automática: "
                f"{'🔔 ACTIVADA' if notif else '🔕 DESACTIVADA'}\n\n"
                "Crear la programación no hizo ninguna consulta "
                "a Instagram."
            ),
            reply_markup=menu_programaciones(),
        )
        return

    if estado == SCHED_TIMES:
        horario = parse_hhmm(texto)

        if horario is None:
            await responder_texto(update,
                "❌ Horario inválido. Usá HH:MM, por ejemplo 21:30."
            )
            return

        horarios = context.user_data.setdefault(
            "times",
            [],
        )

        if horario in horarios:
            await responder_texto(update,
                "❌ Ese horario ya fue agregado."
            )
            return

        horarios.append(horario)

        cantidad = int(
            context.user_data[SCHED_COUNT]
        )
        username = str(
            context.user_data[SCHED_USERNAME]
        )

        if len(horarios) < cantidad:
            await responder_texto(update,
                f"✅ {horario}\n\n"
                f"Escribí el horario {len(horarios) + 1} "
                f"de {cantidad}:"
            )
            return

        horarios.sort()

        db.guardar_programacion(
            AUTHORIZED_CHAT_ID,
            username,
            horarios,
        )

        registrar_jobs_programacion(
            context.application,
            AUTHORIZED_CHAT_ID,
            username,
            horarios,
        )

        context.user_data.clear()

        row_guardada = db.obtener_programacion(
            AUTHORIZED_CHAT_ID,
            username,
        )
        notif = (
            db.notificacion_activada(row_guardada)
            if row_guardada is not None
            else True
        )

        await responder_texto(update,
            f"✅ Programación guardada para @{username}\n\n"
            f"Horarios Argentina: {' · '.join(horarios)}\n"
            f"Multimedia automática: "
            f"{'🔔 ACTIVADA' if notif else '🔕 DESACTIVADA'}\n\n"
            "Crear la programación no hizo ninguna consulta a Instagram.",
            reply_markup=menu_programaciones(),
        )
        return


async def error_global(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Captura errores no manejados de callbacks/jobs.

    No reemplaza los avisos específicos de Instagram: sirve como
    última red de seguridad.
    """
    error = context.error

    if error is None:
        return

    logger.error(
        "Error global no manejado por StoryPulse",
        exc_info=(
            type(error),
            error,
            error.__traceback__,
        ),
    )

    detalle = str(error).strip() or repr(error)

    if BOT_TOKEN:
        detalle = detalle.replace(
            BOT_TOKEN,
            "[TOKEN OCULTO]",
        )

    detalle = detalle[:1200]

    try:
        await enviar_texto_bot(context,
            chat_id=AUTHORIZED_CHAT_ID,
            text=(
                "🚨 <b>ERROR INTERNO STORYPULSE</b>\n\n"
                f"Hora: {esc(datetime.now(TZ).strftime('%d/%m/%Y %H:%M:%S'))}\n\n"
                "<b>Detalle:</b>\n"
                f"<code>{esc(detalle)}</code>\n\n"
                "El error completo quedó registrado en journalctl."
            ),
            parse_mode="HTML",
            disable_notification=False,
        )
    except Exception:
        logger.exception(
            "También falló el envío del aviso global a Telegram."
        )


async def post_init(application: Application) -> None:
    db.inicializar()

    await application.bot.set_my_commands(
        [
            BotCommand("start", "Abrir StoryPulse"),
        ]
    )

    for row in db.listar_programaciones_activas():
        registrar_programacion_guardada(
            application,
            row,
        )

    logger.info(
        "Programaciones restauradas: %s",
        len(db.listar_programaciones_activas()),
    )


def main() -> None:
    db.inicializar()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )
    application.add_handler(
        CallbackQueryHandler(callback)
    )
    application.add_handler(
        MessageHandler(
            (
                filters.PHOTO
                | filters.VIDEO
                | filters.ANIMATION
                | filters.Document.ALL
            ),
            registrar_multimedia_entrante,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            recibir_texto,
        )
    )

    application.add_error_handler(
        error_global
    )

    logger.info("StoryPulse Web Private iniciado.")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()

# StoryPulse v2.0
# Created by FacuSecX https://github.com/FacuSecX/StoryPulse-Private



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
import uuid
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
    comprobar_perfiles_desde_pagina,
    comprobar_sesion_local,
    descargar_historias,
    limpiar_username,
    resolver_user_id,
)
from publicaciones import descargar_publicaciones
from highlights import (
    SinHistoriasDestacadas,
    descargar_destacadas,
)
from instagram_sessions import (
    listar_sesiones, usar_sesion, obtener_ruta_sesion,
    estado_sesiones_local, SesionesAgotadas, ErrorSesionInstagram,
    ConfiguracionSesionesError, obtener_vinculo, cambiar_sesion_preferida,
    verificar_accesos_perfil, ejecutar_con_sesiones, sesion_actual,
    listar_sesiones_nuevas, actualizar_accesos_sesion,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID_ENV = os.getenv("TELEGRAM_CHAT_ID", "").strip()
HISTORYS_DIR = Path(
    os.getenv("HISTORYS_DIR", "/historys")
).expanduser()

PANEL_URL = os.getenv("STORYPULSE_PANEL_URL", "").strip()
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
SCHED_MODALIDAD = "sched_modalidad"
SCHED_SESIONES = "sched_sesiones"
SCHED_SESIONES_DETECTADAS = "sched_sesiones_detectadas"
SCHED_SELECCION_TOKENS = "sched_seleccion_tokens"
SCHED_SELECCION_MESSAGE_ID = "sched_seleccion_message_id"
SCHED_SESIONES_ELEGIDAS = "sched_sesiones_elegidas"


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
        modificado = obtener_ruta_sesion().stat().st_mtime
    except OSError:
        return "no disponible"

    return _formatear_antiguedad_segundos(
        time.time() - modificado
    )


def _cookie_storage_state(nombre: str) -> str | None:
    try:
        data = json.loads(
            obtener_ruta_sesion().read_text(encoding="utf-8")
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
    """Abre únicamente la portada y exige señales visibles del feed autenticado."""
    comprobar_sesion_local()
    ds_user_id = _cookie_storage_state("ds_user_id")
    usernames: list[str] = []
    ruta = obtener_ruta_sesion()
    try:
        username = _username_de_objeto_por_id(
            json.loads(ruta.read_text(encoding="utf-8")), ds_user_id,
        )
        if username:
            usernames.append(username)
    except (OSError, ValueError):
        pass

    # No buscar palabras sueltas en las publicaciones: una descripción puede
    # mencionar CAPTCHA o challenge sin que la sesión tenga ningún problema.
    comprobar_dom = r"""() => {
        const visible = el => !!el && el.getClientRects().length > 0 &&
            getComputedStyle(el).visibility !== 'hidden';
        const visibles = selector => Array.from(document.querySelectorAll(selector)).filter(visible);
        const texto = el => (el.innerText || el.textContent || '').trim();
        const navegacion = visibles('a[href*="/direct/inbox"], a[href*="/accounts/edit"], a[href*="/explore/"]');
        const publicaciones = visibles('article, main [role="article"], [role="main"] [role="article"]');
        const historias = visibles('a[href^="/stories/"]').filter(el =>
            !el.closest('article, [role="article"]'));
        const titulos = visibles('h1, h2, [role="dialog"], form').filter(el =>
            !el.closest('article, [role="article"]')).map(texto);
        const alertas = visibles('[role="alert"]').filter(el =>
            !el.closest('article, [role="article"]')).map(texto);
        const login = visibles('input[name="password"], input[type="password"]').length > 0;
        const captcha = visibles('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], form[action*="/challenge"], form[action*="/checkpoint"]').length > 0;
        const vacio = visibles('main h1, main h2, [role="main"] h1, [role="main"] h2').some(el =>
            /^(you['’]re all caught up|ya est[aá]s al d[ií]a|est[aá]s al d[ií]a|welcome to instagram|te damos la bienvenida a instagram)[.!]?$/i.test(texto(el)));
        return {navegacion: navegacion.length > 0, publicaciones: publicaciones.length,
            historias: historias.length, feed_vacio: vacio, login, captcha,
            titulos, alertas};
    }"""

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = None
        try:
            context = browser.new_context(
                storage_state=str(ruta),
                viewport={"width": 1365, "height": 900},
                locale="es-AR",
                timezone_id=str(getattr(TZ, "key", "America/Argentina/Buenos_Aires")),
            )
            page = context.new_page()

            def procesar_response(response) -> None:
                # Respuestas opcionales fallidas no invalidan un feed visible.
                # Se leen datos ya recibidos para identificar la cuenta; no se
                # hacen consultas adicionales de Stories ni de publicaciones.
                try:
                    if not re.match(r"https://(?:[a-z0-9-]+\.)?instagram\.com/", response.url, re.I):
                        return
                    if response.status != 200 or "json" not in response.headers.get("content-type", "").lower():
                        return
                    username = _username_de_objeto_por_id(response.json(), ds_user_id)
                    if username and username not in usernames:
                        usernames.append(username)
                except Exception:
                    pass

            page.on("response", procesar_response)
            respuesta = page.goto(
                "https://www.instagram.com/", wait_until="domcontentloaded", timeout=40_000,
            )
            if respuesta is None:
                raise RuntimeError("Instagram no devolvió respuesta al abrir el feed.")
            estado_http = int(respuesta.status)
            if estado_http >= 400:
                raise RuntimeError(f"Instagram respondió HTTP {estado_http} al abrir el feed.")

            limite = time.monotonic() + 15
            for _ in range(21):
                url = page.url.casefold()
                if "/accounts/login" in url:
                    raise RuntimeError("Instagram redirigió al login. La sesión necesita actualizarse.")
                if any(ruta_bloqueo in url for ruta_bloqueo in (
                    "/challenge", "/checkpoint", "/auth_platform/", "/accounts/confirm_email", "/accounts/confirm_phone",
                )):
                    raise RuntimeError("Instagram abrió un checkpoint/CAPTCHA. La sesión necesita verificación manual.")

                dom = page.evaluate(comprobar_dom)
                if not isinstance(dom, dict):
                    dom = {}
                if dom.get("login"):
                    raise RuntimeError("Instagram mostró el login. La sesión necesita actualizarse.")
                if dom.get("captcha"):
                    raise RuntimeError("Instagram mostró un CAPTCHA. La sesión necesita verificación manual.")

                titulos = "\n".join(str(item).casefold() for item in dom.get("titulos", []))
                indicadores_verificacion = (
                    "confirm it's you", "confirm it’s you", "confirma que eres tú",
                    "confirma que sos vos", "ayúdanos a confirmar que eres tú",
                    "confirma tu identidad", "verifica tu identidad", "confirm your identity",
                    "help us confirm you own this account", "enter your security code",
                    "ingresa el código de seguridad", "introduce el código de seguridad",
                    "suspendimos tu cuenta", "we suspended your account",
                    "demuestra que eres una persona", "prove you're human", "prove you’re human",
                    "confirma que no eres un robot", "confirm you're human", "confirm you’re human",
                )
                if any(indicador in titulos for indicador in indicadores_verificacion):
                    raise RuntimeError("Instagram mostró una verificación de seguridad. La sesión necesita verificación manual.")

                feed_visible = bool(re.fullmatch(r"https://(?:www\.)?instagram\.com/?(?:[?#].*)?", url)
                    and dom.get("navegacion") and (
                    dom.get("publicaciones") or dom.get("historias") or dom.get("feed_vacio")
                ))
                alertas = "\n".join(str(item).casefold() for item in dom.get("alertas", []))
                # Un aviso explícito de fallo del feed invalida una carga
                # parcial, aunque ya haya publicaciones renderizadas.
                if any(indicador in alertas for indicador in (
                    "couldn't refresh feed", "couldn’t refresh feed", "no se pudo actualizar el feed",
                )):
                    raise RuntimeError("Instagram mostró un error y no cargó el feed normalmente.")
                # La portada debe mostrar contenido además de navegación: un
                # HTTP 200 o la presencia de cookies por sí solos no bastan.
                if feed_visible:
                    if not usernames:
                        try:
                            username = _username_de_html_por_id(page.content(), ds_user_id)
                        except Exception:
                            username = None
                        if username:
                            usernames.append(username)
                    return {
                        "ok": True, "http_status": estado_http,
                        "username": usernames[0] if usernames else None,
                        "user_id": ds_user_id, "url_final": page.url,
                        "antiguedad_archivo": _antiguedad_archivo_sesion(),
                    }

                if any(indicador in alertas for indicador in (
                    "something went wrong", "se produjo un error", "try again later", "inténtalo de nuevo más tarde",
                )):
                    raise RuntimeError("Instagram mostró un error y no cargó el feed normalmente.")
                if time.monotonic() >= limite:
                    break
                page.wait_for_timeout(750)
            raise RuntimeError("No se pudo confirmar el feed autenticado. La sesión necesita actualización o revisión.")
        finally:
            if context is not None:
                context.close()
            browser.close()


def _tipo_error_estado_feed(error: Exception) -> str:
    if isinstance(error, ErrorSesionInstagram):
        return error.tipo
    if isinstance(error, (FileNotFoundError, json.JSONDecodeError)):
        return "archivo"
    texto = str(error).casefold()
    if any(valor in texto for valor in ("login", "iniciar sesión", "inicie sesión", "http 401")):
        return "login"
    if any(valor in texto for valor in (
        "captcha", "recaptcha", "challenge", "checkpoint", "verificación", "verification", "robot",
    )):
        return "verificacion"
    if "429" in texto or "rate limit" in texto or "too many requests" in texto:
        return "rate_limit"
    if "400" in texto:
        return "http_400"
    if "403" in texto:
        return "rechazada"
    return "feed"


def _estado_sesiones_feed(progress_callback=None, detener=None) -> list[dict[str, object]]:
    """Diagnóstico al pulsar Estado, independiente del historial de descargas."""
    resultados: list[dict[str, object]] = []
    sesiones = listar_sesiones()
    for indice, sesion in enumerate(sesiones, 1):
        if detener is not None and detener.is_set():
            break
        base = {"id": sesion.id, "username": sesion.username, "etiqueta": sesion.etiqueta}
        if progress_callback:
            progress_callback({"actual": base, "procesadas": indice - 1, "total": len(sesiones)})
        try:
            # No consultar _indisponibilidad ni registrar éxitos/errores aquí:
            # un error histórico no determina este diagnóstico del feed.
            with usar_sesion(sesion):
                remoto = _comprobar_sesion_web_real()
            if remoto.get("ok") is not True:
                raise RuntimeError("No se pudo confirmar el feed autenticado.")
            resultado = {
                **base, "disponible": True, "ultimo_error_tipo": None,
                "detalle": "Feed autenticado visible; sesión correcta.", "remoto": remoto,
            }
        except Exception as error:
            resultado = {
                **base, "disponible": False, "ultimo_error_tipo": _tipo_error_estado_feed(error),
                "detalle": str(error) or "El feed no se pudo comprobar.",
            }
        resultados.append(resultado)
        if progress_callback:
            progress_callback({"actual": base, "procesadas": indice, "total": len(sesiones)})
    return resultados


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

    if isinstance(error, ConfiguracionSesionesError):
        return ("📄", "CONFIGURACIÓN DE SESIONES", "Revisá sesiones_instagram.json y el registro de sesiones.")
    if isinstance(error, SesionesAgotadas):
        return ("🔐", "NINGUNA SESIÓN DISPONIBLE",
                "Revisá los archivos JSON y los últimos errores. CAPTCHA y login requieren renovación manual de la sesión afectada.")
    if isinstance(error, ErrorSesionInstagram) and error.tipo == "verificacion":
        return ("🔐", "VERIFICACIÓN DE INSTAGRAM",
                "Completá la verificación manualmente y exportá de nuevo la sesión afectada.")
    if isinstance(error, ErrorSesionInstagram) and error.tipo == "http_400":
        return ("⚠️", "INSTAGRAM RECHAZÓ LA CONSULTA (HTTP 400)",
                "Instagram rechazó esta consulta. Revisá el último error registrado y el archivo JSON de esa sesión.")
    if (
        "429" in bajo
        or "rate limit" in bajo
        or "too many requests" in bajo
        or "feedback_required" in bajo
    ):
        return (
            "🚦",
            "LÍMITE DE INSTAGRAM",
            (
                "Instagram informó un límite para esta consulta. "
                "Revisá el último rechazo registrado para esa sesión."
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
                    **item,
                    "nombre": nombre,
                    "username": username,
                    "user_id": user_id,
                    "destacada": item.get("destacada") is True,
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
    with CUENTAS_LOCK:
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


def boton_cuenta(cuenta: dict, texto: str, callback_data: str) -> InlineKeyboardButton:
    """El estilo normal se conserva cuando el perfil no está destacado."""
    estilo = {"style": "primary"} if cuenta.get("destacada") is True else {}
    return InlineKeyboardButton(texto, callback_data=callback_data, **estilo)


def alternar_cuenta_destacada(username: str) -> bool:
    username = limpiar_username(username)
    with CUENTAS_LOCK:
        cuentas = cargar_cuentas()
        cuenta = next((c for c in cuentas if c["username"] == username), None)
        if cuenta is None:
            raise ValueError("Ese perfil ya no está en las cuentas agregadas.")
        cuenta["destacada"] = not cuenta.get("destacada", False)
        guardar_cuentas(cuentas)
        return cuenta["destacada"]


def menu_cuentas_destacadas() -> InlineKeyboardMarkup:
    botones = [boton_cuenta(c, f"{'⭐' if c.get('destacada') else '☆'} {c['nombre']} (@{c['username']})",
                            f"highlight_toggle:{c['username']}") for c in cargar_cuentas()]
    filas = [botones[i:i + 2] for i in range(0, len(botones), 2)]
    filas.append([InlineKeyboardButton("‹ Gestión de cuentas", callback_data="manage")])
    return InlineKeyboardMarkup(filas)


def menu_perfiles_sesion_preferida() -> InlineKeyboardMarkup:
    botones = [boton_cuenta(c, f"👤 {c['nombre']} (@{c['username']})",
                            f"pref_profile:{c['username']}") for c in cargar_cuentas()]
    filas = [botones[i:i + 2] for i in range(0, len(botones), 2)]
    filas.append([InlineKeyboardButton("‹ Gestión de cuentas", callback_data="manage")])
    return InlineKeyboardMarkup(filas)


def nombres_sesiones(sesiones) -> dict[str, str | None]:
    """Identificar principal comparando ds_user_id con cuentas conocidas, sin red."""
    nombres = {s.id: s.username for s in sesiones}
    if all(nombres.values()):
        return nombres
    conocidas = {}
    pendientes = []
    for sesion in sesiones:
        try:
            data = json.loads(sesion.archivo.read_text(encoding="utf-8-sig"))
            uid = next((c.get("value") for c in data.get("cookies", [])
                        if isinstance(c, dict) and c.get("name") == "ds_user_id"), None)
        except (OSError, ValueError, TypeError, AttributeError):
            uid = None
        if not isinstance(uid, str) or not uid:
            uid = None
        if uid and sesion.username:
            conocidas[uid] = sesion.username
        if not sesion.username:
            pendientes.append((sesion.id, uid))
    for identificador, uid in pendientes:
        if uid in conocidas:
            nombres[identificador] = conocidas[uid]
    return nombres


def menu_elegir_sesion_preferida(username: str, context, message_id=None) -> InlineKeyboardMarkup:
    sesiones = listar_sesiones()
    nombres = nombres_sesiones(sesiones)
    preferida = (obtener_vinculo(username) or {}).get("sesion_id")
    elecciones = {}
    filas = []
    for sesion in sesiones:
        token = uuid.uuid4().hex[:12]
        elecciones[token] = sesion
        nombre = f"@{nombres[sesion.id]}" if nombres.get(sesion.id) else "Usuario sin identificar"
        actual = "✅ " if sesion.id == preferida else ""
        filas.append([InlineKeyboardButton(f"{actual}{nombre} · {sesion.id}", callback_data=f"pref_pick:{token}")])
    context.user_data["preferencia_seleccion"] = {
        "username": username, "message_id": message_id, "elecciones": elecciones,
    }
    filas.append([InlineKeyboardButton("‹ Elegir otro perfil", callback_data="pref_accounts")])
    return InlineKeyboardMarkup(filas)


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
                "✨ HISTORIAS DESTACADAS",
                callback_data="highlights_menu",
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
            boton_cuenta(
                cuenta,
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
            boton_cuenta(
                cuenta,
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
                    "⭐ Destacar cuentas",
                    callback_data="highlight_accounts",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔑 Cambiar sesión preferida",
                    callback_data="pref_accounts",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 Actualizar sesiones",
                    callback_data="sessions_update",
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
            boton_cuenta(
                cuenta,
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


ANTIREPETICION_TIPOS = {
    "todo": ("todo", "Resetear todo"),
    "pub": ("publicaciones", "Resetear publicaciones"),
    "hist": ("historias", "Resetear historias"),
    "dest": ("destacadas", "Resetear historias destacadas"),
}


def menu_tipos_antirepeticion(username: str) -> InlineKeyboardMarkup:
    """El alcance se elige después del perfil, antes de confirmar."""
    filas = [
        [InlineKeyboardButton(
            etiqueta,
            callback_data=f"dedupe_reset_type:{username}:{codigo}",
            style="danger" if codigo == "todo" else None,
        )]
        for codigo, (_tipo, etiqueta) in ANTIREPETICION_TIPOS.items()
    ]
    filas.append([InlineKeyboardButton(
        "‹ Volver", callback_data="dedupe_reset_menu",
    )])
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


def menu_modalidades_programacion() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Programaciones normales", callback_data="schedules_normal")],
        [InlineKeyboardButton("🔄 Programaciones variables", callback_data="schedules_variable")],
        [InlineKeyboardButton("📋 Ver programaciones", callback_data="sched_list")],
        [InlineKeyboardButton("‹ Menú principal", callback_data="menu")],
    ])


def menu_programaciones(modalidad: str = "normal") -> InlineKeyboardMarkup:
    filas = []
    botones = []

    for cuenta in cuentas_disponibles_para_programar():
        botones.append(
            boton_cuenta(
                cuenta,
                f"⏰ {cuenta['nombre']}",
                callback_data=f"{'schedv' if modalidad == 'variable' else 'sched'}:{cuenta['username']}",
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
                "‹ Elegir modalidad",
                callback_data="schedules",
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
            boton_cuenta(
                buscar_cuenta(username) or {},
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
        f"Modalidad: <b>{'Variable' if db.modalidad_sesiones_de(row) == 'variable' else 'Normal'}</b>",
        "",
    ]

    if db.modalidad_sesiones_de(row) == "variable":
        ids = db.sesiones_rotacion_de(row)
        sesiones = listar_sesiones()
        nombres = nombres_sesiones(sesiones)
        etiquetas = [f"@{nombres[sid]} ({sid})" if nombres.get(sid) else sid for sid in ids]
        orden = db.preparar_rotacion(int(row["chat_id"]), username)
        siguiente = orden[0] if orden else None
        proxima = f"@{nombres[siguiente]} ({siguiente})" if nombres.get(siguiente) else siguiente
        lineas.extend(["Sesiones de rotación: " + esc(" · ".join(etiquetas)),
                       "Próximo turno: " + esc(proxima or "sin sesiones"), ""])

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
    modalidad: str = "normal",
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
                        f"{'schedv' if modalidad == 'variable' else 'sched'}_mode_daily:{username}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "⏱ Intervalo de tiempo",
                    callback_data=(
                        f"{'schedv' if modalidad == 'variable' else 'sched'}_mode_interval:{username}"
                    ),
                )
            ],
            [
                InlineKeyboardButton(
                    "‹ Volver",
                    callback_data=f"schedules_{modalidad}",
                )
            ],
        ]
    )


def menu_intervalos(
    username: str,
    modalidad: str = "normal",
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
                    f"{'schedv' if modalidad == 'variable' else 'sched'}_interval:{username}:{horas}"
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
                callback_data=f"{'schedv' if modalidad == 'variable' else 'sched'}:{username}",
            )
        ]
    )

    return InlineKeyboardMarkup(
        filas
    )


def menu_cantidad(username: str, modalidad: str = "normal") -> InlineKeyboardMarkup:
    filas = []

    for n in range(1, 7):
        filas.append(
            [
                InlineKeyboardButton(
                    f"{n} revisión{'es' if n != 1 else ''} por día",
                    callback_data=f"{'schedv' if modalidad == 'variable' else 'sched'}_count:{username}:{n}",
                )
            ]
        )

    filas.append(
        [
            InlineKeyboardButton(
                "‹ Volver",
                callback_data=f"{'schedv' if modalidad == 'variable' else 'sched'}:{username}",
            )
        ]
    )

    return InlineKeyboardMarkup(filas)


def menu_destacadas() -> InlineKeyboardMarkup:
    """Cuentas fijas para archivar sus carruseles de historias destacadas."""
    filas = []
    botones = []

    for cuenta in cargar_cuentas():
        botones.append(
            boton_cuenta(
                cuenta,
                f"✨ {cuenta['nombre']}",
                callback_data=f"highlights:{cuenta['username']}",
            )
        )

    for posicion in range(0, len(botones), 2):
        filas.append(botones[posicion:posicion + 2])

    filas.append([
        InlineKeyboardButton("‹ Menú principal", callback_data="menu")
    ])
    return InlineKeyboardMarkup(filas)


def conservar_flujo_programacion(context, username: str, modalidad: str) -> bool:
    """Cada botón lleva su modalidad; un flujo variable exige su comprobación."""
    sesiones = []
    elegidas = []
    if (context.user_data.get(SCHED_USERNAME) not in (None, username)
            or context.user_data.get(SCHED_MODALIDAD) not in (None, modalidad)):
        return False
    if modalidad == "variable":
        if (context.user_data.get(SCHED_USERNAME) != username
                or context.user_data.get(SCHED_MODALIDAD) != "variable"
                or not context.user_data.get(SCHED_SESIONES)
                or not context.user_data.get(SCHED_SESIONES_ELEGIDAS)):
            return False
        sesiones = list(context.user_data[SCHED_SESIONES])
        elegidas = list(context.user_data[SCHED_SESIONES_ELEGIDAS])
    context.user_data.clear()
    context.user_data.update({SCHED_USERNAME: username, SCHED_MODALIDAD: modalidad,
                              SCHED_SESIONES: sesiones,
                              SCHED_SESIONES_ELEGIDAS: elegidas})
    return True


def menu_seleccionar_sesiones_variable(username: str, context, message_id=None) -> InlineKeyboardMarkup:
    """Lista las sesiones que confirmaron acceso y permite elegir el subconjunto."""
    detectadas = list(context.user_data.get(SCHED_SESIONES_DETECTADAS) or
                      context.user_data.get(SCHED_SESIONES) or [])
    elegidas = set(context.user_data.get(SCHED_SESIONES_ELEGIDAS) or [])
    nombres = nombres_sesiones(listar_sesiones())
    tokens = {}
    filas = []
    for sesion_id in detectadas:
        token = uuid.uuid4().hex[:12]
        tokens[token] = sesion_id
        nombre = nombres.get(sesion_id)
        etiqueta = f"@{nombre}" if nombre else "Usuario sin identificar"
        marcado = sesion_id in elegidas
        button_kwargs = {"callback_data": f"schedv_session_toggle:{token}"}
        if marcado:
            button_kwargs["style"] = "primary"
        filas.append([InlineKeyboardButton(
            f"{'☑️' if marcado else '⬜'} {etiqueta} · {sesion_id}", **button_kwargs)])
    filas.append([InlineKeyboardButton(
        "✅ Confirmar sesiones elegidas", callback_data="schedv_session_confirm")])
    filas.append([InlineKeyboardButton(
        "‹ Volver a cuentas", callback_data="schedules_variable")])
    context.user_data[SCHED_SELECCION_TOKENS] = tokens
    context.user_data[SCHED_SELECCION_MESSAGE_ID] = message_id
    return InlineKeyboardMarkup(filas)


def sesiones_rotacion_confirmadas(perfil: dict) -> list[str]:
    """Dos JSON del mismo usuario representan una sola cuenta en la rotación."""
    sesiones = listar_sesiones()
    por_id = {sesion.id: sesion for sesion in sesiones}
    nombres = nombres_sesiones(sesiones)
    identidades, resultado = set(), []
    for fila in perfil.get("sesiones_confirmadas", []):
        sid = fila["id"]
        if sid not in por_id:
            continue
        nombre = nombres.get(sid) or fila.get("username")
        identidad = ("username", nombre.casefold()) if nombre else ("sesion", sid)
        if identidad not in identidades:
            identidades.add(identidad)
            resultado.append(sid)
    return resultado


def texto_modalidad_guardada(row) -> str:
    if db.modalidad_sesiones_de(row) == "variable":
        cantidad = len(db.sesiones_rotacion_de(row))
        return (f"Modalidad: VARIABLE · {cantidad} sesión(es) con acceso confirmado.\n"
                "Cada ejecución toma el siguiente turno de la lista guardada.")
    return "Modalidad: NORMAL. Crear la programación no hizo ninguna consulta a Instagram."


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


def consultar_historias_variables(chat_id: int, username: str, user_id: int | None):
    """Resolver y descargar en la misma sesión; conservar la preferida normal."""
    orden = db.preparar_rotacion(chat_id, username)
    if not orden:
        raise ConfiguracionSesionesError("La programación variable no tiene sesiones de rotación.")
    sesion_exitosa = None

    def consultar():
        nonlocal user_id, sesion_exitosa
        sesion = sesion_actual()
        if user_id is None:
            user_id = resolver_user_id(username)
            actualizar_user_id(username, user_id)
        try:
            historias = descargar_historias(username, user_id)
        except SinHistoriasDisponibles:
            sesion_exitosa = sesion.id
            raise
        sesion_exitosa = sesion.id
        return historias

    try:
        return ejecutar_con_sesiones(username, consultar, orden_sesion_ids=orden,
                                     conservar_preferida=True)
    finally:
        # Incluso un turno sin Stories o con todos los intentos fallidos avanza.
        db.registrar_sesion_rotacion(chat_id, username, sesion_exitosa or orden[0])


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
        programacion = db.obtener_programacion(chat_id, username) if not manual else None
        if programacion is not None and db.modalidad_sesiones_de(programacion) == "variable":
            historias = await asyncio.to_thread(consultar_historias_variables, chat_id, username, user_id)
        else:
            if user_id is None:
                user_id = await asyncio.to_thread(resolver_user_id, username)
                actualizar_user_id(username, user_id)
            historias = await asyncio.to_thread(descargar_historias, username, user_id)

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

    if data == "highlights_menu":
        context.user_data.clear()

        cuentas = cargar_cuentas()
        texto = (
            "✨ <b>Historias destacadas</b>\n\n"
            "Elegí una cuenta para descargar sus carruseles al servidor:"
            if cuentas
            else
            "ℹ️ No hay cuentas configuradas."
        )

        await query.edit_message_text(
            texto,
            parse_mode="HTML",
            reply_markup=menu_destacadas(),
        )
        return

    if data.startswith("highlights:"):
        username = limpiar_username(data.split(":", 1)[1])
        cuenta = buscar_cuenta(username)

        if cuenta is None:
            await query.edit_message_text(
                f"ℹ️ @{username} ya no está en las cuentas fijas.",
                reply_markup=menu_destacadas(),
            )
            return

        await query.edit_message_text(
            f"⏳ Buscando historias destacadas de @{username}...\n\n"
            "Se usará primero la sesión preferida y, si hace falta, las sesiones autorizadas.\n"
            "El progreso se actualizará automáticamente cada 10 segundos."
        )

        progreso_lock = threading.Lock()
        progreso_estado = {
            "etapa": "iniciando",
            "grupos_detectados": 0,
            "grupos_procesados": 0,
            "historias_detectadas": 0,
            "historias_procesadas": 0,
            "archivos_nuevos": 0,
            "archivos_guardados": 0,
            "fallidas": 0,
        }
        inicio_progreso = time.monotonic()

        def recibir_progreso_destacadas(datos: dict) -> None:
            with progreso_lock:
                for clave, valor in datos.items():
                    if clave in {
                        "grupos_detectados", "grupos_procesados",
                        "historias_detectadas", "historias_procesadas",
                        "archivos_nuevos", "archivos_guardados", "fallidas",
                    }:
                        progreso_estado[clave] = max(
                            int(progreso_estado.get(clave) or 0),
                            int(valor or 0),
                        )
                    else:
                        progreso_estado[clave] = valor

        def texto_progreso_destacadas() -> str:
            with progreso_lock:
                estado = dict(progreso_estado)

            transcurrido = max(0, int(time.monotonic() - inicio_progreso))
            minutos, segundos = divmod(transcurrido, 60)
            tiempo_texto = (
                f"{minutos} min {segundos:02d} s"
                if minutos
                else f"{segundos} s"
            )
            etiquetas = {
                "iniciando": "Preparando navegador y sesión",
                "procesando": "Descargando y guardando Highlights",
                "terminado": "Finalizando",
            }
            etapa = etiquetas.get(
                str(estado.get("etapa") or ""),
                "Procesando",
            )
            grupos_detectados = int(estado.get("grupos_detectados") or 0)
            grupos_procesados = int(estado.get("grupos_procesados") or 0)
            historias_detectadas = int(estado.get("historias_detectadas") or 0)
            historias_procesadas = int(estado.get("historias_procesadas") or 0)
            archivos_nuevos = int(estado.get("archivos_nuevos") or 0)
            archivos_guardados = int(estado.get("archivos_guardados") or 0)
            fallidas = int(estado.get("fallidas") or 0)

            return (
                f"⏳ Descargando Highlights de @{username}...\n\n"
                f"🟢 Estado: {etapa}\n"
                f"🕒 Tiempo: {tiempo_texto}\n"
                f"✨ Carruseles detectados: {grupos_detectados}\n"
                f"⚙️ Carruseles procesados: {grupos_procesados}/{grupos_detectados}\n"
                f"🔎 Historias detectadas: {historias_detectadas}\n"
                f"📥 Historias procesadas: {historias_procesadas}\n"
                f"🆕 Archivos nuevos: {archivos_nuevos}\n"
                f"📁 Archivos guardados: {archivos_guardados}\n"
                f"⚠️ Fallidas/pendientes: {fallidas}\n\n"
                "Actualización automática cada 10 segundos."
            )

        async def refrescar_progreso_destacadas() -> None:
            while True:
                await asyncio.sleep(10)
                try:
                    await query.edit_message_text(
                        texto_progreso_destacadas()
                    )
                except TelegramError as error:
                    if "not modified" not in str(error).lower():
                        logger.warning(
                            "No se pudo refrescar el progreso de Highlights de @%s: %s",
                            username,
                            error,
                        )

        tarea_progreso = asyncio.create_task(
            refrescar_progreso_destacadas()
        )

        try:
            try:
                async with IG_LOCK:
                    resultado = await asyncio.to_thread(
                        descargar_destacadas,
                        username,
                        cuenta.get("user_id"),
                        AUTHORIZED_CHAT_ID,
                        progress_callback=recibir_progreso_destacadas,
                    )
            finally:
                tarea_progreso.cancel()
                try:
                    await tarea_progreso
                except asyncio.CancelledError:
                    pass

            await query.edit_message_text(
                f"✅ <b>@{esc(username)}</b>\n\n"
                f"Carruseles encontrados: <b>{len(resultado.grupos)}</b>\n"
                f"Archivos nuevos: <b>{resultado.archivos_nuevos}</b>\n"
                f"Ya guardados: <b>{resultado.archivos_ya_guardados}</b>\n\n"
                f"📁 <code>{esc(str(resultado.carpeta))}</code>",
                parse_mode="HTML",
                reply_markup=menu_destacadas(),
            )
        except SinHistoriasDestacadas:
            await query.edit_message_text(
                f"ℹ️ <b>@{esc(username)}</b>\n\n"
                "No hemos detectado historias destacadas dentro de este perfil.",
                parse_mode="HTML",
                reply_markup=menu_destacadas(),
            )
        except Exception as error:
            logger.exception("Error descargando historias destacadas de @%s", username)
            icono, tipo, recomendacion = diagnosticar_error(error)
            detalle = str(error).strip() or repr(error)
            if BOT_TOKEN:
                detalle = detalle.replace(BOT_TOKEN, "[TOKEN OCULTO]")
            await query.edit_message_text(
                f"{icono} <b>ERROR EN HISTORIAS DESTACADAS</b>\n\n"
                f"Cuenta: <b>@{esc(username)}</b>\n"
                f"Tipo: <b>{esc(tipo)}</b>\n\n"
                f"<code>{esc(detalle[:900])}</code>\n\n"
                f"💡 {esc(recomendacion)}",
                parse_mode="HTML",
                reply_markup=menu_destacadas(),
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
                # Comparte con Stories la selección y asociación de sesiones.
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
            "Elegí una cuenta y después qué IDs querés resetear."
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
                f"♻️ <b>Antirepetición de @{esc(username)}</b>\n\n"
                "Elegí qué registros querés resetear.\n"
                "Los archivos descargados se conservarán."
            ),
            parse_mode="HTML",
            reply_markup=menu_tipos_antirepeticion(username),
        )
        return

    if data.startswith("dedupe_reset_type:"):
        partes = data.split(":", 2)
        username = limpiar_username(partes[1])
        codigo = partes[2] if len(partes) == 3 else ""
        if codigo not in ANTIREPETICION_TIPOS:
            await query.edit_message_text(
                "ℹ️ Opción de antirepetición inválida. Elegí nuevamente.",
                reply_markup=menu_reiniciar_antirepeticion(),
            )
            return
        if buscar_cuenta(username) is None:
            await query.edit_message_text(
                f"ℹ️ @{username} ya no está en las cuentas configuradas.",
                reply_markup=menu_reiniciar_antirepeticion(),
            )
            return
        tipo, etiqueta = ANTIREPETICION_TIPOS[codigo]
        alcance = {
            "todo": "Stories, publicaciones e historias destacadas",
            "historias": "Stories",
            "publicaciones": "publicaciones",
            "destacadas": "historias destacadas",
        }[tipo]
        texto = (
            f"⚠️ <b>{esc(etiqueta)} de @{esc(username)}</b>\n\n"
            f"Se eliminarán únicamente los IDs registrados de {alcance}.\n"
            "Los archivos descargados se conservarán."
        )
        if tipo in {"todo", "publicaciones"}:
            texto += (
                "\n\nLa próxima descarga de publicaciones volverá a recorrer "
                "el historial completo disponible."
            )
        await query.edit_message_text(
            texto,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            f"♻️ Sí, {etiqueta.lower()}",
                            callback_data=f"dedupe_reset_confirm:{username}:{codigo}",
                            style="danger",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "❌ Cancelar",
                            callback_data=f"dedupe_reset_select:{username}",
                        )
                    ],
                ]
            ),
        )
        return

    if data.startswith("dedupe_reset_confirm:"):
        partes = data.split(":", 2)
        username = limpiar_username(partes[1])
        # Los botones de la versión anterior conservan el reseteo completo.
        codigo = partes[2] if len(partes) == 3 else "todo"
        if codigo not in ANTIREPETICION_TIPOS:
            await query.edit_message_text(
                "ℹ️ Opción de antirepetición inválida. Elegí nuevamente.",
                reply_markup=menu_reiniciar_antirepeticion(),
            )
            return
        tipo, etiqueta = ANTIREPETICION_TIPOS[codigo]
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
                tipo=tipo,
            )

        lineas = []
        for nombre, descripcion in (
            ("historias", "Stories"),
            ("destacadas", "historias destacadas"),
            ("publicaciones", "publicaciones"),
        ):
            if tipo in {"todo", nombre}:
                lineas.append(f"IDs de {descripcion} eliminados: {eliminados[nombre]}")
        await query.edit_message_text(
            (
                f"✅ <b>@{esc(username)}</b>: {esc(etiqueta)} completado.\n\n"
                + "\n".join(lineas)
                + "\n\n"
                "Los archivos existentes no fueron eliminados.\n"
                "La próxima revisión podrá procesar nuevamente el contenido elegido."
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

    if data == "highlight_accounts":
        context.user_data.clear()
        await query.edit_message_text(
            "⭐ Destacar cuentas\n\nTocá una cuenta para destacar o quitar la marca. Las destacadas aparecen en azul en las listas.",
            reply_markup=menu_cuentas_destacadas(),
        )
        return

    if data.startswith("highlight_toggle:"):
        username = limpiar_username(data.split(":", 1)[1])
        try:
            destacada = alternar_cuenta_destacada(username)
            texto = f"{'⭐ Destacada' if destacada else '☆ Marca quitada'}: @{username}\n\nTocá otra cuenta para cambiar su marca."
        except ValueError as error:
            texto = str(error)
        await query.edit_message_text(texto, reply_markup=menu_cuentas_destacadas())
        return

    if data == "pref_accounts":
        context.user_data.clear()
        await query.edit_message_text("🔑 Cambiar sesión preferida\n\nElegí el perfil que querés consultar con otra sesión.",
                                      reply_markup=menu_perfiles_sesion_preferida())
        return

    if data.startswith("pref_profile:"):
        username = limpiar_username(data.split(":", 1)[1])
        if buscar_cuenta(username) is None:
            await query.edit_message_text("Ese perfil ya no está agregado.", reply_markup=menu_perfiles_sesion_preferida())
            return
        message_id = getattr(query.message, "message_id", None)
        await query.edit_message_text(
            f"🔑 Sesión preferida para @{username}\n\nElegí la cuenta de Instagram. Si todavía no tiene acceso registrado, se comprobará sólo esa cuenta antes de guardar.",
            reply_markup=menu_elegir_sesion_preferida(username, context, message_id),
        )
        return

    if data.startswith("pref_pick:"):
        token = data.split(":", 1)[1]
        seleccion = context.user_data.get("preferencia_seleccion") or {}
        sesion_elegida = seleccion.get("elecciones", {}).get(token)
        username = seleccion.get("username")
        message_id = getattr(query.message, "message_id", None)
        if not sesion_elegida or not username or seleccion.get("message_id") != message_id:
            await query.edit_message_text("Esta selección quedó desactualizada. Volvé a elegir el perfil.",
                                          reply_markup=menu_perfiles_sesion_preferida())
            return
        if buscar_cuenta(username) is None:
            context.user_data.pop("preferencia_seleccion", None)
            await query.edit_message_text("Ese perfil ya no está agregado.", reply_markup=menu_perfiles_sesion_preferida())
            return
        await query.edit_message_text(f"⏳ Aplicando la sesión elegida para @{username}...")
        try:
            async with IG_LOCK:
                vinculo = await asyncio.to_thread(
                    cambiar_sesion_preferida, username, sesion_elegida.id,
                    lambda: comprobar_perfil_accesible(username), sesion_esperada=sesion_elegida,
                )
            sesiones = listar_sesiones()
            nombre = nombres_sesiones(sesiones).get(vinculo["sesion_id"]) or vinculo.get("cuenta")
            etiqueta = f"@{nombre}" if nombre else vinculo["sesion_id"]
            texto = f"✅ @{username}\nSesión preferida: {etiqueta} ({vinculo['sesion_id']}).\nTu elección queda guardada como predeterminada."
        except Exception as error:
            _icono, tipo, _recomendacion = diagnosticar_error(error)
            texto = f"❌ No se cambió la sesión preferida de @{username}.\n{tipo}: {str(error)[:400]}"
        context.user_data.pop("preferencia_seleccion", None)
        await query.edit_message_text(texto, reply_markup=menu_perfiles_sesion_preferida())
        return

    if data == "sessions_update":
        context.user_data.clear()
        try:
            nuevas = await asyncio.to_thread(listar_sesiones_nuevas)
            if not nuevas:
                await query.edit_message_text(
                    "ℹ️ No hay sesiones nuevas ni actualizaciones pendientes.\n\n"
                    "Todas las sesiones habilitadas ya tienen asociaciones.",
                    reply_markup=menu_gestion(),
                )
                return
            token = uuid.uuid4().hex[:12]
            context.user_data["actualizar_sesiones_seleccion"] = {
                "token": token,
                "message_id": getattr(query.message, "message_id", None),
                "sesiones": nuevas,
            }
            filas = [
                [InlineKeyboardButton(
                    sesion.etiqueta,
                    callback_data=f"session_add_access:{token}:{posicion}",
                )]
                for posicion, sesion in enumerate(nuevas)
            ]
            filas.append([InlineKeyboardButton("‹ Volver", callback_data="manage")])
            await query.edit_message_text(
                "🔄 Actualizar sesiones\n\n"
                "Elegí una sesión nueva o una actualización pendiente. "
                "Se comprobará su acceso a los perfiles agregados y se añadirá como respaldo.\n\n"
                "Las sesiones preferidas se conservan.",
                reply_markup=InlineKeyboardMarkup(filas),
            )
        except Exception as error:
            await query.edit_message_text(
                f"❌ No se pudieron listar las sesiones: {str(error)[:500]}",
                reply_markup=menu_gestion(),
            )
        return

    if data.startswith("session_add_access:"):
        partes = data.split(":")
        seleccion = context.user_data.get("actualizar_sesiones_seleccion")
        valida = (
            len(partes) == 3
            and isinstance(seleccion, dict)
            and partes[1] == seleccion.get("token")
            and partes[2].isascii()
            and partes[2].isdigit()
            and seleccion.get("message_id") == getattr(query.message, "message_id", None)
            and int(partes[2]) < len(seleccion.get("sesiones", []))
        )
        if not valida:
            await query.edit_message_text(
                "ℹ️ Esta selección venció. Abrí Actualizar sesiones nuevamente.",
                reply_markup=menu_gestion(),
            )
            return
        elegida = seleccion["sesiones"][int(partes[2])]
        context.user_data.pop("actualizar_sesiones_seleccion", None)
        cuentas_para_actualizar = cargar_cuentas()
        objetivos = [cuenta["username"] for cuenta in cuentas_para_actualizar]
        ids_perfiles = {cuenta["username"]: cuenta.get("user_id") for cuenta in cuentas_para_actualizar}
        if not objetivos:
            await query.edit_message_text(
                "ℹ️ No hay perfiles agregados para comprobar.", reply_markup=menu_gestion()
            )
            return
        progreso_lock = threading.Lock()
        progreso_estado = {
            "total": len(objetivos), "procesados": 0, "añadidos": 0,
            "sin_acceso": 0, "fallidas": 0, "pendientes": len(objetivos),
            "username_actual": None, "etapa": "esperando",
        }
        inicio = time.monotonic()

        def recibir_progreso_asociaciones(datos: dict) -> None:
            with progreso_lock:
                progreso_estado.update(datos)

        def actualizar_accesos_desde_paginas() -> dict:
            # El navegador se crea al verificar el primer perfil bajo la
            # sesión elegida y se reutiliza durante todo el lote.
            with comprobar_perfiles_desde_pagina() as verificar:
                return actualizar_accesos_sesion(
                    elegida.id,
                    objetivos,
                    lambda username: verificar(username, user_id=ids_perfiles.get(username)),
                    sesion_esperada=elegida,
                    progreso=recibir_progreso_asociaciones,
                )

        def texto_progreso_asociaciones() -> str:
            with progreso_lock:
                estado = dict(progreso_estado)
            segundos = max(0, int(time.monotonic() - inicio))
            minutos, resto = divmod(segundos, 60)
            tiempo = f"{minutos} min {resto:02d} s" if minutos else f"{resto} s"
            actual = estado.get("username_actual")
            consulta = f"Comprobando @{actual}" if actual else "Preparando comprobación"
            return (
                f"⏳ Actualizando accesos de {elegida.etiqueta}...\n\n"
                f"🟢 Estado: {consulta}\n"
                f"🕒 Tiempo: {tiempo}\n"
                f"⚙️ Perfiles comprobados: {estado['procesados']}/{estado['total']}\n"
                f"✅ Con acceso: {estado['añadidos']}\n"
                f"🔒 Sin acceso: {estado['sin_acceso']}\n"
                f"⚠️ No se pudieron comprobar: {estado['fallidas']}\n"
                f"📋 Pendientes: {estado['pendientes']}\n\n"
                "Actualización automática cada 10 segundos."
            )

        await query.edit_message_text(texto_progreso_asociaciones())

        async def refrescar_asociaciones() -> None:
            while True:
                await asyncio.sleep(10)
                try:
                    await query.edit_message_text(texto_progreso_asociaciones())
                except TelegramError as error:
                    if "not modified" not in str(error).lower():
                        logger.warning("No se pudo mostrar el progreso de accesos: %s", error)

        tarea = asyncio.create_task(refrescar_asociaciones())
        try:
            try:
                async with IG_LOCK:
                    tarea_accesos = asyncio.create_task(asyncio.to_thread(
                        actualizar_accesos_desde_paginas,
                    ))
                    try:
                        resultado = await asyncio.shield(tarea_accesos)
                    except asyncio.CancelledError:
                        # Cancelar el callback no detiene el hilo. Conservar el
                        # bloqueo hasta que termine evita consultas solapadas.
                        while not tarea_accesos.done():
                            try:
                                await asyncio.shield(tarea_accesos)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                        if not tarea_accesos.cancelled() and tarea_accesos.exception():
                            logger.warning("La actualización cancelada terminó con un error: %s",
                                           tarea_accesos.exception())
                        raise
            finally:
                tarea.cancel()
                try:
                    await tarea
                except asyncio.CancelledError:
                    pass
            interrumpida = resultado["interrumpida"]
            parcial = not interrumpida and (
                resultado.get("etapa") == "parcial" or resultado["pendientes"] > 0
            )
            estado_final = (
                "⚠️ Actualización interrumpida" if interrumpida else
                "⚠️ Actualización parcial" if parcial else
                "✅ Actualización terminada"
            )
            texto = (
                f"{estado_final}\n\n"
                f"Sesión: {elegida.etiqueta}\n"
                f"Perfiles comprobados: {resultado['procesados']}/{resultado['total']}\n"
                f"Con acceso / respaldo añadido: {resultado['añadidos']}\n"
                f"Sin acceso: {resultado['sin_acceso']}\n"
                f"No se pudieron comprobar: {resultado['fallidas']}\n"
                f"Pendientes: {resultado['pendientes']}\n\n"
                "Las sesiones preferidas se conservaron."
            )
            if interrumpida:
                texto += (
                    f"\n\nMotivo: {str(resultado.get('motivo') or '')[:500]}\n"
                    "Los accesos confirmados quedaron guardados. "
                    "Podés continuar desde Actualizar sesiones después de resolver el problema."
                )
            elif parcial:
                texto += (
                    "\n\nLos accesos confirmados quedaron guardados. "
                    "Podés continuar desde Actualizar sesiones para comprobar los perfiles pendientes."
                )
        except Exception as error:
            texto = f"❌ No se pudo completar la actualización de {elegida.etiqueta}.\n{str(error)[:700]}"
        await query.edit_message_text(texto, reply_markup=menu_gestion())
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
                    f"{'⭐' if cuenta.get('destacada') else '•'} {esc(cuenta['nombre'])} — "
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
                boton_cuenta(
                    c,
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

        with CUENTAS_LOCK:
            cuentas = [c for c in cargar_cuentas() if c["username"].casefold() != username.casefold()]
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
        await query.edit_message_text(
            "⚙️ Programar revisiones\n\nElegí la modalidad de sesiones:",
            reply_markup=menu_modalidades_programacion(),
        )
        return

    if data in ("schedules_normal", "schedules_variable"):
        modalidad = "variable" if data == "schedules_variable" else "normal"
        context.user_data.clear()
        context.user_data[SCHED_MODALIDAD] = modalidad
        disponibles = (
            cuentas_disponibles_para_programar()
        )

        if disponibles:
            texto = (
                f"⚙️ <b>Programaciones {'variables' if modalidad == 'variable' else 'normales'}</b>\n\n"
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
            reply_markup=menu_programaciones(modalidad),
        )
        return

    if data.startswith(("sched:", "schedv:")):
        modalidad = "variable" if data.startswith("schedv:") else "normal"
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        if buscar_cuenta(username) is None:
            await query.edit_message_text("Ese perfil ya no está agregado.",
                                          reply_markup=menu_programaciones(modalidad))
            return

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
                reply_markup=menu_programaciones(modalidad),
            )
            return

        cache = (context.user_data.get(SCHED_USERNAME) == username
                 and context.user_data.get(SCHED_MODALIDAD) == modalidad
                 and context.user_data.get(SCHED_SESIONES))
        if modalidad == "variable" and not cache:
            context.user_data.clear()
            await query.edit_message_text(f"⏳ Comprobando qué sesiones tienen acceso a @{username}...")
            try:
                async with IG_LOCK:
                    perfil = await asyncio.to_thread(verificar_accesos_perfil, username,
                        lambda: comprobar_perfil_accesible(username))
                sesiones = sesiones_rotacion_confirmadas(perfil)
                if not sesiones:
                    raise ConfiguracionSesionesError("Ninguna sesión confirmó acceso al perfil.")
            except Exception as error:
                _icono, tipo, _recomendacion = diagnosticar_error(error)
                await query.edit_message_text(f"❌ No se pudo preparar la programación variable.\n{tipo}: {str(error)[:1000]}",
                                              reply_markup=menu_programaciones("variable"))
                return
            context.user_data.update({SCHED_USERNAME: username, SCHED_MODALIDAD: modalidad,
                                      SCHED_SESIONES_DETECTADAS: sesiones,
                                      SCHED_SESIONES_ELEGIDAS: None,
                                      SCHED_SESIONES: sesiones})
            await query.edit_message_text(
                f"🔄 @{username}\n\nSe detectaron {len(sesiones)} sesión(es) con acceso. "
                "Elegí una o varias para las ejecuciones automáticas:",
                reply_markup=menu_seleccionar_sesiones_variable(
                    username, context, getattr(query.message, "message_id", None)),
            )
            return
        if modalidad == "variable" and not context.user_data.get(SCHED_SESIONES_ELEGIDAS):
            await query.edit_message_text(
                f"🔄 @{username}\n\nElegí una o varias sesiones para las ejecuciones automáticas:",
                reply_markup=menu_seleccionar_sesiones_variable(
                    username, context, getattr(query.message, "message_id", None)),
            )
            return
        if modalidad == "normal":
            context.user_data.clear()
            context.user_data.update({SCHED_USERNAME: username, SCHED_MODALIDAD: "normal"})
        conservar_flujo_programacion(context, username, modalidad)
        cantidad = len(context.user_data[SCHED_SESIONES])
        resumen = (f"🔄 Variable: {cantidad} sesión(es) con acceso confirmado.\n"
                   + ("Sólo hay una: las revisiones usarán esa cuenta.\n" if cantidad == 1 else "")) if modalidad == "variable" else ""

        await query.edit_message_text(
            f"⏰ @{username}\n\n"
            f"{resumen}"
            "¿Cómo querés programar las revisiones?",
            reply_markup=menu_tipo_programacion(
                username, modalidad
            ),
        )
        return

    if data.startswith("schedv_session_toggle:"):
        token = data.split(":", 1)[1]
        if getattr(query.message, "message_id", None) != context.user_data.get(SCHED_SELECCION_MESSAGE_ID):
            await query.edit_message_text(
                "Esta selección quedó desactualizada. Volvé a elegir las sesiones.",
                reply_markup=menu_programaciones("variable"),
            )
            return
        username = context.user_data.get(SCHED_USERNAME)
        sesion_id = (context.user_data.get(SCHED_SELECCION_TOKENS) or {}).get(token)
        detectadas = set(context.user_data.get(SCHED_SESIONES_DETECTADAS) or [])
        if (context.user_data.get(SCHED_MODALIDAD) != "variable" or not username
                or sesion_id not in detectadas):
            await query.edit_message_text(
                "Esta selección quedó desactualizada. Volvé a verificar el perfil.",
                reply_markup=menu_programaciones("variable"),
            )
            return
        elegidas = list(context.user_data.get(SCHED_SESIONES_ELEGIDAS) or [])
        if sesion_id in elegidas:
            elegidas.remove(sesion_id)
        else:
            elegidas.append(sesion_id)
        context.user_data[SCHED_SESIONES_ELEGIDAS] = elegidas
        await query.edit_message_text(
            f"🔄 @{username}\n\nElegidas: {len(elegidas)} de {len(detectadas)}. "
            "Podés seleccionar una o varias:",
            reply_markup=menu_seleccionar_sesiones_variable(
                username, context, getattr(query.message, "message_id", None)),
        )
        return

    if data == "schedv_session_confirm":
        username = context.user_data.get(SCHED_USERNAME)
        elegidas = list(context.user_data.get(SCHED_SESIONES_ELEGIDAS) or [])
        detectadas = set(context.user_data.get(SCHED_SESIONES_DETECTADAS) or [])
        if (getattr(query.message, "message_id", None) != context.user_data.get(SCHED_SELECCION_MESSAGE_ID)
                or context.user_data.get(SCHED_MODALIDAD) != "variable"
                or not username or not elegidas or any(sesion not in detectadas for sesion in elegidas)):
            await query.edit_message_text(
                "Elegí al menos una sesión válida para continuar.",
                reply_markup=menu_seleccionar_sesiones_variable(
                    username or "", context, getattr(query.message, "message_id", None)),
            )
            return
        context.user_data[SCHED_SESIONES] = elegidas
        context.user_data.pop(SCHED_SESIONES_DETECTADAS, None)
        context.user_data.pop(SCHED_SELECCION_TOKENS, None)
        context.user_data.pop(SCHED_SELECCION_MESSAGE_ID, None)
        await query.edit_message_text(
            f"✅ @{username}\n\nSe usarán {len(elegidas)} sesión(es) elegida(s).\n\n"
            "¿Cómo querés programar las revisiones?",
            reply_markup=menu_tipo_programacion(username, "variable"),
        )
        return

    if data.startswith(("sched_mode_daily:", "schedv_mode_daily:")):
        modalidad = "variable" if data.startswith("schedv_") else "normal"
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        if not conservar_flujo_programacion(context, username, modalidad):
            await query.edit_message_text("Esta selección quedó desactualizada. Elegí nuevamente el perfil.",
                                          reply_markup=menu_programaciones(modalidad))
            return

        await query.edit_message_text(
            f"📅 @{username}\n\n"
            "¿Cuántas revisiones por día?",
            reply_markup=menu_cantidad(
                username, modalidad
            ),
        )
        return

    if data.startswith(("sched_mode_interval:", "schedv_mode_interval:")):
        modalidad = "variable" if data.startswith("schedv_") else "normal"
        username = limpiar_username(
            data.split(":", 1)[1]
        )

        if not conservar_flujo_programacion(context, username, modalidad):
            await query.edit_message_text("Esta selección quedó desactualizada. Elegí nuevamente el perfil.",
                                          reply_markup=menu_programaciones(modalidad))
            return

        await query.edit_message_text(
            f"⏱ @{username}\n\n"
            "Elegí cada cuánto tiempo revisar.\n\n"
            "Después vas a elegir el minuto exacto de la hora "
            "en que querés hacer las revisiones.",
            reply_markup=menu_intervalos(
                username, modalidad
            ),
        )
        return

    if data.startswith(("sched_interval:", "schedv_interval:")):
        modalidad = "variable" if data.startswith("schedv_") else "normal"
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
                reply_markup=menu_programaciones(modalidad),
            )
            return

        if not conservar_flujo_programacion(context, username, modalidad):
            await query.edit_message_text("Esta selección quedó desactualizada. Elegí nuevamente el perfil.",
                                          reply_markup=menu_programaciones(modalidad))
            return
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

    if data.startswith(("sched_count:", "schedv_count:")):
        modalidad = "variable" if data.startswith("schedv_") else "normal"
        _, username, cantidad = data.split(":", 2)
        username = limpiar_username(username)
        cantidad = int(cantidad)

        if not 1 <= cantidad <= 6:
            await query.edit_message_text("Cantidad de revisiones inválida.", reply_markup=menu_programaciones(modalidad))
            return
        if not conservar_flujo_programacion(context, username, modalidad):
            await query.edit_message_text("Esta selección quedó desactualizada. Elegí nuevamente el perfil.",
                                          reply_markup=menu_programaciones(modalidad))
            return
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
            "⏳ <b>Comprobando el feed de las sesiones...</b>\nEsperando turno si hay otra consulta en curso.",
            parse_mode="HTML",
        )
        cuentas = cargar_cuentas()
        programaciones = db.listar_programaciones(AUTHORIZED_CHAT_ID)
        progreso_lock = threading.Lock()
        progreso = {}
        detener = threading.Event()
        cambio = asyncio.Event()
        loop = asyncio.get_running_loop()

        def recibir_progreso(datos):
            with progreso_lock:
                progreso.update(datos)
            loop.call_soon_threadsafe(cambio.set)

        async def refrescar_estado():
            anterior = None
            while True:
                await cambio.wait()
                cambio.clear()
                with progreso_lock:
                    actual = dict(progreso)
                cuenta = actual.get("actual", {})
                nombre = cuenta.get("username")
                etiqueta = f"@{nombre} ({cuenta.get('id', '')})" if nombre else cuenta.get("id", "")
                texto = (
                    "⏳ <b>Comprobando el feed de las sesiones...</b>\n\n"
                    f"Cuenta: <b>{esc(etiqueta)}</b>\n"
                    f"Comprobadas: <b>{actual.get('procesadas', 0)}/{actual.get('total', 0)}</b>"
                )
                if texto != anterior:
                    try:
                        await query.edit_message_text(texto, parse_mode="HTML")
                        anterior = texto
                    except TelegramError:
                        logger.warning("No se pudo actualizar el progreso de Estado.")
                # Sólo regula las ediciones de Telegram; no pausa sesiones.
                await asyncio.sleep(2)

        tarea_progreso = asyncio.create_task(refrescar_estado())
        try:
            async with IG_LOCK:
                tarea_estado = asyncio.create_task(asyncio.to_thread(
                    _estado_sesiones_feed, recibir_progreso, detener,
                ))
                try:
                    estados = await asyncio.shield(tarea_estado)
                except asyncio.CancelledError:
                    detener.set()
                    # Mantener el bloqueo hasta que el navegador termine evita
                    # solapar el hilo cancelado con otra consulta de Instagram.
                    await asyncio.shield(tarea_estado)
                    raise
        except Exception as error:
            estados = []
            error_general = esc(str(error)[:600])
        else:
            error_general = None
        finally:
            tarea_progreso.cancel()
            try:
                await tarea_progreso
            except asyncio.CancelledError:
                pass

        cabecera = "ℹ️ <b>ESTADO DE SESIONES</b>\nComprobación actual del feed de Instagram.\n\n"
        partes = []
        texto = cabecera
        if error_general:
            texto += f"❌ No se pudo completar la comprobación: {error_general}\n\n"
        elif not estados:
            texto += "No hay sesiones habilitadas configuradas.\n\n"
        for estado in estados:
            nombre = estado.get("username")
            etiqueta = f"@{nombre} ({estado['id']})" if nombre else estado["id"]
            detalle = str(estado.get("detalle", ""))[:700]
            bloque = (
                f"{'✅' if estado['disponible'] else '❌'} <b>{esc(etiqueta)}</b>\n"
                f"{esc(detalle)}\n\n"
            )
            if len(texto) + len(bloque) > 3600:
                partes.append(texto)
                texto = cabecera
            texto += bloque
        resumen = (
            f"Perfiles: <b>{len(cuentas)}</b>\n"
            f"Programaciones: <b>{len(programaciones)}</b>\n"
            f"Historias: <code>{esc(str(HISTORYS_DIR))}</code>"
        )
        if len(texto) + len(resumen) > 3900:
            partes.append(texto)
            texto = cabecera
        partes.append(texto + resumen)
        # Cada entrada conserva su HTML completo, sin omitir ninguna sesión.
        await query.edit_message_text(
            partes[0], parse_mode="HTML",
            reply_markup=menu_estado() if len(partes) == 1 else None,
        )
        for indice, parte in enumerate(partes[1:], 1):
            await enviar_texto_bot(
                context, chat_id=AUTHORIZED_CHAT_ID, text=parte, parse_mode="HTML",
                reply_markup=menu_estado() if indice == len(partes) - 1 else None,
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
            f"⏳ Verificando acceso a @{username} con las sesiones disponibles..."
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
                    "Ninguna de las sesiones de Instagram tiene acceso a ese perfil, "
                    "por lo tanto no fue agregada."
                ),
                reply_markup=menu_gestion(),
            )
            return

        except RuntimeError as error:
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

        sesion_nombre = perfil.get("sesion_username") or perfil.get("sesion_id", "principal")
        cantidad_acceso = len(perfil.get("sesiones_con_acceso", []))
        await responder_texto(update,
            f"✅ @{username} agregada.\n"
            f"Instagram ID: {user_id}\n"
            f"Sesión preferida: {sesion_nombre}\n"
            f"Sesiones con acceso: {cantidad_acceso}",
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
            modalidad_sesiones=context.user_data.get(SCHED_MODALIDAD, "normal"),
            sesiones_rotacion=context.user_data.get(SCHED_SESIONES, []),
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
                f"{texto_modalidad_guardada(row_guardada)}"
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
            modalidad_sesiones=context.user_data.get(SCHED_MODALIDAD, "normal"),
            sesiones_rotacion=context.user_data.get(SCHED_SESIONES, []),
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
            f"{texto_modalidad_guardada(row_guardada)}",
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

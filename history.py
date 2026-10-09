# StoryPulse v2.1
# Created by FacuSecX https://github.com/FacuSecX/StoryPulse-Private

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError
from playwright.sync_api import sync_playwright
from instagram_sessions import (
    SinHistoriasDisponibles, PerfilPrivado, PerfilNoEncontrado,
    ErrorSesionInstagram, ErrorConsultaInstagram, comprobar_archivo_sesion, con_sesiones,
    obtener_ruta_sesion, guardar_estado_contexto, escribir_json_atomico, sesion_actual, usar_sesion,
    usar_playwright_sincronico,
    validar_respuesta_instagram, validar_pagina_instagram, validar_datos_instagram,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

RUTA_CACHE_IDS = BASE_DIR / "user_ids_cache.json"

STORIES_QUERY_HASH = os.getenv(
    "INSTAGRAM_STORIES_QUERY_HASH",
    "de8017ee0a7c9c45ec4260733d81ea31",
).strip()

PROFILE_DOC_ID = os.getenv(
    "INSTAGRAM_PROFILE_DOC_ID",
    "8759034877476257",
).strip()

ZONA_LOCAL = os.getenv(
    "STORYPULSE_TIMEZONE",
    "America/Argentina/Buenos_Aires",
).strip()

_BLOQUEO = threading.RLock()


@dataclass
class HistoriaDescargada:
    story_pk: str
    username: str
    contenido: bytes
    content_type: str
    extension: str
    ancho: int
    alto: int
    tomada_en: datetime | None
    hash_archivo: str
    es_video: bool


ImagenDescargada = HistoriaDescargada


def limpiar_username(valor: str) -> str:
    username = str(valor).strip().lstrip("@").strip("/")

    m = re.search(
        r"instagram\.com/(?:stories/)?([^/?#]+)",
        username,
        re.I,
    )
    if m:
        username = m.group(1)

    if not username:
        raise ValueError("El username está vacío.")

    if not re.fullmatch(r"[A-Za-z0-9._]+", username):
        raise ValueError(
            "El username solamente puede contener letras, "
            "números, puntos y guiones bajos."
        )

    return username.lower()


def comprobar_sesion_local() -> dict[str, Any]:
    """Valida localmente el archivo de la sesión seleccionada, sin consultas."""
    return comprobar_archivo_sesion()


def _cargar_cache() -> dict[str, int]:
    if not RUTA_CACHE_IDS.exists():
        return {}

    try:
        data = json.loads(RUTA_CACHE_IDS.read_text(encoding="utf-8"))
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}

    resultado: dict[str, int] = {}

    for key, value in data.items():
        try:
            uid = int(value)
        except (TypeError, ValueError):
            continue

        if uid > 0:
            resultado[str(key).lower()] = uid

    return resultado


def _guardar_cache(cache: dict[str, int]) -> None:
    temporal = RUTA_CACHE_IDS.with_suffix(".tmp")
    temporal.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporal, RUTA_CACHE_IDS)


def _buscar_usuario_en_json(
    obj: Any,
    username: str,
) -> list[int]:
    objetivo = username.casefold()
    encontrados: list[int] = []

    if isinstance(obj, dict):
        actual = str(obj.get("username", "")).casefold()

        if actual == objetivo:
            for key in ("id", "pk", "user_id", "pk_id", "strong_id__"):
                valor = obj.get(key)

                if valor is None:
                    continue

                texto = str(valor)

                if texto.isdigit():
                    uid = int(texto)
                    if uid > 0 and uid not in encontrados:
                        encontrados.append(uid)

        for valor in obj.values():
            for uid in _buscar_usuario_en_json(valor, username):
                if uid not in encontrados:
                    encontrados.append(uid)

    elif isinstance(obj, list):
        for valor in obj:
            for uid in _buscar_usuario_en_json(valor, username):
                if uid not in encontrados:
                    encontrados.append(uid)

    return encontrados


def _buscar_ids_en_html(html: str, username: str) -> list[int]:
    objetivo = re.escape(username)
    patrones = [
        rf'"username"\s*:\s*"{objetivo}".{{0,1800}}?"id"\s*:\s*"(\d+)"',
        rf'"id"\s*:\s*"(\d+)".{{0,1800}}?"username"\s*:\s*"{objetivo}"',
        rf'"username"\s*:\s*"{objetivo}".{{0,1800}}?"pk"\s*:\s*"?(\d+)"?',
        rf'"pk"\s*:\s*"?(\d+)"?.{{0,1800}}?"username"\s*:\s*"{objetivo}"',
    ]

    encontrados: list[int] = []

    for patron in patrones:
        for match in re.finditer(
            patron,
            html,
            re.I | re.S,
        ):
            uid = int(match.group(1))
            if uid > 0 and uid not in encontrados:
                encontrados.append(uid)

    return encontrados


def _crear_contexto(playwright):
    browser = playwright.chromium.launch(
        headless=True,
    )

    context = browser.new_context(
        storage_state=str(obtener_ruta_sesion()),
        viewport={"width": 1365, "height": 900},
        locale="es-AR",
        timezone_id=ZONA_LOCAL,
    )

    return browser, context


def _guardar_estado_contexto(context) -> None:
    guardar_estado_contexto(context)


@con_sesiones(vincular=False)
def resolver_user_id(
    username: str,
    *,
    forzar: bool = False,
) -> int:
    """
    Resuelve username -> ID numérico desde Instagram Web.

    Si ya está guardado en user_ids_cache.json, no hace ninguna consulta.
    """
    username = limpiar_username(username)

    with _BLOQUEO:
        cache = _cargar_cache()

        if not forzar and username in cache:
            return int(cache[username])

        comprobar_sesion_local()

        candidatos: list[int] = []

        with usar_playwright_sincronico(sync_playwright) as p:
            browser, context = _crear_contexto(p)

            try:
                page = context.new_page()

                def procesar_response(response):
                    url = response.url

                    if "instagram.com" not in url:
                        return

                    if not (
                        "/graphql/" in url
                        or "/api/" in url
                        or "profile" in url.lower()
                    ):
                        return

                    try:
                        ctype = (
                            response.headers.get("content-type", "")
                            or ""
                        ).lower()

                        if "json" not in ctype:
                            return

                        data = response.json()
                    except Exception:
                        return

                    for uid in _buscar_usuario_en_json(data, username):
                        if uid not in candidatos:
                            candidatos.append(uid)

                page.on("response", procesar_response)

                response = page.goto(
                    f"https://www.instagram.com/{username}/",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )

                validar_respuesta_instagram(page, response, f"abrir @{username}")

                page.wait_for_timeout(7_000)

                validar_pagina_instagram(page)

                try:
                    html = page.content()
                    for uid in _buscar_ids_en_html(html, username):
                        if uid not in candidatos:
                            candidatos.append(uid)
                except Exception:
                    pass

                # Conserva cualquier cookie/estado que Instagram haya
                # actualizado durante esta navegación correcta.
                _guardar_estado_contexto(
                    context
                )

            finally:
                context.close()
                browser.close()

        candidatos = list(dict.fromkeys(candidatos))

        if not candidatos:
            raise PerfilNoEncontrado(
                f"No pude resolver el ID numérico de @{username}."
            )

        if len(candidatos) > 1:
            raise ErrorConsultaInstagram(
                f"Instagram devolvió más de un ID candidato para @{username}: "
                + ", ".join(map(str, candidatos))
            )

        user_id = int(candidatos[0])
        cache[username] = user_id
        _guardar_cache(cache)

        return user_id


def _construir_profile_info_url(username: str, *, sin_historias: bool = False) -> str:
    """
    Consulta de perfil autenticada que pide explícitamente la información
    de relación entre la sesión y el perfil objetivo.
    """
    variables = {
        "data": {
            "count": 1,
            "include_relationship_info": True,
            "latest_besties_reel_media": not sin_historias,
            "latest_reel_media": not sin_historias,
        },
        "username": username,
        "__relay_internal__pv__PolarisIsLoggedInrelayprovider": True,
        "__relay_internal__pv__PolarisFeedShareMenurelayprovider": True,
    }

    params = {
        "doc_id": PROFILE_DOC_ID,
        "variables": json.dumps(
            variables,
            separators=(",", ":"),
        ),
    }

    return (
        "https://www.instagram.com/graphql/query/?"
        + urlencode(params)
    )


def _bool_explicito(valor: Any) -> bool | None:
    return valor if isinstance(valor, bool) else None


def _extraer_estado_perfil(
    obj: Any,
    username: str,
) -> list[dict[str, Any]]:
    """
    Busca todos los objetos JSON que describen exactamente al usuario objetivo
    y conserva sólo señales explícitas de privacidad/relación.
    """
    objetivo = username.casefold()
    encontrados: list[dict[str, Any]] = []

    def agregar_desde_dict(usuario: dict[str, Any], padre: dict[str, Any] | None = None) -> None:
        actual = str(usuario.get("username", "")).casefold()
        if actual != objetivo:
            return

        privado = _bool_explicito(usuario.get("is_private"))
        siguiendo: bool | None = None

        amistad = usuario.get("friendship_status")
        if not isinstance(amistad, dict) and isinstance(padre, dict):
            amistad = padre.get("friendship_status")

        if isinstance(amistad, dict):
            siguiendo = _bool_explicito(amistad.get("following"))

        if siguiendo is None:
            for fuente in (usuario, padre if isinstance(padre, dict) else {}):
                for clave in (
                    "following",
                    "followed_by_viewer",
                    "viewer_is_following",
                ):
                    valor = _bool_explicito(fuente.get(clave))
                    if valor is not None:
                        siguiendo = valor
                        break
                if siguiendo is not None:
                    break

        uid: int | None = None
        for clave in ("id", "pk", "user_id", "pk_id", "strong_id__"):
            valor = usuario.get(clave)
            if valor is None:
                continue
            texto = str(valor)
            if texto.isdigit() and int(texto) > 0:
                uid = int(texto)
                break

        if privado is not None or siguiendo is not None or uid is not None:
            encontrados.append(
                {
                    "user_id": uid,
                    "is_private": privado,
                    "following": siguiendo,
                }
            )

    def recorrer(valor: Any, padre: dict[str, Any] | None = None, profundidad: int = 0) -> None:
        if profundidad > 60:
            return
        if isinstance(valor, dict):
            agregar_desde_dict(valor, padre)

            usuario_hijo = valor.get("user")
            if isinstance(usuario_hijo, dict):
                agregar_desde_dict(usuario_hijo, valor)

            for hijo in valor.values():
                recorrer(hijo, valor, profundidad + 1)

        elif isinstance(valor, list):
            for hijo in valor:
                recorrer(hijo, padre, profundidad + 1)
        elif isinstance(valor, str) and objetivo in valor.casefold() and valor.lstrip().startswith(("{", "[")):
            try:
                recorrer(json.loads(valor), padre, profundidad + 1)
            except (ValueError, RecursionError):
                pass

    recorrer(obj)
    return encontrados


def _resumir_estado_perfil(estados, username: str, user_id: int):
    ids = {int(e["user_id"]) for e in estados if e.get("user_id")}
    if any(uid != user_id for uid in ids):
        raise ErrorConsultaInstagram(f"Instagram devolvió un ID inconsistente para @{username}.")
    privados = [e["is_private"] for e in estados if e.get("is_private") is not None]
    seguimientos = [e["following"] for e in estados if e.get("following") is not None]
    privado = True if True in privados else (False if False in privados else None)
    siguiendo = True if True in seguimientos else (False if False in seguimientos else None)
    return privado, siguiendo


def _estados_en_html(html: str, username: str):
    class ScriptsJSON(HTMLParser):
        def __init__(self):
            super().__init__()
            self.partes = None
            self.estados = []

        def handle_starttag(self, tag, attrs):
            if tag == "script" and dict(attrs).get("type", "").lower() in ("application/json", "text/json"):
                self.partes = []

        def handle_data(self, data):
            if self.partes is not None:
                self.partes.append(data)

        def handle_endtag(self, tag):
            if tag == "script" and self.partes is not None:
                try:
                    self.estados.extend(_extraer_estado_perfil(json.loads("".join(self.partes)), username))
                except (ValueError, RecursionError):
                    pass
                self.partes = None

    parser = ScriptsJSON()
    parser.feed(html)
    return parser.estados


def _ruta_publicacion_del_perfil(ruta: str, username: str) -> bool:
    """Formatos Web antiguos y actuales; nunca enlaces a otro perfil."""
    partes = str(ruta).strip("/").split("/")
    if len(partes) == 3:
        if partes[0].casefold() != username.casefold():
            return False
        partes = partes[1:]
    return (len(partes) == 2 and partes[0] in ("p", "reel")
            and re.fullmatch(r"[A-Za-z0-9_-]+", partes[1]) is not None)


def _visual_perfil_cargado(page) -> dict[str, Any]:
    """Sólo observa la página; no abre publicaciones ni el visor de Stories."""
    visual = page.evaluate("""() => {
        const visible = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
        const main = document.querySelector('main,[role="main"]');
        const header = main && main.querySelector('header');
        const candidatos = main ? [...main.querySelectorAll('a[href]')].filter(e => {
            if (!visible(e) || e.closest('header,aside,nav,[role="navigation"]')) return false;
            if (e.closest('[aria-label="Sugerencias para ti"],[aria-label="Suggested for you"],[data-testid*="suggest"]')) return false;
            const zona = e.closest('section,[role="region"]');
            const tituloZona = zona && zona.querySelector('h2,h3,[role="heading"]');
            if (tituloZona && /^(sugerencias para ti|suggested for you|suggestions for you|recommended for you)$/i.test(tituloZona.innerText.trim())) return false;
            const miniatura = e.querySelector('img,video');
            if (!miniatura || !visible(miniatura)) return false;
            const url = new URL(e.href, location.href);
            return ['instagram.com','www.instagram.com'].includes(url.hostname);
        }).map(e => new URL(e.href, location.href).pathname) : [];
        return {
            titulos: header ? [...header.querySelectorAll('h1,h2')].filter(visible).map(e => e.textContent.trim()) : [],
            botones: header ? [...header.querySelectorAll('button,[role="button"]')].filter(visible)
                .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim()) : [],
            enlaces_publicaciones: candidatos,
            login: [...document.querySelectorAll('input[type="password"]')].some(visible),
            captcha: [...document.querySelectorAll('iframe[src*="recaptcha"],iframe[src*="hcaptcha"]')].some(visible),
            autenticado: [...document.querySelectorAll('a[href*="/direct/inbox"],a[href*="/accounts/edit"]')].some(visible),
            login_invitado: [...document.querySelectorAll('header a[href*="/accounts/login"],main a[href*="/accounts/login"]')].some(visible),
            texto: main ? main.innerText : document.body.innerText
        };
    }""")
    if isinstance(visual, dict) and "enlaces_publicaciones" in visual:
        username = urlsplit(str(page.url)).path.strip("/").casefold()
        visual["publicaciones"] = any(_ruta_publicacion_del_perfil(ruta, username)
                                       for ruta in visual["enlaces_publicaciones"])
    return visual


def _datos_json_respuesta_recibida(response) -> list[Any]:
    """Decodifica datos ya recibidos; no ejecuta JavaScript ni hace consultas.

    Instagram también entrega JSON con prefijo anti-XSSI bajo text/javascript,
    y algunas respuestas Relay contienen varios objetos JSON consecutivos.
    """
    ctype = response.headers.get("content-type", "").lower()
    if "json" not in ctype and "javascript" not in ctype:
        return []
    try:
        data = response.json()
    except Exception:
        data = None
    if isinstance(data, (dict, list)):
        return [data]
    try:
        texto = response.text()
    except Exception:
        return []
    if not isinstance(texto, str) or len(texto) > 8_000_000:
        return []
    texto = texto.lstrip("\ufeff \t\r\n")
    texto = re.sub(r"^(?:for\s*\(\s*;\s*;\s*\)|while\s*\(\s*1\s*\))\s*;\s*", "", texto, count=1)
    if texto.startswith(")]}'"):
        texto = texto[4:].lstrip(", \t\r\n")
    decoder = json.JSONDecoder()
    encontrados = []
    posicion = 0
    while posicion < len(texto) and len(encontrados) < 128:
        while posicion < len(texto) and texto[posicion].isspace():
            posicion += 1
        if posicion == len(texto):
            break
        if texto[posicion] not in "{[":
            return []  # Nunca interpretar código JavaScript o valores sueltos.
        try:
            valor, posicion = decoder.raw_decode(texto, posicion)
        except (ValueError, RecursionError):
            return []
        if not isinstance(valor, (dict, list)):
            return []
        encontrados.append(valor)
    return encontrados if posicion == len(texto) else []


@contextmanager
def comprobar_perfiles_desde_pagina(*, espera_maxima: float = 10.0):
    """Un lote de accesos mediante una sola apertura normal de cada perfil.

    El navegador se crea en la primera llamada, bajo la sesión elegida por el
    actualizador. Se reutilizan su contexto y página hasta cerrar el lote.
    No se resuelven IDs por red, ni se consultan endpoints adicionales.
    """
    stack = ExitStack()
    recursos: dict[str, Any] = {}
    observacion: dict[str, Any] = {"username": None, "estados": [], "errores": [], "generacion": 0}
    solicitudes: dict[Any, int] = {}

    def iniciar():
        actual = sesion_actual()
        if actual is None:
            raise ErrorSesionInstagram("archivo", "No hay una sesión seleccionada para comprobar perfiles.")
        if recursos:
            if actual != recursos["sesion"]:
                raise ErrorConsultaInstagram("La sesión cambió durante la comprobación de perfiles.")
            return recursos["page"]
        comprobar_sesion_local()
        stack.enter_context(_BLOQUEO)
        playwright = stack.enter_context(usar_playwright_sincronico(sync_playwright))
        browser, context = _crear_contexto(playwright)
        stack.callback(browser.close)
        stack.callback(context.close)
        page = context.new_page()
        stack.callback(page.close)
        recursos.update(sesion=actual, context=context, page=page, ultimo_estado=None)

        def solicitud_iniciada(request):
            # Una respuesta tardía de la página anterior no pertenece al perfil
            # actual, aunque su cuerpo incluya su nombre entre recomendaciones.
            solicitudes[request] = observacion["generacion"]

        def solicitud_terminada(request):
            """Lee respuestas que el perfil ya pidió; nunca inicia consultas."""
            objetivo = observacion["username"]
            if not objetivo or solicitudes.pop(request, None) != observacion["generacion"]:
                return
            try:
                origen = request.headers.get("referer", "")
                url_origen = urlsplit(origen)
                # Las peticiones al subdominio i.instagram.com pueden llevar
                # sólo el origen por Referrer-Policy, sin la ruta del perfil.
                # La generación sigue acotada a esta navegación y el frame
                # aporta su ruta exacta, sin pedir ningún recurso adicional.
                if not origen or (url_origen.hostname in ("instagram.com", "www.instagram.com")
                                  and not url_origen.path.rstrip("/")):
                    url_origen = urlsplit(str(request.frame.url))
                if url_origen.hostname not in ("instagram.com", "www.instagram.com"):
                    return
                if url_origen.path.rstrip("/").casefold() != "/" + objetivo:
                    return
                response = request.response()
                if response is None:
                    return
                url = urlsplit(response.url)
                if url.hostname not in ("instagram.com", "www.instagram.com", "i.instagram.com"):
                    return
                if not any(ruta in url.path for ruta in ("/api/", "/graphql/")):
                    return
                if int(response.status) == 429:
                    observacion["errores"].append(ErrorSesionInstagram(
                        "rate_limit", f"Instagram respondió HTTP 429 al cargar el perfil de @{objetivo}.", http_status=429,
                    ))
                for data in _datos_json_respuesta_recibida(response):
                    observacion["estados"].extend(_extraer_estado_perfil(data, objetivo))
                    # Un recurso accesorio rechazado no invalida un perfil visible.
                    # Se conserva el diagnóstico sólo si la página no confirma acceso.
                    try:
                        validar_datos_instagram(data)
                    except ErrorSesionInstagram as error:
                        observacion["errores"].append(error)
            except Exception:
                pass

        page.on("request", solicitud_iniciada)
        page.on("requestfinished", solicitud_terminada)
        return page

    def verificar(username: str, *, user_id: int | None = None) -> dict[str, Any]:
        username = limpiar_username(username)
        page = iniciar()
        solicitudes.clear()
        observacion.update(username=username, estados=[], errores=[], generacion=observacion["generacion"] + 1)
        conocido = user_id if user_id is not None else _cargar_cache().get(username)
        try:
            conocido = int(conocido) if conocido is not None else None
        except (TypeError, ValueError):
            conocido = None
        if conocido is not None and conocido <= 0:
            conocido = None
        response = page.goto(
            f"https://www.instagram.com/{username}/",
            wait_until="domcontentloaded", timeout=60_000,
        )
        # HTTP de la navegación y redirecciones reales sí detienen el lote.
        validar_respuesta_instagram(page, response, f"abrir el perfil de @{username}")
        limite = time.monotonic() + max(0.0, float(espera_maxima))
        while True:
            validar_pagina_instagram(page)
            ruta = urlsplit(str(page.url)).path.rstrip("/").casefold()
            if ruta != "/" + username:
                raise ErrorConsultaInstagram("Instagram abrió una página distinta del perfil solicitado.")
            estados = list(observacion["estados"])
            try:
                estados.extend(_estados_en_html(page.content(), username))
            except Exception:
                pass
            ids = {int(e["user_id"]) for e in estados if e.get("user_id")}
            if (conocido is not None and any(uid != conocido for uid in ids)) or len(ids) > 1:
                raise ErrorConsultaInstagram(f"Instagram devolvió un ID inconsistente para @{username}.")
            uid = conocido or next(iter(ids), None)
            privados = [e["is_private"] for e in estados if e.get("is_private") is not None]
            siguiendo_valores = [e["following"] for e in estados if e.get("following") is not None]
            privado = True if True in privados else (False if False in privados else None)
            siguiendo = True if True in siguiendo_valores else (False if False in siguiendo_valores else None)
            visual = _visual_perfil_cargado(page)
            visual = visual if isinstance(visual, dict) else {}
            if visual.get("login") is True:
                raise ErrorSesionInstagram("login", "Instagram mostró el formulario de inicio de sesión.")
            if visual.get("captcha") is True:
                raise ErrorSesionInstagram("verificacion", "Instagram mostró una verificación/CAPTCHA manual.")
            autenticado = visual.get("autenticado") is True
            if not autenticado and visual.get("login_invitado") is True:
                raise ErrorSesionInstagram("login", "Instagram mostró el perfil sin una sesión autenticada.")
            titulos = visual.get("titulos", [])
            titulo_correcto = any(str(t).strip().lstrip("@").casefold() == username for t in titulos)
            labels = {str(b).strip().casefold() for b in visual.get("botones", [])} if titulo_correcto else set()
            if labels & {"siguiendo", "following"}:
                siguiendo = True
            elif labels & {"solicitado", "requested", "seguir", "follow", "seguir también", "follow back"}:
                siguiendo = False
            lineas = {linea.strip().rstrip(".! ").casefold() for linea in str(visual.get("texto", "")).splitlines()}
            aviso_privado = bool(lineas & {"esta cuenta es privada", "este perfil es privado",
                                           "this account is private", "this profile is private"})
            no_encontrado = bool(lineas & {"esta página no está disponible", "esta página no está disponible por el momento",
                                           "sorry, this page isn't available", "page isn't available"})
            if no_encontrado:
                raise PerfilNoEncontrado(f"Instagram no encontró el perfil @{username}.")
            if aviso_privado and titulo_correcto and autenticado:
                if privado is False or siguiendo is True:
                    raise ErrorConsultaInstagram("Instagram mostró señales contradictorias de acceso al perfil.")
                recordar_estado_sano()
                raise PerfilPrivado(f"La sesión autenticada no tiene acceso a @{username}.")
            # El perfil debe estar montado y autenticado. Metadatos aislados o
            # recomendaciones no prueban que su página se haya mostrado.
            identidad = titulo_correcto and autenticado
            publicaciones_visibles = titulo_correcto and visual.get("publicaciones") is True
            if identidad and privado is True and siguiendo is False:
                recordar_estado_sano()
                raise PerfilPrivado(f"La sesión autenticada no tiene acceso a @{username}.")
            if identidad and (siguiendo is True or privado is False or (privado is None and publicaciones_visibles)):
                recordar_estado_sano()
                return {"username": username, "user_id": uid, "is_private": privado,
                        "following": siguiendo, "acceso_confirmado": True}
            if time.monotonic() >= limite:
                if observacion["errores"]:
                    raise observacion["errores"][0]
                raise ErrorConsultaInstagram(
                    f"La página de @{username} no permitió confirmar el acceso: "
                    "no mostró una cabecera autenticada con publicaciones, relación o privacidad explícitas."
                )
            page.wait_for_timeout(250)

    def recordar_estado_sano():
        # Sólo estado local del navegador. Si la siguiente página falla, se
        # conserva este último estado correcto en lugar del interstitial final.
        try:
            recursos["ultimo_estado"] = recursos["context"].storage_state(indexed_db=True)
        except TypeError:
            recursos["ultimo_estado"] = recursos["context"].storage_state()

    try:
        yield verificar
    finally:
        try:
            if recursos and recursos["ultimo_estado"] is not None:
                # El actualizador ya salió de usar_sesion al cerrar el manager.
                with usar_sesion(recursos["sesion"]):
                    escribir_json_atomico(obtener_ruta_sesion(), recursos["ultimo_estado"])
        finally:
            stack.close()


def _comprobar_perfil_desde_pagina(context, username: str, user_id: int, estados):
    """Completa metadatos sin depender de Stories ni de publicaciones visibles."""
    estados = list(estados)
    errores_sesion = []
    page = context.new_page()

    def solicitud_terminada(request):
        try:
            response = request.response()
            if response is None:
                return
            url = urlsplit(response.url)
            if url.hostname not in ("instagram.com", "www.instagram.com", "i.instagram.com"):
                return
            if not any(ruta in url.path for ruta in ("/api/", "/graphql/")):
                return
            if int(response.status) == 429:
                validar_respuesta_instagram(response, response, f"cargar el perfil de @{username}")
            if "json" not in response.headers.get("content-type", "").lower():
                return
            data = response.json()
            validar_datos_instagram(data)
            estados.extend(_extraer_estado_perfil(data, username))
        except ErrorSesionInstagram as error:
            errores_sesion.append(error)
        except Exception:
            pass  # Recursos ajenos al perfil no determinan su acceso.

    page.on("requestfinished", solicitud_terminada)
    try:
        response = page.goto(f"https://www.instagram.com/{username}/", wait_until="domcontentloaded", timeout=60_000)
        validar_respuesta_instagram(page, response, f"abrir @{username}")
        page.wait_for_timeout(2_000)
        validar_pagina_instagram(page)
        # Primero aprovechar la página que ya se cargó. Un endpoint secundario
        # no debe recibir otra petición si «Siguiendo» ya confirma el permiso.
        for intento in range(4):
            if errores_sesion:
                raise errores_sesion[0]
            estados.extend(_estados_en_html(page.content(), username))
            privado, siguiendo = _resumir_estado_perfil(estados, username, user_id)
            if privado is False or siguiendo is True or (privado is True and siguiendo is False):
                return privado, siguiendo
            if urlsplit(str(page.url)).path.rstrip("/").casefold() != "/" + username.casefold():
                raise ErrorConsultaInstagram("Instagram abrió una página distinta del perfil solicitado.")
            texto = page.locator("body").inner_text().casefold()
            aviso_privado = any(linea.strip().rstrip(".! ") in ("esta cuenta es privada", "this account is private")
                                for linea in texto.splitlines())
            visual = page.evaluate("""() => {
                const h = document.querySelector('main header');
                if (!h) return {titulos: [], botones: []};
                const visibles = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
                return {
                    titulos: [...h.querySelectorAll('h1,h2')].filter(visibles).map(e => e.textContent.trim()),
                    botones: [...h.querySelectorAll('button,[role="button"]')].filter(visibles)
                        .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim())
                };
            }""")
            titulos = visual.get("titulos", []) if isinstance(visual, dict) else []
            botones = visual.get("botones", []) if isinstance(visual, dict) else []
            titulo_correcto = any(str(t).strip().lstrip("@").casefold() == username.casefold() for t in titulos)
            if titulo_correcto:
                labels = {str(b).strip().casefold() for b in botones}
                if labels & {"siguiendo", "following"}:
                    siguiendo = True
                elif labels & {"solicitado", "requested", "seguir", "follow", "seguir también", "follow back"}:
                    siguiendo = False
            if aviso_privado:
                if siguiendo is True:
                    raise ErrorConsultaInstagram("Instagram mostró señales contradictorias de acceso al perfil.")
                privado, siguiendo = True, False
            if errores_sesion:
                raise errores_sesion[0]
            if siguiendo is True or (privado is True and siguiendo is False):
                return privado, siguiendo
            if intento < 3:
                page.wait_for_timeout(1_000)
                validar_pagina_instagram(page)

        if errores_sesion:
            raise errores_sesion[0]
        # Sólo si la página sigue siendo indeterminada: una consulta alternativa.
        response_api = None
        try:
            response_api = context.request.get(
                "https://www.instagram.com/api/v1/users/web_profile_info/?" + urlencode({"username": username}),
                headers={"x-ig-app-id": "936619743392459", "Referer": f"https://www.instagram.com/{username}/"},
                timeout=30_000,
            )
            validar_respuesta_instagram(response_api, response_api, f"consultar el perfil de @{username}")
            data = response_api.json()
            validar_datos_instagram(data)
            estados.extend(_extraer_estado_perfil(data, username))
        except (ErrorConsultaInstagram, PerfilNoEncontrado, ValueError):
            pass
        finally:
            if response_api is not None:
                response_api.dispose()
        if errores_sesion:
            raise errores_sesion[0]
        return _resumir_estado_perfil(estados, username, user_id)
    finally:
        page.close()


@con_sesiones(verificar_todas=True)
def comprobar_perfil_accesible(username: str, *, sin_historias: bool = False) -> dict[str, Any]:
    """
    Verifica realmente el acceso al perfil con la sesión Web autenticada.

    - Público: se permite.
    - Privado: sólo se permite si Instagram confirma following=True.
    - Privacidad desconocida: seguir al perfil confirma acceso sin inventar su privacidad.
    - La comprobación alternativa usa la página y sus datos autenticados, no Stories.
    - sin_historias desactiva los campos de reels en la consulta de permisos;
      solicita como máximo una publicación, sin descargar su archivo.
    """
    username = limpiar_username(username)
    comprobar_sesion_local()
    # Resuelve el ID con el mecanismo existente. Si ya está en caché,
    # esto no hace una consulta adicional.
    user_id = resolver_user_id(username)

    with _BLOQUEO:
        with usar_playwright_sincronico(sync_playwright) as p:
            browser, context = _crear_contexto(p)

            try:
                page = context.new_page()
                response = page.goto(
                    _construir_profile_info_url(username, sin_historias=sin_historias),
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )

                validar_respuesta_instagram(page, response, f"verificar @{username}")

                try:
                    data = response.json()
                except Exception:
                    try:
                        data = json.loads(page.locator("body").inner_text())
                    except Exception as error:
                        raise ErrorConsultaInstagram(
                            "Instagram no devolvió JSON válido al verificar "
                            f"@{username}."
                        ) from error

                validar_datos_instagram(data)

                if not isinstance(data, dict):
                    raise ErrorConsultaInstagram(
                        f"Instagram devolvió una respuesta inválida para @{username}."
                    )

                if data.get("errors"):
                    raise ErrorConsultaInstagram(
                        f"Instagram devolvió un error GraphQL para @{username}."
                    )

                estados = _extraer_estado_perfil(data, username)

                es_privado, siguiendo = _resumir_estado_perfil(estados, username, user_id)
                if (es_privado is None and siguiendo is not True) or (es_privado and siguiendo is None):
                    es_privado, siguiendo = _comprobar_perfil_desde_pagina(context, username, user_id, estados)

                if es_privado is None and siguiendo is not True:
                    raise ErrorConsultaInstagram(
                        f"Instagram no permitió confirmar el acceso a @{username}: "
                        "faltan datos de privacidad y no se confirmó que la sesión lo siga."
                    )

                if es_privado and siguiendo is None:
                    raise ErrorConsultaInstagram(
                        f"El perfil @{username} es privado, pero Instagram no permitió confirmar si esta sesión lo sigue."
                    )
                if es_privado and siguiendo is not True:
                    raise PerfilPrivado(
                        f"La sesión autenticada no tiene acceso a @{username}."
                    )

                cache = _cargar_cache()
                cache[username] = int(user_id)
                _guardar_cache(cache)

                _guardar_estado_contexto(context)

                return {
                    "username": username,
                    "user_id": int(user_id),
                    "is_private": es_privado,
                    "following": siguiendo,
                    "acceso_confirmado": True,
                }

            finally:
                context.close()
                browser.close()


# Alias de compatibilidad con versiones antiguas.
def comprobar_perfil_publico(username: str) -> dict[str, Any]:
    return comprobar_perfil_accesible(username)


def _buscar_reels_media(obj: Any):
    if isinstance(obj, dict):
        reels = obj.get("reels_media")

        if isinstance(reels, list):
            return reels

        for valor in obj.values():
            encontrado = _buscar_reels_media(valor)
            if encontrado is not None:
                return encontrado

    elif isinstance(obj, list):
        for valor in obj:
            encontrado = _buscar_reels_media(valor)
            if encontrado is not None:
                return encontrado

    return None


def _video_url(item: dict[str, Any]) -> str | None:
    recursos = item.get("video_resources") or []

    if isinstance(recursos, list):
        for recurso in reversed(recursos):
            if isinstance(recurso, dict) and recurso.get("src"):
                return str(recurso["src"])

    versiones = item.get("video_versions") or []

    if isinstance(versiones, list):
        for recurso in versiones:
            if isinstance(recurso, dict) and recurso.get("url"):
                return str(recurso["url"])

    valor = item.get("video_url")
    return str(valor) if valor else None


def _image_url(item: dict[str, Any]) -> str | None:
    for key in ("display_url", "thumbnail_src"):
        valor = item.get(key)
        if valor:
            return str(valor)

    imagenes = item.get("image_versions2")

    if isinstance(imagenes, dict):
        candidatos = imagenes.get("candidates") or []

        if isinstance(candidatos, list):
            for candidato in candidatos:
                if isinstance(candidato, dict) and candidato.get("url"):
                    return str(candidato["url"])

    return None


def _taken_at(item: dict[str, Any]) -> datetime | None:
    for key in (
        "taken_at_timestamp",
        "taken_at",
        "taken_at_ts",
    ):
        valor = item.get(key)

        if valor is None:
            continue

        try:
            numero = float(valor)

            # algunos IDs/fechas pueden venir en milisegundos
            if numero > 10_000_000_000:
                numero /= 1000.0

            return datetime.fromtimestamp(
                numero,
                tz=timezone.utc,
            )
        except (TypeError, ValueError, OSError):
            continue

    return None


def _dimensiones_imagen(contenido: bytes) -> tuple[int, int]:
    try:
        with Image.open(BytesIO(contenido)) as imagen:
            return int(imagen.width), int(imagen.height)
    except (UnidentifiedImageError, OSError, ValueError):
        return 0, 0


def _extension_desde_content_type(
    content_type: str,
    es_video: bool,
) -> str:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()

    if es_video:
        if ctype == "video/quicktime":
            return "mov"
        return "mp4"

    mapa = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }

    if ctype in mapa:
        return mapa[ctype]

    extension = mimetypes.guess_extension(ctype) or ".jpg"
    return extension.lstrip(".").replace("jpe", "jpg")


def _construir_url_stories(user_id: int) -> str:
    variables = {
        "reel_ids": [int(user_id)],
        "highlight_reel_ids": [],
        "precomposed_overlay": False,
    }

    return (
        "https://www.instagram.com/graphql/query/?"
        + urlencode(
            {
                "query_hash": STORIES_QUERY_HASH,
                "variables": json.dumps(
                    variables,
                    separators=(",", ":"),
                ),
            }
        )
    )


@con_sesiones()
def descargar_historias(
    username: str,
    user_id: int | None = None,
) -> list[HistoriaDescargada]:
    """
    Consulta las Stories mediante Instagram Web GraphQL y descarga
    imagen/video directamente desde CDN.

    No abre el visor normal de Stories y no realiza ninguna llamada
    deliberada para marcarlas como vistas.
    """
    username = limpiar_username(username)

    with _BLOQUEO:
        comprobar_sesion_local()

        if user_id is None:
            user_id = resolver_user_id(username)
        else:
            user_id = int(user_id)

            cache = _cargar_cache()
            if cache.get(username) != user_id:
                cache[username] = user_id
                _guardar_cache(cache)

        url_graphql = _construir_url_stories(user_id)

        with usar_playwright_sincronico(sync_playwright) as p:
            browser, context = _crear_contexto(p)

            try:
                page = context.new_page()

                response = page.goto(
                    url_graphql,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )

                validar_respuesta_instagram(page, response, f"consultar Stories de @{username}")

                try:
                    data = response.json()
                except Exception:
                    texto = page.locator("body").inner_text()
                    try:
                        data = json.loads(texto)
                    except Exception as error:
                        raise ErrorConsultaInstagram(
                            "Instagram no devolvió JSON válido en Stories."
                        ) from error

                validar_datos_instagram(data)
                reels = _buscar_reels_media(data)

                if reels is None:
                    raise ErrorConsultaInstagram(
                        "La respuesta de Instagram no contiene reels_media. "
                        "Es posible que Instagram haya cambiado el endpoint/query_hash."
                    )

                if not reels:
                    # Un privado inaccesible también puede responder con reels vacíos.
                    comprobar_perfil_accesible(username)
                    raise SinHistoriasDisponibles(
                        f"@{username} no tiene historias visibles actualmente."
                    )

                items: list[dict[str, Any]] = []

                for reel in reels:
                    if not isinstance(reel, dict):
                        continue

                    reel_items = reel.get("items") or []

                    if isinstance(reel_items, list):
                        items.extend(
                            item
                            for item in reel_items
                            if isinstance(item, dict)
                        )

                if not items:
                    comprobar_perfil_accesible(username)
                    raise SinHistoriasDisponibles(
                        f"@{username} no tiene historias visibles actualmente."
                    )

                items.sort(
                    key=lambda item: (
                        _taken_at(item).timestamp()
                        if _taken_at(item) is not None
                        else 0
                    )
                )

                resultado: list[HistoriaDescargada] = []

                for item in items:
                    story_pk = str(
                        item.get("id")
                        or item.get("pk")
                        or ""
                    )

                    if not story_pk:
                        continue

                    video = _video_url(item)
                    imagen = _image_url(item)

                    es_video = bool(video)
                    media_url = video or imagen

                    if not media_url:
                        continue

                    respuesta_media = context.request.get(
                        media_url,
                        headers={
                            "Referer": "https://www.instagram.com/",
                            "Accept": "*/*",
                        },
                        timeout=60_000,
                        fail_on_status_code=False,
                    )

                    try:
                        if not respuesta_media.ok:
                            raise ErrorConsultaInstagram(
                                f"CDN respondió HTTP {respuesta_media.status} "
                                f"para Story {story_pk}."
                            )

                        contenido = respuesta_media.body()

                        content_type = (
                            respuesta_media.headers.get(
                                "content-type",
                                "",
                            )
                            or (
                                "video/mp4"
                                if es_video
                                else "image/jpeg"
                            )
                        ).split(";", 1)[0]

                    finally:
                        respuesta_media.dispose()

                    extension = _extension_desde_content_type(
                        content_type,
                        es_video,
                    )

                    if es_video:
                        ancho = 0
                        alto = 0
                    else:
                        ancho, alto = _dimensiones_imagen(contenido)

                    resultado.append(
                        HistoriaDescargada(
                            story_pk=story_pk,
                            username=username,
                            contenido=contenido,
                            content_type=content_type,
                            extension=extension,
                            ancho=ancho,
                            alto=alto,
                            tomada_en=_taken_at(item),
                            hash_archivo=f"igpk:{story_pk}",
                            es_video=es_video,
                        )
                    )

                if not resultado:
                    raise SinHistoriasDisponibles(
                        f"@{username} no tiene historias descargables actualmente."
                    )

                # La operación terminó bien: persistimos el estado web
                # más reciente para futuras revisiones.
                _guardar_estado_contexto(
                    context
                )

                return resultado

            finally:
                context.close()
                browser.close()


def descargar_imagenes(
    username: str,
    user_id: int | None = None,
) -> list[HistoriaDescargada]:
    return descargar_historias(username, user_id=user_id)


def hash_bytes(contenido: bytes) -> str:
    return hashlib.sha256(contenido).hexdigest()

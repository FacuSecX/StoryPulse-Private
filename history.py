# Bot Telegram StoryPulse v1.0
# https://github.com/FacuSecX/

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

_state_env = os.getenv(
    "INSTAGRAM_STORAGE_STATE",
    "instagram_state.json",
).strip()

RUTA_SESION = Path(_state_env).expanduser()
if not RUTA_SESION.is_absolute():
    RUTA_SESION = BASE_DIR / RUTA_SESION

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


class SinHistoriasDisponibles(RuntimeError):
    pass


class PerfilPrivado(RuntimeError):
    pass


class PerfilNoEncontrado(RuntimeError):
    pass


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


def _leer_estado() -> dict[str, Any]:
    if not RUTA_SESION.exists():
        raise FileNotFoundError(
            f"No existe {RUTA_SESION}. "
            "Primero crea instagram_state.json en Windows y cópialo al VPS."
        )

    try:
        data = json.loads(RUTA_SESION.read_text(encoding="utf-8"))
    except Exception as error:
        raise RuntimeError(
            "instagram_state.json no contiene JSON válido."
        ) from error

    if not isinstance(data, dict):
        raise RuntimeError("instagram_state.json tiene un formato inválido.")

    return data


def comprobar_sesion_local() -> dict[str, Any]:
    """
    Comprueba localmente que storage_state contiene sessionid.
    NO hace ninguna consulta a Instagram.
    """
    data = _leer_estado()
    cookies = data.get("cookies") or []

    sessionid_presente = False
    dominios = set()

    if isinstance(cookies, list):
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue

            dominio = str(cookie.get("domain", ""))
            if dominio:
                dominios.add(dominio)

            if cookie.get("name") == "sessionid" and cookie.get("value"):
                sessionid_presente = True

    if not sessionid_presente:
        raise RuntimeError(
            "instagram_state.json existe pero no contiene una cookie sessionid."
        )

    return {
        "session_file": str(RUTA_SESION.resolve()),
        "sessionid_presente": True,
        "dominios": sorted(dominios),
    }


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
        storage_state=str(RUTA_SESION),
        viewport={"width": 1365, "height": 900},
        locale="es-AR",
        timezone_id=ZONA_LOCAL,
    )

    return browser, context


def _guardar_estado_contexto(context) -> None:
    """
    Conserva cookies/local storage actualizados después de una
    operación correcta.

    Se escribe primero en un temporal y luego se reemplaza el JSON
    para reducir el riesgo de dejarlo incompleto si el proceso muere.
    """
    temporal = RUTA_SESION.with_name(
        RUTA_SESION.name + ".tmp"
    )

    try:
        try:
            context.storage_state(
                path=str(temporal),
                indexed_db=True,
            )
        except TypeError:
            # Compatibilidad con versiones de Playwright que no
            # acepten todavía indexed_db como argumento.
            context.storage_state(
                path=str(temporal)
            )

        os.replace(
            temporal,
            RUTA_SESION,
        )

        try:
            RUTA_SESION.chmod(0o600)
        except OSError:
            pass

    finally:
        try:
            temporal.unlink(
                missing_ok=True
            )
        except OSError:
            pass


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

        with sync_playwright() as p:
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

                if response is None:
                    raise RuntimeError(
                        f"Instagram no devolvió respuesta para @{username}."
                    )

                if response.status == 404:
                    raise PerfilNoEncontrado(
                        f"No se encontró @{username}."
                    )

                if response.status >= 400:
                    raise RuntimeError(
                        f"Instagram respondió HTTP {response.status} "
                        f"al abrir @{username}."
                    )

                page.wait_for_timeout(7_000)

                if "/accounts/login" in page.url.lower():
                    raise RuntimeError(
                        "Instagram redirigió al login. "
                        "La sesión web ya no es válida."
                    )

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
            raise RuntimeError(
                f"Instagram devolvió más de un ID candidato para @{username}: "
                + ", ".join(map(str, candidatos))
            )

        user_id = int(candidatos[0])
        cache[username] = user_id
        _guardar_cache(cache)

        return user_id


def _construir_profile_info_url(username: str) -> str:
    """
    Consulta de perfil autenticada que pide explícitamente la información
    de relación entre la sesión y el perfil objetivo.
    """
    variables = {
        "data": {
            "count": 1,
            "include_relationship_info": True,
            "latest_besties_reel_media": True,
            "latest_reel_media": True,
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

    def recorrer(valor: Any, padre: dict[str, Any] | None = None) -> None:
        if isinstance(valor, dict):
            agregar_desde_dict(valor, padre)

            usuario_hijo = valor.get("user")
            if isinstance(usuario_hijo, dict):
                agregar_desde_dict(usuario_hijo, valor)

            for hijo in valor.values():
                recorrer(hijo, valor)

        elif isinstance(valor, list):
            for hijo in valor:
                recorrer(hijo, padre)

    recorrer(obj)
    return encontrados


def comprobar_perfil_accesible(username: str) -> dict[str, Any]:
    """
    Verifica realmente el acceso al perfil con la sesión Web autenticada.

    - Público: se permite.
    - Privado: sólo se permite si Instagram confirma following=True.
    - Si Instagram no permite determinar la privacidad de forma fiable,
      se aborta para no agregar un perfil inaccesible por error.
    """
    username = limpiar_username(username)
    comprobar_sesion_local()
    # Resuelve el ID con el mecanismo existente. Si ya está en caché,
    # esto no hace una consulta adicional.
    user_id = resolver_user_id(username)

    with _BLOQUEO:
        with sync_playwright() as p:
            browser, context = _crear_contexto(p)

            try:
                page = context.new_page()
                response = page.goto(
                    _construir_profile_info_url(username),
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )

                if response is None:
                    raise RuntimeError(
                        f"Instagram no devolvió respuesta al verificar @{username}."
                    )

                if response.status == 404:
                    raise PerfilNoEncontrado(
                        f"No se encontró @{username}."
                    )

                if response.status in (401, 403):
                    raise RuntimeError(
                        "Instagram rechazó la sesión al verificar el perfil "
                        f"(HTTP {response.status})."
                    )

                if response.status == 429:
                    raise RuntimeError(
                        "Instagram respondió 429 / rate limit al verificar el perfil."
                    )

                if response.status >= 400:
                    raise RuntimeError(
                        f"Instagram respondió HTTP {response.status} "
                        f"al verificar @{username}."
                    )

                try:
                    data = response.json()
                except Exception:
                    try:
                        data = json.loads(page.locator("body").inner_text())
                    except Exception as error:
                        raise RuntimeError(
                            "Instagram no devolvió JSON válido al verificar "
                            f"@{username}."
                        ) from error

                if not isinstance(data, dict):
                    raise RuntimeError(
                        f"Instagram devolvió una respuesta inválida para @{username}."
                    )

                if data.get("errors"):
                    raise RuntimeError(
                        f"Instagram devolvió un error GraphQL para @{username}."
                    )

                estados = _extraer_estado_perfil(data, username)

                privados = [
                    estado["is_private"]
                    for estado in estados
                    if estado.get("is_private") is not None
                ]
                seguimientos = [
                    estado["following"]
                    for estado in estados
                    if estado.get("following") is not None
                ]
                ids = [
                    int(estado["user_id"])
                    for estado in estados
                    if estado.get("user_id")
                ]

                if True in privados:
                    es_privado: bool | None = True
                elif False in privados:
                    es_privado = False
                else:
                    es_privado = None

                if True in seguimientos:
                    siguiendo: bool | None = True
                elif False in seguimientos:
                    siguiendo = False
                else:
                    siguiendo = None

                # Fallback visual conservador por si Instagram cambia la forma
                # del JSON pero mantiene el aviso visible de perfil privado.
                if es_privado is None:
                    try:
                        page_perfil = context.new_page()
                        resp_perfil = page_perfil.goto(
                            f"https://www.instagram.com/{username}/",
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                        if resp_perfil is not None and resp_perfil.status == 404:
                            raise PerfilNoEncontrado(
                                f"No se encontró @{username}."
                            )
                        page_perfil.wait_for_timeout(2_000)
                        texto_pagina = page_perfil.locator("body").inner_text().casefold()
                        if (
                            "esta cuenta es privada" in texto_pagina
                            or "this account is private" in texto_pagina
                        ):
                            es_privado = True
                            if siguiendo is None:
                                siguiendo = False
                    except PerfilNoEncontrado:
                        raise
                    except Exception:
                        pass

                if es_privado is None:
                    raise RuntimeError(
                        "No pude confirmar si el perfil es público o privado. "
                        "Por seguridad no fue agregado."
                    )

                # Si el JSON trajo el ID, sólo lo usamos para comprobar que
                # corresponde al mismo perfil resuelto por el motor existente.
                if ids and int(user_id) not in ids:
                    raise RuntimeError(
                        f"Instagram devolvió un ID inconsistente para @{username}."
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
                    "is_private": bool(es_privado),
                    "following": siguiendo,
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

        with sync_playwright() as p:
            browser, context = _crear_contexto(p)

            try:
                page = context.new_page()

                response = page.goto(
                    url_graphql,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )

                if response is None:
                    raise RuntimeError(
                        f"Instagram no respondió al consultar Stories de @{username}."
                    )

                if response.status in (401, 403):
                    raise RuntimeError(
                        "Instagram rechazó la sesión web. "
                        "Puede ser necesario exportar una sesión nueva."
                    )

                if response.status >= 400:
                    raise RuntimeError(
                        f"Instagram respondió HTTP {response.status} "
                        f"al consultar Stories de @{username}."
                    )

                if "/accounts/login" in page.url.lower():
                    raise RuntimeError(
                        "Instagram redirigió al login. "
                        "La sesión web ya no es válida."
                    )

                try:
                    data = response.json()
                except Exception:
                    texto = page.locator("body").inner_text()
                    try:
                        data = json.loads(texto)
                    except Exception as error:
                        raise RuntimeError(
                            "Instagram no devolvió JSON válido en Stories."
                        ) from error

                reels = _buscar_reels_media(data)

                if reels is None:
                    raise RuntimeError(
                        "La respuesta de Instagram no contiene reels_media. "
                        "Es posible que Instagram haya cambiado el endpoint/query_hash."
                    )

                if not reels:
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
                            raise RuntimeError(
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
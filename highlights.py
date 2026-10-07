# StoryPulse v2.0
# Created by FacuSecX https://github.com/FacuSecX/StoryPulse-Private

from __future__ import annotations

import json
import html as html_lib
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

import database as db
from history import (
    STORIES_QUERY_HASH,
    _buscar_reels_media,
    _crear_contexto,
    _extension_desde_content_type,
    _guardar_estado_contexto,
    _image_url,
    _video_url,
    comprobar_sesion_local,
    comprobar_perfil_accesible,
    limpiar_username,
)
from instagram_sessions import (
    ErrorConsultaInstagram,
    ErrorSesionInstagram,
    PerfilNoEncontrado,
    PerfilPrivado,
    SinHistoriasDisponibles,
    con_sesiones,
    usar_playwright_sincronico,
    validar_datos_instagram,
    validar_pagina_instagram,
    validar_respuesta_instagram,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

HISTORYS_DIR = Path(os.getenv("HISTORYS_DIR", "/historys")).expanduser()
HIGHLIGHTS_DIR_NAME = "Higlights"
try:
    HIGHLIGHTS_TIMEZONE = ZoneInfo(
        os.getenv("STORYPULSE_TIMEZONE", "America/Argentina/Buenos_Aires")
    )
except Exception:
    HIGHLIGHTS_TIMEZONE = timezone.utc


class SinHistoriasDestacadas(SinHistoriasDisponibles):
    """El perfil es accesible pero no tiene carruseles destacados."""


@dataclass(frozen=True)
class DestacadaDescargada:
    story_pk: str
    grupo: str
    ruta: Path
    nueva: bool


@dataclass(frozen=True)
class ResultadoDestacadas:
    username: str
    carpeta: Path
    grupos: tuple[str, ...]
    archivos_nuevos: int
    archivos_ya_guardados: int
    archivos: tuple[DestacadaDescargada, ...]


def limpiar_nombre_destacada(valor: Any) -> str:
    """Devuelve un nombre de carpeta ASCII, estable y apto para Linux."""
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    texto = texto.casefold()
    texto = re.sub(r"[^a-z0-9]+", "_", texto)
    texto = texto.strip("._-")
    return texto or "destacada"


def nombres_destacadas_unicos(titulos: list[Any]) -> list[str]:
    """Sanitiza títulos y agrega 2, 3... cuando el nombre se repite."""
    usados: dict[str, int] = {}
    resultado: list[str] = []
    for titulo in titulos:
        base = limpiar_nombre_destacada(titulo)
        numero = usados.get(base, 0) + 1
        usados[base] = numero
        resultado.append(base if numero == 1 else f"{base}{numero}")
    return resultado


_ETIQUETAS_GENERICAS = {
    "ver historia destacada",
    "ver historias destacadas",
    "view story highlight",
    "view highlight",
    "story highlight",
    "highlight",
}


def _texto_titulo_destacada(valor: Any) -> str:
    texto = re.sub(r"\s+", " ", str(valor or "")).strip()
    texto = re.sub(
        r"^(?:ver historias? destacadas?|view (?:story )?highlight)\s*[:\-–—]?\s*",
        "",
        texto,
        flags=re.IGNORECASE,
    )
    texto = re.sub(
        r"\s*\((?:ver historias? destacadas?|view (?:story )?highlight)\)$",
        "",
        texto,
        flags=re.IGNORECASE,
    ).strip()
    if texto.casefold() in _ETIQUETAS_GENERICAS:
        return ""
    return texto


_HIGHLIGHT_ID_RE = re.compile(r"(?:highlight|reel)\s*[:/_-]\s*(\d{3,})", re.IGNORECASE)
_HIGHLIGHT_URL_RE = re.compile(r"/(?:stories/)?highlights/(\d{3,})(?:[/?#]|$)", re.IGNORECASE)


def _normalizar_id_destacada(valor: Any) -> str | None:
    """Convierte las variantes de ID que entrega Instagram a un ID numérico.

    Según la respuesta utilizada por Instagram el mismo carrusel puede llegar
    como ``123``, ``highlight:123`` o dentro de un enlace
    ``/stories/highlights/123/``. El endpoint de reels necesita únicamente la
    parte numérica.
    """
    if isinstance(valor, bool) or valor is None:
        return None
    texto = str(valor).strip()
    if re.fullmatch(r"\d{3,}", texto):
        return texto
    encontrado = _HIGHLIGHT_URL_RE.search(texto)
    if encontrado:
        return encontrado.group(1)
    encontrado = _HIGHLIGHT_ID_RE.search(texto)
    return encontrado.group(1) if encontrado else None


def _es_id_destacada(valor: Any) -> bool:
    return _normalizar_id_destacada(valor) is not None


def _desenvolver_nodos(valor: Any) -> list[dict[str, Any]]:
    """Acepta edges, nodes o una lista directa de objetos."""
    if isinstance(valor, dict):
        if isinstance(valor.get("edges"), list):
            return _desenvolver_nodos(valor["edges"])
        if isinstance(valor.get("node"), dict):
            return [valor["node"]]
        return [valor]
    if isinstance(valor, list):
        resultado: list[dict[str, Any]] = []
        for fila in valor:
            if isinstance(fila, dict) and isinstance(fila.get("node"), dict):
                resultado.append(fila["node"])
            elif isinstance(fila, dict):
                resultado.append(fila)
        return resultado
    return []


def _campo_id(grupo: dict[str, Any]) -> str | None:
    for clave in ("id", "pk", "reel_id", "highlight_id", "highlight_reel_id"):
        valor = grupo.get(clave)
        normalizado = _normalizar_id_destacada(valor)
        if normalizado:
            return normalizado
    for clave in ("url", "href", "link", "permalink", "web_link"):
        normalizado = _normalizar_id_destacada(grupo.get(clave))
        if normalizado:
            return normalizado
    # Algunas respuestas actuales envuelven el carrusel en ``reel`` o
    # ``highlight`` en vez de dejar el ID en el nodo principal.
    for clave in ("reel", "highlight", "highlight_reel", "node"):
        anidado = grupo.get(clave)
        if isinstance(anidado, dict):
            normalizado = _campo_id(anidado)
            if normalizado:
                return normalizado
    return None


def _campo_titulo(grupo: dict[str, Any]) -> str:
    for clave in ("title", "name", "reel_title", "label"):
        titulo = _texto_titulo_destacada(grupo.get(clave))
        if titulo:
            return titulo
    for clave in ("reel", "highlight", "highlight_reel", "node"):
        anidado = grupo.get(clave)
        if isinstance(anidado, dict):
            titulo = _campo_titulo(anidado)
            if titulo != "Destacada":
                return titulo
    return "Destacada"


_CLAVES_DESTACADAS = {
    "edge_highlight_reels",
    "highlight_reels",
    "highlights",
    "highlight_reel",
    "edge_user_to_highlights",
    "user_highlights",
    "story_highlights",
    "highlights_tray",
    "story_tray",
}


def _es_clave_destacadas(clave: Any) -> bool:
    normalizada = re.sub(r"[^a-z]", "", str(clave).casefold())
    if normalizada in {re.sub(r"[^a-z]", "", item) for item in _CLAVES_DESTACADAS}:
        return True
    # Instagram agrega sufijos como ``edge_user_to_highlights_connection``.
    # ``highlight_reel_ids`` también es una fuente válida aunque sólo traiga
    # IDs escalares.
    return "highlight" in normalizada and normalizada not in {"highlightcover"}


def _agregar_grupo(encontrados: list[dict[str, str]], vistos: set[str],
                   grupo: dict[str, Any]) -> None:
    identificador = _campo_id(grupo)
    if identificador and identificador not in vistos:
        vistos.add(identificador)
        encontrados.append({"id": identificador, "title": _campo_titulo(grupo)})


def _grupos_en_enlace(valor: Any, titulo: Any = None) -> list[dict[str, str]]:
    if not isinstance(valor, str):
        return []
    identificador = _normalizar_id_destacada(valor)
    if not identificador:
        return []
    titulo_limpio = _texto_titulo_destacada(titulo)
    return [{"id": identificador, "title": titulo_limpio or "Destacada"}]


def extraer_grupos_destacadas(obj: Any) -> list[dict[str, str]]:
    """Extrae IDs/títulos de las formas GraphQL conocidas de Instagram.

    La respuesta del perfil cambió varias veces (edges, nodes y listas
    directas). Se busca únicamente bajo claves de highlights y se deduplican
    IDs para evitar descargar dos veces el mismo carrusel.
    """
    encontrados: list[dict[str, str]] = []
    vistos: set[str] = set()

    def recorrer(valor: Any) -> None:
        if isinstance(valor, dict):
            for clave, hijo in valor.items():
                if _es_clave_destacadas(clave):
                    for grupo in _desenvolver_nodos(hijo):
                        _agregar_grupo(encontrados, vistos, grupo)
                        # ``highlight_reel_ids`` puede ser una lista de
                        # strings/enteros en vez de una lista de objetos.
                        if isinstance(grupo, str):
                            for item in _grupos_en_enlace(grupo):
                                _agregar_grupo(encontrados, vistos, item)
                        # Algunas respuestas anidan items o una lista de
                        # carruseles dentro del mismo nodo.
                        recorrer(grupo)
                    if isinstance(hijo, list):
                        for item in hijo:
                            identificador = _normalizar_id_destacada(item)
                            if identificador:
                                _agregar_grupo(encontrados, vistos, {
                                    "id": identificador,
                                    "title": "Destacada",
                                })
                # También se reconocen enlaces de highlights embebidos en
                # nodos que no llevan una clave de highlight explícita.
                if str(clave).casefold() in {"url", "href", "link", "permalink", "web_link"}:
                    for grupo in _grupos_en_enlace(hijo, valor.get("title") or valor.get("name")):
                        _agregar_grupo(encontrados, vistos, grupo)
                recorrer(hijo)
        elif isinstance(valor, list):
            for hijo in valor:
                recorrer(hijo)

    recorrer(obj)
    return encontrados


def extraer_grupos_destacadas_de_html(documento: str) -> list[dict[str, str]]:
    """Obtiene carruseles desde enlaces presentes en el HTML del perfil.

    Es un respaldo para las versiones donde Instagram dibuja los círculos en
    HTML pero no incluye ``edge_highlight_reels`` en el JSON inicial.
    """
    if not isinstance(documento, str) or not documento:
        return []
    texto = html_lib.unescape(documento).replace("\\/", "/")
    encontrados: list[dict[str, str]] = []
    vistos: set[str] = set()
    patron_ancla = re.compile(
        r"<a\b(?P<attrs>[^>]*?(?:href|data-href)\s*=\s*[\"'](?P<href>[^\"']+)[\"'][^>]*)>"
        r"(?P<body>.*?)</a>", re.IGNORECASE | re.DOTALL,
    )
    for coincidencia in patron_ancla.finditer(texto):
        identificador = _normalizar_id_destacada(coincidencia.group("href"))
        if not identificador or identificador in vistos:
            continue
        cuerpo = re.sub(r"<[^>]+>", " ", coincidencia.group("body"))
        titulo = _texto_titulo_destacada(html_lib.unescape(cuerpo))
        attrs = coincidencia.group("attrs")
        etiqueta = re.search(r"(?:aria-label|title)\s*=\s*[\"']([^\"']+)", attrs, re.IGNORECASE)
        titulo_atributo = _texto_titulo_destacada(etiqueta.group(1) if etiqueta else "")
        # ``aria-label=Ver historia destacada`` es una instrucción genérica,
        # no el nombre del carrusel. Preferimos el texto visible y sólo
        # usamos el atributo si contiene un título real.
        titulo = titulo or titulo_atributo or "Destacada"
        vistos.add(identificador)
        encontrados.append({"id": identificador, "title": titulo})
    # JSON serializado o HTML minificado puede no contener una etiqueta <a>.
    for coincidencia in _HIGHLIGHT_URL_RE.finditer(texto):
        identificador = coincidencia.group(1)
        if identificador not in vistos:
            vistos.add(identificador)
            encontrados.append({"id": identificador, "title": "Destacada"})
    return encontrados


def extraer_grupos_destacadas_de_dom(valor: Any) -> list[dict[str, str]]:
    """Normaliza los enlaces de destacadas extraídos con ``page.evaluate``."""
    if not isinstance(valor, list):
        return []
    encontrados: list[dict[str, str]] = []
    vistos: set[str] = set()
    for fila in valor:
        if not isinstance(fila, dict):
            continue
        identificador = _normalizar_id_destacada(
            fila.get("href") or fila.get("url") or ""
        )
        if not identificador or identificador in vistos:
            continue
        titulo = _texto_titulo_destacada(
            fila.get("text") or fila.get("title") or fila.get("aria") or ""
        ) or "Destacada"
        vistos.add(identificador)
        encontrados.append({"id": identificador, "title": titulo})
    return encontrados


def _fecha_destacada(item: dict[str, Any]) -> str:
    """Devuelve la fecha local de publicación de una Story destacada."""
    for clave in (
        "taken_at_timestamp",
        "taken_at",
        "device_timestamp",
        "created_at",
        "timestamp",
    ):
        valor = item.get(clave)
        if valor is None:
            continue
        try:
            if isinstance(valor, str) and not valor.strip().isdigit():
                texto = valor.strip().replace("Z", "+00:00")
                fecha = datetime.fromisoformat(texto)
                if fecha.tzinfo is None:
                    fecha = fecha.replace(tzinfo=timezone.utc)
                return fecha.astimezone(HIGHLIGHTS_TIMEZONE).strftime("%Y-%m-%d")
            segundos = float(valor)
            if segundos > 100_000_000_000:
                segundos /= 1000
            fecha = datetime.fromtimestamp(segundos, tz=timezone.utc)
            return fecha.astimezone(HIGHLIGHTS_TIMEZONE).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    # Algunas respuestas de Instagram omiten la fecha. El nombre sigue
    # manteniendo el formato solicitado y no usa el ID interno.
    return datetime.now(HIGHLIGHTS_TIMEZONE).strftime("%Y-%m-%d")


def _nombre_archivo_destacada(
    carpeta: Path,
    username: str,
    fecha: str,
    extension: str,
    reservados: set[Path],
) -> Path:
    """Genera ``usuario_fecha.ext`` y ``usuario_fecha_2.ext`` sin pisar nada."""
    base = f"{username}_{fecha}"
    intento = 1
    while True:
        sufijo = "" if intento == 1 else f"_{intento}"
        ruta = carpeta / f"{base}{sufijo}.{extension}"
        if not ruta.exists() and ruta not in reservados:
            reservados.add(ruta)
            return ruta
        intento += 1


class _JsonScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._en_script = False
        self._partes: list[str] = []
        self.documentos: list[Any] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "script":
            return
        tipo = dict(attrs).get("type", "").lower()
        # Instagram a veces omite el atributo type en los bloques JSON que
        # contienen el estado inicial de la página.
        self._en_script = tipo in {"application/ld+json", "application/json", ""}
        self._partes = []

    def handle_data(self, data: str) -> None:
        if self._en_script:
            self._partes.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or not self._en_script:
            return
        self._en_script = False
        contenido = "".join(self._partes).strip()
        if not contenido.startswith(("{", "[")):
            return
        try:
            self.documentos.append(json.loads(contenido))
        except (TypeError, ValueError, json.JSONDecodeError):
            pass


def _json_en_html(html: str) -> list[Any]:
    parser = _JsonScriptParser()
    try:
        parser.feed(html)
    except Exception:
        return []
    return parser.documentos


def _documento_confirma_acceso(obj: Any, username: str) -> bool:
    """Reconoce la privacidad básica ya presente en la respuesta del perfil."""
    if isinstance(obj, dict):
        nombre = obj.get("username")
        if isinstance(nombre, str) and nombre.casefold().lstrip("@") == username.casefold():
            privado = obj.get("is_private")
            if privado is False:
                return True
            estado = obj.get("friendship_status")
            if isinstance(estado, dict) and estado.get("following") is True:
                return True
            if obj.get("following") is True:
                return True
        return any(_documento_confirma_acceso(valor, username) for valor in obj.values())
    if isinstance(obj, list):
        return any(_documento_confirma_acceso(valor, username) for valor in obj)
    return False


def _construir_url_destacada(highlight_id: str) -> str:
    variables = {
        "reel_ids": [],
        "highlight_reel_ids": [int(highlight_id)],
        "precomposed_overlay": False,
    }
    return "https://www.instagram.com/graphql/query/?" + urlencode({
        "query_hash": STORIES_QUERY_HASH,
        "variables": json.dumps(variables, separators=(",", ":")),
    })


def _contenido_y_tipo(respuesta) -> tuple[bytes, str]:
    if not respuesta.ok:
        raise ErrorConsultaInstagram(
            f"CDN respondió HTTP {respuesta.status} al descargar una destacada."
        )
    contenido = respuesta.body()
    tipo = (respuesta.headers.get("content-type", "") or "image/jpeg").split(";", 1)[0]
    return contenido, tipo


def _migrar_archivo_destacada(
    ruta_anterior: Path,
    carpeta: Path,
    username: str,
    fecha: str,
    extension: str,
    reservados: set[Path],
) -> Path | None:
    """Mueve archivos de ``ver_historia_destacada`` al formato nuevo."""
    if not ruta_anterior.is_file():
        return None
    if ruta_anterior.parent == carpeta and ruta_anterior.name.startswith(f"{username}_"):
        return ruta_anterior
    extension_real = ruta_anterior.suffix.lstrip(".").lower() or extension
    destino = _nombre_archivo_destacada(
        carpeta, username, fecha, extension_real, reservados
    )
    if ruta_anterior.resolve() == destino.resolve():
        return destino
    carpeta.mkdir(parents=True, exist_ok=True)
    shutil.move(str(ruta_anterior), str(destino))
    return destino


def _descargar_grupo(context, username: str, grupo: dict[str, str], carpeta: Path,
                    nombre_grupo: str, chat_id: int | None,
                    progress_callback: Callable[[dict[str, Any]], None] | None = None
                    ) -> list[DestacadaDescargada]:
    def reportar(datos: dict[str, Any]) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(dict(datos))
        except Exception:
            # El progreso es informativo y nunca debe interrumpir la descarga.
            pass

    page = context.new_page()
    try:
        response = page.goto(
            _construir_url_destacada(grupo["id"]),
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        validar_respuesta_instagram(page, response, f"consultar destacada de @{username}")
        try:
            data = response.json()
        except Exception as error:
            raise ErrorConsultaInstagram("Instagram no devolvió JSON válido para una destacada.") from error
        validar_datos_instagram(data)
        reels = _buscar_reels_media(data) or []
        items: list[dict[str, Any]] = []
        for reel in reels:
            if isinstance(reel, dict) and isinstance(reel.get("items"), list):
                items.extend(item for item in reel["items"] if isinstance(item, dict))
        reportar({"historias_detectadas": len(items)})

        archivos: list[DestacadaDescargada] = []
        reservados: set[Path] = set()
        posiciones_sin_chat: dict[tuple[str, str], int] = {}
        existentes_iniciales: dict[tuple[str, str], list[Path]] = {}
        for item in items:
            story_pk = str(item.get("id") or item.get("pk") or "").strip()
            media_url = _video_url(item) or _image_url(item)
            if not story_pk or not media_url:
                reportar({"historias_procesadas": 1, "fallidas": 1})
                continue
            es_video = bool(_video_url(item))
            fecha = _fecha_destacada(item)
            extension_sugerida = "mp4" if es_video else "jpg"

            # El registro es la fuente principal de antirepetición. También
            # adoptamos archivos creados por la versión anterior, para que la
            # primera ejecución con esta mejora no vuelva a descargarlos.
            if chat_id is not None and db.destacada_ya_descargada(
                    chat_id, username, grupo["id"], story_pk):
                registro = db.obtener_destacada(
                    chat_id, username, grupo["id"], story_pk
                )
                ruta_registrada = Path(registro["ruta"]) if registro and registro.get("ruta") else None
                existentes = list(carpeta.glob(f"*_{story_pk}.*"))
                ruta_existente = existentes[0] if existentes else ruta_registrada
                if ruta_existente is not None:
                    migrada = _migrar_archivo_destacada(
                        ruta_existente,
                        carpeta,
                        username,
                        fecha,
                        extension_sugerida,
                        reservados,
                    )
                    if migrada is not None:
                        ruta_existente = migrada
                        db.actualizar_ruta_destacada(
                            chat_id,
                            username,
                            grupo["id"],
                            story_pk,
                            grupo=nombre_grupo,
                            ruta=str(migrada),
                        )
                if ruta_existente is None:
                    ruta_existente = carpeta
                archivos.append(DestacadaDescargada(
                    story_pk, nombre_grupo, ruta_existente, False
                ))
                reportar({"historias_procesadas": 1, "archivos_guardados": 1})
                continue

            existentes = list(carpeta.glob(f"*_{story_pk}.*"))
            if existentes:
                ruta_existente = existentes[0]
                if chat_id is not None:
                    db.registrar_destacada(
                        chat_id, username, grupo["id"], story_pk,
                        grupo=nombre_grupo, ruta=str(ruta_existente),
                    )
                archivos.append(DestacadaDescargada(
                    story_pk, nombre_grupo, ruta_existente, False
                ))
                reportar({"historias_procesadas": 1, "archivos_guardados": 1})
                continue

            # Las llamadas independientes que no vienen del bot no tienen
            # chat_id para consultar SQLite. Conservamos su antirepetición
            # mediante el nombre nuevo, respetando el orden de las Stories de
            # cada fecha sin confundir las reservas de esta misma ejecución.
            if chat_id is None:
                clave_fecha = (fecha, extension_sugerida)
                posicion = posiciones_sin_chat.get(clave_fecha, 0) + 1
                posiciones_sin_chat[clave_fecha] = posicion
                iniciales = existentes_iniciales.setdefault(
                    clave_fecha,
                    sorted(carpeta.glob(f"{username}_{fecha}*.{extension_sugerida}")),
                )
                if posicion <= len(iniciales):
                    archivos.append(DestacadaDescargada(
                        story_pk, nombre_grupo, iniciales[posicion - 1], False
                    ))
                    continue

            respuesta_media = context.request.get(
                media_url,
                headers={"Referer": "https://www.instagram.com/", "Accept": "*/*"},
                timeout=60_000,
                fail_on_status_code=False,
            )
            try:
                contenido, content_type = _contenido_y_tipo(respuesta_media)
            finally:
                respuesta_media.dispose()
            extension = _extension_desde_content_type(content_type, es_video)
            ruta = _nombre_archivo_destacada(
                carpeta, username, fecha, extension, reservados
            )
            nueva = not ruta.exists()
            if nueva:
                carpeta.mkdir(parents=True, exist_ok=True)
                ruta.write_bytes(contenido)
            if chat_id is not None:
                db.registrar_destacada(
                    chat_id, username, grupo["id"], story_pk,
                    grupo=nombre_grupo, ruta=str(ruta),
                )
            archivos.append(DestacadaDescargada(story_pk, nombre_grupo, ruta, nueva))
            reportar({
                "historias_procesadas": 1,
                "archivos_guardados": 1,
                "archivos_nuevos": int(nueva),
            })
        return archivos
    finally:
        page.close()


@con_sesiones()
def descargar_destacadas(username: str, user_id: int | None = None,
                         chat_id: int | None = None,
                         progress_callback: Callable[[dict[str, Any]], None] | None = None
                         ) -> ResultadoDestacadas:
    """Descarga todos los carruseles destacados accesibles para el perfil."""
    username = limpiar_username(username)
    progreso = {
        "etapa": "iniciando",
        "grupos_detectados": 0,
        "grupos_procesados": 0,
        "historias_detectadas": 0,
        "historias_procesadas": 0,
        "archivos_nuevos": 0,
        "archivos_guardados": 0,
        "fallidas": 0,
    }

    def reportar(datos: dict[str, Any] | None = None, *, etapa: str | None = None) -> None:
        if datos:
            for clave, valor in datos.items():
                if clave in {
                    "grupos_detectados", "grupos_procesados",
                    "historias_detectadas", "historias_procesadas",
                    "archivos_nuevos", "archivos_guardados", "fallidas",
                }:
                    progreso[clave] = int(progreso.get(clave, 0)) + int(valor or 0)
                else:
                    progreso[clave] = valor
        if etapa is not None:
            progreso["etapa"] = etapa
        if progress_callback is not None:
            try:
                progress_callback(dict(progreso))
            except Exception:
                pass

    reportar(etapa="iniciando")
    if chat_id is not None:
        db.inicializar()
    comprobar_sesion_local()
    # user_id se conserva para que el bot no tenga que resolver otra vez el
    # perfil. La navegación al perfil es la fuente de los IDs de destacados.
    _ = int(user_id) if user_id is not None else None
    carpeta_raiz = HISTORYS_DIR / username / HIGHLIGHTS_DIR_NAME

    with usar_playwright_sincronico(sync_playwright) as p:
        browser, context = _crear_contexto(p)
        try:
            page = context.new_page()
            respuestas: list[Any] = []

            def capturar(response) -> None:
                url = str(getattr(response, "url", ""))
                if "instagram.com" not in url:
                    return
                if not ("graphql" in url or "/api/" in url or "profile" in url.lower()):
                    return
                try:
                    tipo = (getattr(response, "headers", {}) or {}).get("content-type", "")
                    if tipo and "json" not in tipo.lower():
                        return
                    respuestas.append(response.json())
                except Exception:
                    return

            page.on("response", capturar)
            response = page.goto(
                f"https://www.instagram.com/{username}/",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            validar_respuesta_instagram(page, response, f"abrir @{username}")
            # Los círculos de destacadas se cargan después del HTML inicial.
            # Sin esta espera la página puede verse correctamente en el
            # navegador, pero todavía no haber recibido su JSON al momento de
            # inspeccionarla.
            try:
                page.wait_for_timeout(7_000)
            except Exception:
                pass
            try:
                respuestas.append(response.json())
            except Exception:
                pass
            try:
                validar_pagina_instagram(page)
            except Exception:
                # El JSON de GraphQL sigue siendo válido si la página sólo
                # entrega una pantalla mínima sin texto de perfil.
                if not respuestas:
                    raise
            try:
                respuestas.extend(_json_en_html(page.content()))
                html_destacadas = extraer_grupos_destacadas_de_html(page.content())
            except Exception:
                html_destacadas = []
            try:
                dom_destacadas = extraer_grupos_destacadas_de_dom(page.evaluate("""() => {
                    return [...document.querySelectorAll(
                        'a[href*="/stories/highlights/"], a[href*="/highlights/"]'
                    )].map(elemento => ({
                        href: elemento.href || elemento.getAttribute('href') || '',
                        text: (
                            elemento.innerText || elemento.textContent ||
                            (elemento.parentElement && elemento.parentElement.innerText) || ''
                        ).trim(),
                        title: elemento.getAttribute('title') || '',
                        aria: elemento.getAttribute('aria-label') || ''
                    }));
                }"""))
            except Exception:
                dom_destacadas = []

            grupos: list[dict[str, str]] = []
            vistos: dict[str, dict[str, str]] = {}

            def agregar_grupo(grupo: dict[str, str]) -> None:
                identificador = grupo["id"]
                anterior = vistos.get(identificador)
                if anterior is None:
                    vistos[identificador] = grupo
                    grupos.append(grupo)
                    return
                # El JSON suele tener el título real; si llegó sin título,
                # completarlo con el texto visible del DOM.
                titulo = _texto_titulo_destacada(grupo.get("title"))
                if anterior.get("title") == "Destacada" and titulo and titulo != "Destacada":
                    anterior["title"] = titulo

            for documento in respuestas:
                for grupo in extraer_grupos_destacadas(documento):
                    agregar_grupo(grupo)
            for grupo in html_destacadas + dom_destacadas:
                agregar_grupo(grupo)
            if not grupos:
                # Una respuesta vacía también puede significar que la sesión
                # no puede ver el perfil privado. Confirmamos el acceso con
                # el mecanismo existente antes de informar que no hay grupos.
                if not any(_documento_confirma_acceso(documento, username)
                           for documento in respuestas):
                    comprobar_perfil_accesible(username)
                raise SinHistoriasDestacadas(
                    f"No hemos detectado historias destacadas dentro de @{username}."
                )

            reportar({"grupos_detectados": len(grupos)}, etapa="procesando")
            nombres = nombres_destacadas_unicos([grupo["title"] for grupo in grupos])
            archivos: list[DestacadaDescargada] = []
            for grupo, nombre in zip(grupos, nombres):
                archivos.extend(_descargar_grupo(
                    context, username, grupo, carpeta_raiz / nombre, nombre, chat_id,
                    progress_callback=reportar,
                ))
                reportar({"grupos_procesados": 1}, etapa="procesando")
            _guardar_estado_contexto(context)
            nuevas = sum(archivo.nueva for archivo in archivos)
            reportar(etapa="terminado")
            return ResultadoDestacadas(
                username=username,
                carpeta=carpeta_raiz,
                grupos=tuple(nombres),
                archivos_nuevos=nuevas,
                archivos_ya_guardados=len(archivos) - nuevas,
                archivos=tuple(archivos),
            )
        finally:
            context.close()
            browser.close()

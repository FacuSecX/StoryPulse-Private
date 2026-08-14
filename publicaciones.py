#!/usr/bin/env python3
# -*- coding: utf-8 -*-


# Bot Telegram StoryPulse v1.0
# https://github.com/FacuSecX/



from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from PIL import Image
from playwright.sync_api import sync_playwright

import database as db


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

STATE_ENV = os.getenv(
    "INSTAGRAM_STORAGE_STATE",
    "instagram_state.json",
).strip()
STATE_PATH = Path(STATE_ENV).expanduser()
if not STATE_PATH.is_absolute():
    STATE_PATH = BASE_DIR / STATE_PATH

HISTORYS_DIR = Path(
    os.getenv("HISTORYS_DIR", "/historys")
).expanduser()

PROFILE_DOC_ID = os.getenv(
    "INSTAGRAM_PROFILE_DOC_ID",
    "8759034877476257",
).strip()

# Lote por página. 24 mantiene pocas consultas sin pedir lotes excesivos.
PROFILE_COUNT = int(
    os.getenv("INSTAGRAM_PUBLICATIONS_COUNT", "24")
)
PROFILE_COUNT = max(1, min(PROFILE_COUNT, 24))

# La primera consulta sigue siendo directa y compacta. Para perfiles grandes,
# la continuación se hace usando el scroll real de Instagram Web. Esto evita
# depender de la posición interna del cursor de una consulta privada que puede
# cambiar sin aviso.
MAX_SCROLL_ROUNDS = int(
    os.getenv("INSTAGRAM_PUBLICATIONS_MAX_SCROLLS", "120")
)
MAX_SCROLL_ROUNDS = max(10, min(MAX_SCROLL_ROUNDS, 300))

SCROLL_WAIT_MS = int(
    os.getenv("INSTAGRAM_PUBLICATIONS_SCROLL_WAIT_MS", "1400")
)
SCROLL_WAIT_MS = max(500, min(SCROLL_WAIT_MS, 5000))

SCROLL_STABLE_ROUNDS = int(
    os.getenv("INSTAGRAM_PUBLICATIONS_STABLE_ROUNDS", "5")
)
SCROLL_STABLE_ROUNDS = max(3, min(SCROLL_STABLE_ROUNDS, 12))

TZ = ZoneInfo(
    os.getenv(
        "STORYPULSE_TIMEZONE",
        "America/Argentina/Buenos_Aires",
    )
)


@dataclass(frozen=True)
class MediaPublicacion:
    indice: int
    url: str
    es_video: bool


@dataclass(frozen=True)
class PublicacionGuardada:
    post_id: str
    fecha: str
    archivos: tuple[Path, ...]


@dataclass(frozen=True)
class ResultadoPublicaciones:
    username: str
    carpeta: Path
    consultas_graphql: int
    publicaciones_totales_perfil: int | None
    publicaciones_detectadas: int
    publicaciones_nuevas: int
    publicaciones_ya_descargadas: int
    publicaciones_fallidas: int
    archivos_nuevos: int
    sincronizacion_completa: bool
    corte_por_antirepeticion: bool
    guardadas: tuple[PublicacionGuardada, ...]


def normalizar_username(valor: str) -> str:
    valor = str(valor).strip()
    m = re.search(
        r"instagram\.com/(?:stories/)?([^/?#]+)",
        valor,
        re.I,
    )
    if m:
        valor = m.group(1)

    valor = valor.strip().lstrip("@").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9._]+", valor):
        raise ValueError("Username inválido.")

    return valor.lower()


def construir_profile_url(
    username: str,
    cursor: str | None = None,
) -> str:
    data_variables: dict[str, Any] = {
        "count": PROFILE_COUNT,
        "include_relationship_info": True,
        "latest_besties_reel_media": True,
        "latest_reel_media": True,
    }

    # El cursor pertenece al input `data` de la conexión timeline XDT.
    if cursor:
        data_variables["after"] = str(cursor)

    variables = {
        "data": data_variables,
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


def leer_json_respuesta(page, response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        texto = page.locator("body").inner_text()
        try:
            data = json.loads(texto)
        except Exception as exc:
            raise RuntimeError(
                "Instagram no devolvió JSON válido en publicaciones."
            ) from exc

    if not isinstance(data, dict):
        raise RuntimeError(
            "La respuesta GraphQL de publicaciones no es un objeto JSON."
        )

    if data.get("errors"):
        raise RuntimeError(
            "Instagram devolvió un error GraphQL en publicaciones."
        )

    return data


def iter_dicts(obj: Any) -> Iterable[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for valor in obj.values():
            yield from iter_dicts(valor)
    elif isinstance(obj, list):
        for valor in obj:
            yield from iter_dicts(valor)


def node_parece_publicacion(node: Any) -> bool:
    if not isinstance(node, dict):
        return False

    tiene_id = any(
        node.get(k) is not None
        for k in ("id", "pk")
    )
    tiene_codigo = any(
        node.get(k)
        for k in ("shortcode", "code")
    )
    tiene_media = any(
        node.get(k)
        for k in (
            "display_url",
            "thumbnail_src",
            "image_versions2",
            "display_resources",
            "video_url",
            "video_versions",
            "carousel_media",
            "edge_sidecar_to_children",
        )
    )

    return bool(
        tiene_id
        and (tiene_codigo or tiene_media)
    )


def extraer_edges(
    conexion: Any,
) -> list[dict[str, Any]]:
    if not isinstance(conexion, dict):
        return []

    edges = conexion.get("edges")
    if not isinstance(edges, list):
        return []

    nodes: list[dict[str, Any]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue

        node = edge.get("node")
        if node_parece_publicacion(node):
            nodes.append(node)

    return nodes


def encontrar_conexion_publicaciones(
    data: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str]:
    """
    Devuelve (nodes, page_info, ruta_detectada).

    Conservamos page_info porque es imprescindible para recorrer perfiles
    con más publicaciones que las que caben en el primer lote.
    """
    # Forma clásica.
    for obj in iter_dicts(data):
        conexion = obj.get(
            "edge_owner_to_timeline_media"
        )
        nodes = extraer_edges(conexion)
        if nodes:
            page_info = (
                conexion.get("page_info")
                if isinstance(conexion, dict)
                else None
            )
            return (
                nodes,
                page_info if isinstance(page_info, dict) else None,
                "edge_owner_to_timeline_media",
            )

    # Forma XDT usada actualmente por el proyecto.
    for obj in iter_dicts(data):
        for clave, valor in obj.items():
            clave_baja = str(clave).lower()
            if not isinstance(valor, dict):
                continue
            if not any(
                palabra in clave_baja
                for palabra in (
                    "timeline",
                    "feed",
                    "media",
                )
            ):
                continue

            nodes = extraer_edges(valor)
            if nodes:
                page_info = valor.get("page_info")
                return (
                    nodes,
                    page_info if isinstance(page_info, dict) else None,
                    str(clave),
                )

    # Respaldo defensivo.
    for obj in iter_dicts(data):
        nodes = extraer_edges(obj)
        if nodes:
            page_info = obj.get("page_info")
            return (
                nodes,
                page_info if isinstance(page_info, dict) else None,
                "conexion_edges_generica",
            )

    return [], None, "no_detectada"



def _entero_publicaciones(valor: Any) -> int | None:
    """Convierte contadores de publicaciones a entero cuando es seguro."""
    if isinstance(valor, bool) or valor is None:
        return None
    if isinstance(valor, int):
        return valor if valor >= 0 else None
    if isinstance(valor, float):
        return int(valor) if valor >= 0 else None

    texto = str(valor).strip()
    if not texto:
        return None

    # Para contadores completos del perfil Instagram suele devolver enteros.
    # También aceptamos separadores de miles visibles en la interfaz.
    if re.fullmatch(r"[0-9][0-9.,]*", texto):
        limpio = texto.replace(".", "").replace(",", "")
        try:
            return int(limpio)
        except ValueError:
            return None

    return None


def extraer_total_publicaciones(data: dict[str, Any]) -> int | None:
    """
    Busca el contador total de posts del perfil sin usar valores genéricos
    ajenos al timeline. Devuelve el mayor candidato razonable encontrado.
    """
    candidatos: list[int] = []

    for obj in iter_dicts(data):
        conexion = obj.get("edge_owner_to_timeline_media")
        if isinstance(conexion, dict):
            total = _entero_publicaciones(conexion.get("count"))
            if total is not None:
                candidatos.append(total)

        # Respuestas XDT/API de perfil suelen incluir media_count.
        total_media = _entero_publicaciones(obj.get("media_count"))
        if total_media is not None:
            candidatos.append(total_media)

        for clave, valor in obj.items():
            clave_baja = str(clave).lower()
            if not isinstance(valor, dict):
                continue
            if "user_timeline" not in clave_baja and not (
                "timeline" in clave_baja and "feed" in clave_baja
            ):
                continue

            total = _entero_publicaciones(valor.get("count"))
            if total is not None:
                candidatos.append(total)

    candidatos = [n for n in candidatos if 0 <= n <= 1_000_000]
    return max(candidatos) if candidatos else None


def extraer_total_desde_texto_perfil(texto: str) -> int | None:
    """Fallback para el contador visible del encabezado del perfil."""
    if not texto:
        return None

    patrones = (
        r"([0-9][0-9.,]*)\s+publicaciones\b",
        r"([0-9][0-9.,]*)\s+posts\b",
    )
    for patron in patrones:
        match = re.search(patron, texto, re.I)
        if not match:
            continue
        total = _entero_publicaciones(match.group(1))
        if total is not None:
            return total

    return None


def _username_del_node(node: dict[str, Any]) -> str | None:
    for clave in ("user", "owner"):
        usuario = node.get(clave)
        if not isinstance(usuario, dict):
            continue
        valor = usuario.get("username")
        if isinstance(valor, str) and valor.strip():
            return valor.strip().lower()
    return None


def _node_pertenece_al_usuario(
    node: dict[str, Any],
    username: str,
) -> bool:
    propietario = _username_del_node(node)
    if propietario is None:
        # Algunas conexiones del timeline omiten el propietario en cada node.
        # Como sólo llamamos esta función sobre conexiones específicas del
        # timeline del perfil, la ausencia no invalida el elemento.
        return True
    return propietario == username.lower()


def _deduplicar_nodes(
    nodes: Iterable[dict[str, Any]],
    username: str,
) -> list[dict[str, Any]]:
    salida: list[dict[str, Any]] = []
    vistos: set[str] = set()

    for node in nodes:
        if not node_parece_publicacion(node):
            continue
        # La conexión ya fue identificada como timeline del perfil solicitado.
        # No filtramos por node.user porque una publicación colaborativa puede
        # pertenecer al grid del perfil aunque el autor principal sea otra cuenta.
        post_id = texto_id(node)
        if not post_id or post_id in vistos:
            continue
        vistos.add(post_id)
        salida.append(node)

    return salida


def extraer_nodos_timeline_navegador(
    data: dict[str, Any],
    response_url: str,
    username: str,
) -> list[dict[str, Any]]:
    """
    Extrae sólo publicaciones provenientes de conexiones que representan el
    timeline del usuario. Se evita el fallback genérico para no incorporar
    recomendaciones, Explore u otros feeds que la página pueda cargar.
    """
    encontrados: list[dict[str, Any]] = []
    url_baja = str(response_url).lower()

    for obj in iter_dicts(data):
        conexion_clasica = obj.get("edge_owner_to_timeline_media")
        if isinstance(conexion_clasica, dict):
            encontrados.extend(extraer_edges(conexion_clasica))

        for clave, valor in obj.items():
            clave_baja = str(clave).lower()
            if not isinstance(valor, dict):
                continue

            es_timeline_usuario = (
                "user_timeline" in clave_baja
                or "feed__user_timeline" in clave_baja
                or (
                    "timeline" in clave_baja
                    and "feed" in clave_baja
                    and "user" in clave_baja
                )
            )
            if not es_timeline_usuario:
                continue

            encontrados.extend(extraer_edges(valor))
            items = valor.get("items")
            if isinstance(items, list):
                encontrados.extend(
                    item for item in items if isinstance(item, dict)
                )

    # Algunas cargas infinitas usan una respuesta REST /feed/user/... con
    # `items` en vez de una conexión GraphQL edges/node.
    if "/feed/user" in url_baja or "user_timeline" in url_baja:
        for obj in iter_dicts(data):
            items = obj.get("items")
            if isinstance(items, list):
                encontrados.extend(
                    item for item in items if isinstance(item, dict)
                )

    return _deduplicar_nodes(encontrados, username)


def descubrir_publicaciones_por_scroll(
    context,
    username: str,
    *,
    vistos_iniciales: set[str],
    conocidos: set[str],
    historial_ya_completo: bool,
    total_objetivo: int | None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], int, bool, bool, int | None]:
    """
    Abre el perfil real y deja que Instagram Web genere sus propias peticiones
    de paginación al hacer scroll. De esta forma no dependemos de adivinar dónde
    espera Instagram el cursor interno de una consulta privada.

    Devuelve:
      nodes capturados,
      cantidad de respuestas timeline capturadas,
      si se alcanzó el final con suficiente certeza,
      si se encontró contenido ya conocido (para corte incremental),
      contador total del perfil si pudo detectarse.
    """
    page = context.new_page()
    capturados: dict[str, dict[str, Any]] = {}
    respuestas_timeline = 0
    encontro_conocido = False
    total_detectado = total_objetivo

    def reportar_scroll() -> None:
        if progress_callback is None:
            return

        try:
            progress_callback(
                {
                    "etapa": "recorriendo",
                    "total_perfil": total_detectado,
                    "posts_encontrados": len(
                        vistos_iniciales.union(capturados.keys())
                    ),
                    "respuestas_timeline": respuestas_timeline,
                }
            )
        except Exception:
            # El progreso es informativo: nunca debe interrumpir la descarga.
            pass

    def procesar_response(response) -> None:
        nonlocal respuestas_timeline, encontro_conocido, total_detectado

        url = str(response.url)
        url_baja = url.lower()
        if "instagram.com" not in url_baja:
            return
        if not (
            "/graphql/query" in url_baja
            or "/api/v1/feed/user" in url_baja
            or "user_timeline" in url_baja
        ):
            return

        try:
            data = response.json()
        except Exception:
            return

        if not isinstance(data, dict):
            return

        nodes = extraer_nodos_timeline_navegador(
            data,
            url,
            username,
        )
        if not nodes:
            return

        respuestas_timeline += 1

        posible_total = extraer_total_publicaciones(data)
        if posible_total is not None:
            if total_detectado is None:
                total_detectado = posible_total
            else:
                total_detectado = max(total_detectado, posible_total)

        for node in nodes:
            post_id = texto_id(node)
            if not post_id:
                continue
            capturados.setdefault(post_id, node)
            if historial_ya_completo and post_id in conocidos:
                encontro_conocido = True

        reportar_scroll()

    page.on("response", procesar_response)

    try:
        response = page.goto(
            f"https://www.instagram.com/{username}/",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        if response is not None and response.status in (401, 403):
            raise RuntimeError(
                "Instagram rechazó la sesión al abrir el perfil "
                f"(HTTP {response.status})."
            )

        page.wait_for_timeout(2500)

        try:
            texto_perfil = page.locator("body").inner_text(timeout=5000)
            total_texto = extraer_total_desde_texto_perfil(texto_perfil)
            if total_texto is not None:
                if total_detectado is None:
                    total_detectado = total_texto
                else:
                    total_detectado = max(total_detectado, total_texto)
        except Exception:
            pass

        reportar_scroll()

        estable = 0
        altura_anterior = 0
        llego_al_final = False

        for _ in range(MAX_SCROLL_ROUNDS):
            total_vistos = len(
                vistos_iniciales.union(capturados.keys())
            )

            if (
                total_detectado is not None
                and total_vistos >= total_detectado
            ):
                llego_al_final = True
                break

            if historial_ya_completo and encontro_conocido:
                break

            cantidad_antes = len(capturados)

            try:
                medidas_antes = page.evaluate(
                    """() => ({
                        y: window.scrollY,
                        h: window.innerHeight,
                        total: Math.max(
                            document.body.scrollHeight,
                            document.documentElement.scrollHeight
                        )
                    })"""
                )
                altura_antes = int(medidas_antes.get("total") or 0)
            except Exception:
                altura_antes = altura_anterior

            # Llegar al fondo es lo que dispara la carga infinita del perfil.
            page.evaluate(
                """() => window.scrollTo(
                    0,
                    Math.max(
                        document.body.scrollHeight,
                        document.documentElement.scrollHeight
                    )
                )"""
            )
            page.wait_for_timeout(SCROLL_WAIT_MS)

            try:
                medidas = page.evaluate(
                    """() => ({
                        y: window.scrollY,
                        h: window.innerHeight,
                        total: Math.max(
                            document.body.scrollHeight,
                            document.documentElement.scrollHeight
                        )
                    })"""
                )
                altura_actual = int(medidas.get("total") or 0)
                al_fondo = (
                    float(medidas.get("y") or 0)
                    + float(medidas.get("h") or 0)
                    >= altura_actual - 80
                )
            except Exception:
                altura_actual = altura_antes
                al_fondo = True

            nuevos = len(capturados) - cantidad_antes
            crecio_documento = altura_actual > max(altura_antes, altura_anterior) + 20

            if nuevos > 0 or crecio_documento:
                estable = 0
            elif al_fondo:
                estable += 1
            else:
                estable = 0

            altura_anterior = altura_actual
            reportar_scroll()

            if estable >= SCROLL_STABLE_ROUNDS:
                total_vistos = len(
                    vistos_iniciales.union(capturados.keys())
                )
                # Si conocemos el contador del perfil, sólo afirmamos que
                # llegamos al final cuando alcanzamos ese número. Si no hay
                # contador disponible, varias rondas estables en el fondo son
                # nuestra señal de finalización.
                llego_al_final = (
                    total_detectado is None
                    or total_vistos >= total_detectado
                )
                break

        return (
            list(capturados.values()),
            respuestas_timeline,
            llego_al_final,
            encontro_conocido,
            total_detectado,
        )
    finally:
        page.close()

def texto_id(node: dict[str, Any]) -> str:
    valor = (
        node.get("pk")
        or node.get("id")
        or node.get("media_id")
    )
    return str(valor or "").strip()


def fecha_media(node: dict[str, Any]) -> datetime | None:
    candidatos = (
        node.get("taken_at_timestamp"),
        node.get("taken_at"),
        node.get("device_timestamp"),
    )

    for valor in candidatos:
        try:
            if valor is None:
                continue
            return datetime.fromtimestamp(
                float(valor),
                tz=timezone.utc,
            )
        except (TypeError, ValueError, OSError):
            continue

    return None


def extraer_video_url(
    node: dict[str, Any],
) -> str | None:
    directa = node.get("video_url")
    if (
        isinstance(directa, str)
        and directa.startswith("http")
    ):
        return directa

    versiones = node.get("video_versions")
    if isinstance(versiones, list):
        for item in versiones:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if (
                isinstance(url, str)
                and url.startswith("http")
            ):
                return url

    recursos = node.get("video_resources")
    if isinstance(recursos, list):
        for item in reversed(recursos):
            if not isinstance(item, dict):
                continue
            url = item.get("src")
            if (
                isinstance(url, str)
                and url.startswith("http")
            ):
                return url

    return None


def extraer_imagen_url(
    node: dict[str, Any],
) -> str | None:
    for clave in (
        "display_url",
        "thumbnail_src",
    ):
        valor = node.get(clave)
        if (
            isinstance(valor, str)
            and valor.startswith("http")
        ):
            return valor

    versiones = node.get("image_versions2")
    if isinstance(versiones, dict):
        candidatos = versiones.get("candidates")
        if isinstance(candidatos, list):
            for item in candidatos:
                if not isinstance(item, dict):
                    continue
                url = item.get("url")
                if (
                    isinstance(url, str)
                    and url.startswith("http")
                ):
                    return url

    recursos = node.get("display_resources")
    if isinstance(recursos, list):
        for item in reversed(recursos):
            if not isinstance(item, dict):
                continue
            url = item.get("src")
            if (
                isinstance(url, str)
                and url.startswith("http")
            ):
                return url

    return None


def es_video(node: dict[str, Any]) -> bool:
    if node.get("is_video") is True:
        return True

    if str(node.get("media_type")) == "2":
        return True

    typename = str(
        node.get("__typename", "")
    ).lower()
    if "video" in typename:
        return True

    return bool(extraer_video_url(node))


def hijos_carrusel(
    node: dict[str, Any],
) -> list[dict[str, Any]]:
    sidecar = node.get(
        "edge_sidecar_to_children"
    )
    if isinstance(sidecar, dict):
        edges = sidecar.get("edges")
        if isinstance(edges, list):
            hijos = []
            for edge in edges:
                if not isinstance(edge, dict):
                    continue
                hijo = edge.get("node")
                if isinstance(hijo, dict):
                    hijos.append(hijo)
            if hijos:
                return hijos

    carousel = node.get("carousel_media")
    if isinstance(carousel, list):
        hijos = [
            item
            for item in carousel
            if isinstance(item, dict)
        ]
        if hijos:
            return hijos

    return [node]


def extraer_medios_publicacion(
    node: dict[str, Any],
) -> list[MediaPublicacion]:
    medios: list[MediaPublicacion] = []
    hijos = hijos_carrusel(node)

    for indice, hijo in enumerate(
        hijos,
        1,
    ):
        video = es_video(hijo)
        url = (
            extraer_video_url(hijo)
            if video
            else extraer_imagen_url(hijo)
        )

        # En el módulo definitivo no registramos un video como completo
        # usando sólo su thumbnail. Si el timeline no trae la URL real,
        # dejamos el post pendiente en vez de hacer una consulta extra.
        if not url:
            return []

        medios.append(
            MediaPublicacion(
                indice=indice,
                url=url,
                es_video=video,
            )
        )

    if len(medios) != len(hijos):
        return []

    return medios


def guardar_state_atomico(context) -> None:
    temporal = STATE_PATH.with_suffix(
        STATE_PATH.suffix + ".tmp"
    )
    context.storage_state(
        path=str(temporal)
    )
    temporal.replace(STATE_PATH)


def _guardar_imagen_jpg(
    contenido: bytes,
    destino: Path,
) -> None:
    with Image.open(BytesIO(contenido)) as imagen:
        if imagen.mode in ("RGBA", "LA"):
            fondo = Image.new(
                "RGB",
                imagen.size,
                (255, 255, 255),
            )
            alpha = imagen.getchannel("A")
            fondo.paste(
                imagen.convert("RGB"),
                mask=alpha,
            )
            imagen_final = fondo
        elif imagen.mode != "RGB":
            imagen_final = imagen.convert("RGB")
        else:
            imagen_final = imagen.copy()

        imagen_final.save(
            destino,
            format="JPEG",
            quality=95,
            optimize=True,
        )


def _nombres_finales(
    carpeta: Path,
    username: str,
    fecha: str,
    tipos_video: list[bool],
) -> list[Path]:
    """
    Respeta USERNAME_YYYY-MM-DD.jpg para un post simple.

    Carrusel:
      USERNAME_YYYY-MM-DD_01.jpg
      USERNAME_YYYY-MM-DD_02.mp4

    Si hay dos posts distintos el mismo día, agrega _2, _3... sólo
    cuando es necesario para no pisar archivos existentes.
    """
    cantidad = len(tipos_video)
    intento = 1

    while True:
        base = f"{username}_{fecha}"
        if intento > 1:
            base += f"_{intento}"

        rutas: list[Path] = []
        for indice, video in enumerate(
            tipos_video,
            1,
        ):
            extension = "mp4" if video else "jpg"
            if cantidad == 1:
                nombre = f"{base}.{extension}"
            else:
                nombre = (
                    f"{base}_{indice:02d}."
                    f"{extension}"
                )
            rutas.append(carpeta / nombre)

        if not any(ruta.exists() for ruta in rutas):
            return rutas

        intento += 1


def descargar_publicaciones(
    username: str,
    chat_id: int,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> ResultadoPublicaciones:
    """
    Descarga publicaciones de un perfil.

    Estrategia V2.14:
      1. Obtiene el lote reciente mediante la consulta GraphQL ya probada.
      2. Si hace falta continuar, abre el perfil web y deja que Instagram
         genere su paginación real mediante scroll.
      3. Captura únicamente respuestas del timeline del usuario y procesa
         Post ID únicos.
      4. En perfiles ya sincronizados, puede detener el scroll cuando alcanza
         contenido conocido.

    Así se mantiene una revisión rápida para el uso diario y un backfill
    completo para perfiles grandes sin depender de un cursor construido a mano.
    """
    db.inicializar()

    username = normalizar_username(username)
    chat_id = int(chat_id)

    if not STATE_PATH.exists():
        raise FileNotFoundError(
            f"Falta {STATE_PATH.name}"
        )

    carpeta = HISTORYS_DIR / username / "publicaciones"
    carpeta.mkdir(parents=True, exist_ok=True)

    conocidos = db.ids_publicaciones_descargadas(
        chat_id,
        username,
    )
    # Snapshot de lo que existía ANTES de esta ejecución. Es importante para
    # el corte incremental: los posts que acabamos de descargar en el primer
    # lote no deben confundirse con contenido histórico ya conocido.
    conocidos_antes_de_ejecutar = set(conocidos)

    historial_ya_completo = db.publicaciones_historial_completo(
        chat_id,
        username,
    )

    detectadas = 0
    nuevas = 0
    ya_descargadas = 0
    fallidas = 0
    archivos_nuevos = 0
    consultas_graphql = 0
    corte_por_antirepeticion = False
    llego_al_final = False
    total_perfil: int | None = None
    guardadas: list[PublicacionGuardada] = []

    # Deduplicación entre el primer lote y las respuestas producidas por scroll.
    vistos_en_esta_ejecucion: set[str] = set()

    def reportar_progreso(
        etapa: str,
        *,
        posts_encontrados: int | None = None,
        total_detectado: int | None = None,
        respuestas_timeline: int | None = None,
    ) -> None:
        if progress_callback is None:
            return

        try:
            datos = {
                "etapa": etapa,
                "total_perfil": (
                    total_detectado
                    if total_detectado is not None
                    else total_perfil
                ),
                "posts_encontrados": (
                    len(vistos_en_esta_ejecucion)
                    if posts_encontrados is None
                    else int(posts_encontrados)
                ),
                "posts_procesados": detectadas,
                "publicaciones_nuevas": nuevas,
                "ya_descargadas": ya_descargadas,
                "fallidas": fallidas,
                "archivos_guardados": archivos_nuevos,
                "consultas_graphql": consultas_graphql,
            }
            if respuestas_timeline is not None:
                datos["consultas_graphql"] = (
                    consultas_graphql + int(respuestas_timeline)
                )
            progress_callback(datos)
        except Exception:
            # El progreso es informativo: nunca debe interrumpir la descarga.
            pass

    reportar_progreso("iniciando")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=str(STATE_PATH),
            viewport={"width": 1365, "height": 900},
            locale="es-AR",
            timezone_id=str(TZ.key),
        )

        def procesar_lote(
            nodes: Iterable[dict[str, Any]],
        ) -> tuple[int, int]:
            nonlocal detectadas, nuevas, ya_descargadas
            nonlocal fallidas, archivos_nuevos

            conocidos_lote = 0
            nuevos_lote = 0

            for node in nodes:
                post_id = texto_id(node)
                if not post_id:
                    fallidas += 1
                    continue

                if post_id in vistos_en_esta_ejecucion:
                    continue

                vistos_en_esta_ejecucion.add(post_id)
                detectadas += 1

                if post_id in conocidos:
                    ya_descargadas += 1
                    conocidos_lote += 1
                    reportar_progreso("procesando")
                    continue

                nuevos_lote += 1

                fecha_utc = fecha_media(node)
                if fecha_utc is None:
                    fallidas += 1
                    reportar_progreso("procesando")
                    continue

                fecha = fecha_utc.astimezone(TZ).strftime("%Y-%m-%d")
                medios = extraer_medios_publicacion(node)
                if not medios:
                    fallidas += 1
                    reportar_progreso("procesando")
                    continue

                temporales: list[Path] = []
                tipos_video = [medio.es_video for medio in medios]

                try:
                    for medio in medios:
                        respuesta_media = context.request.get(
                            medio.url,
                            headers={
                                "Referer": (
                                    "https://www.instagram.com/"
                                    f"{username}/"
                                ),
                            },
                            timeout=60000,
                        )

                        if not respuesta_media.ok:
                            raise RuntimeError(
                                "CDN de Instagram respondió "
                                f"HTTP {respuesta_media.status}."
                            )

                        sufijo = ".mp4" if medio.es_video else ".jpg"
                        temporal = carpeta / (
                            f".storypulse_tmp_{post_id}_"
                            f"{medio.indice:02d}{sufijo}"
                        )
                        temporal.unlink(missing_ok=True)

                        contenido = respuesta_media.body()
                        if medio.es_video:
                            temporal.write_bytes(contenido)
                        else:
                            _guardar_imagen_jpg(contenido, temporal)

                        temporales.append(temporal)

                    finales = _nombres_finales(
                        carpeta,
                        username,
                        fecha,
                        tipos_video,
                    )

                    for temporal, final in zip(
                        temporales,
                        finales,
                        strict=True,
                    ):
                        temporal.replace(final)

                    try:
                        db.registrar_publicacion(
                            chat_id,
                            username,
                            post_id,
                            fecha_publicacion=fecha,
                            cantidad_archivos=len(finales),
                        )
                    except Exception:
                        for final in finales:
                            try:
                                final.unlink(missing_ok=True)
                            except Exception:
                                pass
                        raise

                    conocidos.add(post_id)
                    nuevas += 1
                    archivos_nuevos += len(finales)
                    guardadas.append(
                        PublicacionGuardada(
                            post_id=post_id,
                            fecha=fecha,
                            archivos=tuple(finales),
                        )
                    )
                    reportar_progreso("procesando")

                except Exception:
                    for temporal in temporales:
                        try:
                            temporal.unlink(missing_ok=True)
                        except Exception:
                            pass
                    fallidas += 1
                    reportar_progreso("procesando")
                    continue

            return conocidos_lote, nuevos_lote

        try:
            # ------------------------------------------------------------
            # 1) PRIMER LOTE: consulta directa, rápida y conocida.
            # ------------------------------------------------------------
            page = context.new_page()
            response = page.goto(
                construir_profile_url(username),
                wait_until="domcontentloaded",
                timeout=60000,
            )
            consultas_graphql += 1

            if response is None:
                raise RuntimeError(
                    "Instagram no devolvió respuesta GraphQL de publicaciones."
                )
            if response.status in (401, 403):
                raise RuntimeError(
                    "Instagram rechazó la sesión "
                    f"(HTTP {response.status})."
                )
            if response.status == 429:
                raise RuntimeError(
                    "Instagram respondió 429 / rate limit."
                )

            data = leer_json_respuesta(page, response)
            nodes, page_info, _ruta = encontrar_conexion_publicaciones(data)
            total_perfil = extraer_total_publicaciones(data)
            reportar_progreso("leyendo_perfil")

            conocidos_primero, _ = procesar_lote(nodes)
            guardar_state_atomico(context)
            page.close()

            has_next = bool(
                page_info
                and page_info.get("has_next_page")
            )

            if not nodes:
                llego_al_final = True
            elif historial_ya_completo and conocidos_primero > 0:
                # Uso diario: ya tocamos el historial conocido en el lote
                # reciente, así que no hay motivo para recorrer el perfil.
                corte_por_antirepeticion = True
            else:
                faltan_segun_total = (
                    total_perfil is not None
                    and len(vistos_en_esta_ejecucion) < total_perfil
                )

                if has_next or faltan_segun_total:
                    # ----------------------------------------------------
                    # 2) CONTINUACIÓN: paginación REAL de Instagram Web.
                    # ----------------------------------------------------
                    reportar_progreso("recorriendo")

                    def progreso_scroll(datos: dict[str, Any]) -> None:
                        reportar_progreso(
                            "recorriendo",
                            posts_encontrados=int(
                                datos.get("posts_encontrados")
                                or len(vistos_en_esta_ejecucion)
                            ),
                            total_detectado=datos.get("total_perfil"),
                            respuestas_timeline=int(
                                datos.get("respuestas_timeline") or 0
                            ),
                        )

                    (
                        nodes_scroll,
                        respuestas_scroll,
                        final_scroll,
                        encontro_conocido_scroll,
                        total_scroll,
                    ) = descubrir_publicaciones_por_scroll(
                        context,
                        username,
                        vistos_iniciales=set(vistos_en_esta_ejecucion),
                        conocidos=conocidos_antes_de_ejecutar,
                        historial_ya_completo=historial_ya_completo,
                        total_objetivo=total_perfil,
                        progress_callback=progreso_scroll,
                    )

                    consultas_graphql += respuestas_scroll
                    if total_scroll is not None:
                        total_perfil = (
                            total_scroll
                            if total_perfil is None
                            else max(total_perfil, total_scroll)
                        )

                    procesar_lote(nodes_scroll)
                    guardar_state_atomico(context)

                    if historial_ya_completo and encontro_conocido_scroll:
                        corte_por_antirepeticion = True
                    elif final_scroll:
                        llego_al_final = True

                    # Si conocemos el total, éste manda sobre la señal de scroll.
                    if total_perfil is not None:
                        llego_al_final = (
                            len(vistos_en_esta_ejecucion) >= total_perfil
                        )
                else:
                    llego_al_final = True

            reportar_progreso("finalizando")

            # Un perfil ya sincronizado conserva ese estado cuando la ejecución
            # corta por antirepetición. Para una primera sincronización, sólo lo
            # marcamos completo si alcanzamos el final y no quedó ningún post
            # fallido/pendiente.
            if llego_al_final and fallidas == 0:
                db.marcar_publicaciones_historial_completo(
                    chat_id,
                    username,
                )
                historial_ya_completo = True
            elif not historial_ya_completo:
                # Mantener explícitamente "incompleto" para que una ejecución
                # futura vuelva a intentar recorrer las páginas faltantes.
                historial_ya_completo = False

            reportar_progreso("terminado")

        finally:
            try:
                guardar_state_atomico(context)
            except Exception:
                pass
            browser.close()

    return ResultadoPublicaciones(
        username=username,
        carpeta=carpeta,
        consultas_graphql=consultas_graphql,
        publicaciones_totales_perfil=total_perfil,
        publicaciones_detectadas=detectadas,
        publicaciones_nuevas=nuevas,
        publicaciones_ya_descargadas=ya_descargadas,
        publicaciones_fallidas=fallidas,
        archivos_nuevos=archivos_nuevos,
        sincronizacion_completa=historial_ya_completo,
        corte_por_antirepeticion=corte_por_antirepeticion,
        guardadas=tuple(guardadas),
    )


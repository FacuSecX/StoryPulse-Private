# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Portadas de Stories: máxima resolución disponible, sin descargar videos."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Callable
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError

MODOS_HISTORIAS = {'normal', 'portada'}


@dataclass(frozen=True)
class FalloPortada:
    story_pk: str
    motivo: str


class LoteHistorias(list):
    """Compatible con la lista habitual; conserva fallos por Story separadamente."""
    def __init__(self):
        super().__init__()
        self.fallos_portada: list[FalloPortada] = []
        self.omitidas = 0


class PortadaNoDisponible(RuntimeError):
    pass


class ResultadoRevision(int):
    """Conserva el contador existente y comunica portadas pendientes al grupo."""
    def __new__(cls, procesadas: int, portadas_pendientes: int = 0):
        result = super().__new__(cls, procesadas)
        result.portadas_pendientes = portadas_pendientes
        return result


@dataclass(frozen=True)
class CandidataPortada:
    url: str
    ancho: int
    alto: int
    principal: bool
    prioridad: int

    @property
    def area(self) -> int:
        return self.ancho*self.alto if self.ancho and self.alto else 0


@dataclass(frozen=True)
class PortadaJPG:
    contenido: bytes
    ancho: int
    alto: int

    @property
    def area(self) -> int:
        return self.ancho*self.alto


def _dimension(valor) -> int:
    try:
        number = int(valor)
        return number if number > 0 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def candidatas_portada(item: dict) -> list[CandidataPortada]:
    candidates: dict[str, CandidataPortada] = {}
    videos = {item['video_url']} if isinstance(item.get('video_url'), str) else set()
    for field in ('video_versions', 'video_resources'):
        resources = item.get(field)
        for resource in resources if isinstance(resources, list) else []:
            if isinstance(resource, dict):
                url = resource.get('url') or resource.get('src')
                if isinstance(url, str):
                    videos.add(url)

    def add(url, width=0, height=0, *, principal=True, prioridad=1):
        if not isinstance(url, str) or not url.startswith(('https://', 'http://')):
            return
        try:
            if url in videos or urlsplit(url).path.lower().endswith(('.mp4', '.mov', '.m4v', '.webm', '.m3u8')):
                return
        except ValueError:
            return
        candidate = CandidataPortada(url, _dimension(width), _dimension(height), principal, prioridad)
        previous = candidates.get(url)
        if previous:
            sized = max((previous, candidate), key=lambda c: c.area)
            candidate = CandidataPortada(url, sized.ancho, sized.alto,
                                        previous.principal or principal,
                                        max(previous.prioridad, prioridad))
        candidates[url] = candidate

    versions = item.get('image_versions2')
    if isinstance(versions, dict):
        resources = versions.get('candidates')
        for candidate in resources if isinstance(resources, list) else []:
            if isinstance(candidate, dict):
                add(candidate.get('url'), candidate.get('width'), candidate.get('height'))
    for field in ('display_resources', 'image_resources'):
        resources = item.get(field)
        if isinstance(resources, list):
            for resource in resources:
                if isinstance(resource, dict):
                    add(resource.get('src') or resource.get('url'),
                        resource.get('config_width') or resource.get('width'),
                        resource.get('config_height') or resource.get('height'))
    dimensions = item.get('dimensions')
    dimensions = dimensions if isinstance(dimensions, dict) else {}
    add(item.get('display_url'), dimensions.get('width') or item.get('original_width'),
        dimensions.get('height') or item.get('original_height'), prioridad=2)
    thumbnail = item.get('thumbnail_dimensions')
    thumbnail = thumbnail if isinstance(thumbnail, dict) else {}
    add(item.get('thumbnail_src'), thumbnail.get('width') or item.get('thumbnail_width'),
        thumbnail.get('height') or item.get('thumbnail_height'), principal=False, prioridad=0)
    return list(candidates.values())


def preparar_jpg(contenido: bytes) -> PortadaJPG:
    try:
        with Image.open(BytesIO(contenido)) as original:
            original.load()
            preserve = original.format == 'JPEG' and original.mode in {'RGB', 'L'}
            orientation = original.getexif().get(274, 1)
            oriented = original if preserve or orientation == 1 else ImageOps.exif_transpose(original)
            rgb = None
            try:
                width, height = oriented.size
                if preserve and orientation in {5, 6, 7, 8}:
                    width, height = height, width
                if width <= 0 or height <= 0:
                    raise PortadaNoDisponible('La vista previa no tiene dimensiones válidas.')
                if preserve:
                    rgb = original
                elif oriented.mode in {'RGBA', 'LA'} or 'transparency' in oriented.info:
                    with oriented.convert('RGBA') as rgba:
                        rgb = Image.new('RGB', rgba.size, 'white')
                        with rgba.getchannel('A') as alpha:
                            rgb.paste(rgba, mask=alpha)
                else:
                    rgb = oriented.convert('RGB')
                extrema = rgb.getextrema()
                upper = extrema[1] if isinstance(extrema[0], int) else max(hi for lo, hi in extrema)
                if upper <= 8:
                    raise PortadaNoDisponible('La vista previa está prácticamente negra.')
                if preserve:
                    return PortadaJPG(contenido, width, height)
                output = BytesIO()
                rgb.save(output, 'JPEG', quality=98, subsampling=0)
                return PortadaJPG(output.getvalue(), width, height)
            finally:
                if rgb is not None and rgb is not original and rgb is not oriented:
                    rgb.close()
                if oriented is not original:
                    oriented.close()
    except PortadaNoDisponible:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise PortadaNoDisponible('Instagram no entregó una imagen de portada válida.') from error


def descargar_portada(item: dict, obtener: Callable[[str], bytes]) -> PortadaJPG:
    candidates = candidatas_portada(item)
    if not candidates:
        raise PortadaNoDisponible('Instagram no entregó una imagen de portada para esta historia.')
    main = [c for c in candidates if c.principal]
    thumbnails = [c for c in candidates if not c.principal]
    best = None
    last_error = None

    def inspect(candidate):
        nonlocal best, last_error
        try:
            image = preparar_jpg(obtener(candidate.url))
        except PortadaNoDisponible as error:
            last_error = str(error)
        except Exception:
            last_error = 'No se pudo descargar la imagen de portada.'
        else:
            if best is None or image.area > best.area:
                best = image

    for candidate in sorted((c for c in main if not c.area), key=lambda c: c.prioridad, reverse=True):
        inspect(candidate)
    for candidate in sorted((c for c in main if c.area), key=lambda c: (c.area, c.prioridad), reverse=True):
        if best is not None and candidate.area <= best.area:
            break
        inspect(candidate)
    for candidate in sorted(thumbnails, key=lambda c: c.area, reverse=True):
        if best is None or candidate.area > best.area:
            inspect(candidate)
    if best is None:
        raise PortadaNoDisponible(last_error or 'No hay una imagen de portada utilizable.')
    return best

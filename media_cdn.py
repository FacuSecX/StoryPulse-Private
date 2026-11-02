# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Reintentos moderados para archivos CDN, sin cambiar de sesión ni URL."""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

MAX_REINTENTOS = 3
ESPERAS_SEGUNDOS = (15, 30, 30)
MAX_ESPERA_POR_REINTENTO = 30
MAX_ESPERA_TOTAL = 90


class ErrorLimiteCDN(RuntimeError):
    """Límite de descarga, independiente de la validez de las cookies."""

    def __init__(self, intentos: int, espera_servidor: int | None = None):
        self.intentos = intentos
        self.espera_servidor = espera_servidor
        detalle = (
            f" El servidor solicita esperar {espera_servidor} segundos."
            if espera_servidor is not None else ""
        )
        super().__init__(
            f"El CDN mantiene el límite HTTP 429 tras {intentos} intento(s)."
            f"{detalle} Lo ya descargado se conserva; quedan archivos pendientes."
        )


def _fecha_http(value: str) -> datetime | None:
    try:
        fecha = parsedate_to_datetime(value)
        return fecha if fecha.tzinfo else fecha.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _retry_after(headers: dict[str, str]) -> int | None:
    normalizados = {str(key).lower(): str(value) for key, value in headers.items()}
    valor = normalizados.get("retry-after", "").strip()
    if valor.isascii() and valor.isdigit():
        return int(valor) if len(valor) <= 12 else MAX_ESPERA_TOTAL + 1
    fecha = _fecha_http(valor)
    if fecha is None:
        return None
    referencia = _fecha_http(normalizados.get("date", "")) or datetime.now(timezone.utc)
    return max(0, math.ceil((fecha - referencia).total_seconds()))


def solicitar_archivo_cdn(
    request: Any,
    url: str,
    *,
    headers: dict[str, str],
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
):
    """Devuelve una respuesta que el llamador debe liberar con ``dispose``.

    Sólo se reintenta HTTP 429. Los demás estados conservan su tratamiento
    anterior. Una espera del servidor que exceda el presupuesto detiene el
    trabajo en lugar de reintentar antes de tiempo o rotar credenciales.
    """
    def reportar(datos: dict[str, Any]) -> None:
        if progress_callback is not None:
            try:
                progress_callback(datos)
            except Exception:
                pass

    esperados = 0
    for numero in range(MAX_REINTENTOS + 1):
        respuesta = request.get(
            url, headers=headers, timeout=60_000, fail_on_status_code=False,
        )
        if int(respuesta.status) != 429:
            if numero:
                reportar({"etapa": "procesando", "espera_cdn_hasta": 0})
            return respuesta

        try:
            espera_servidor = _retry_after(respuesta.headers)
        finally:
            respuesta.dispose()
        if numero == MAX_REINTENTOS:
            raise ErrorLimiteCDN(numero + 1, espera_servidor)
        espera = max(ESPERAS_SEGUNDOS[numero], espera_servidor or 0)
        if espera > MAX_ESPERA_POR_REINTENTO or esperados + espera > MAX_ESPERA_TOTAL:
            raise ErrorLimiteCDN(numero + 1, espera_servidor)
        reportar({
            "etapa": "esperando_cdn",
            "espera_cdn_hasta": time.monotonic() + espera,
            "reintento_cdn": numero + 1,
            "max_reintentos_cdn": MAX_REINTENTOS,
        })
        restante = espera
        while restante:
            bloque = min(30, restante)
            time.sleep(bloque)
            restante -= bloque
        esperados += espera


def aviso_espera_cdn(estado: dict[str, Any]) -> str:
    if estado.get("etapa") != "esperando_cdn":
        return ""
    segundos = max(0, math.ceil(float(estado.get("espera_cdn_hasta") or 0) - time.monotonic()))
    reintento = int(estado.get("reintento_cdn") or 0)
    maximo = int(estado.get("max_reintentos_cdn") or MAX_REINTENTOS)
    return f"🚦 Límite del CDN: reintento {reintento}/{maximo} en {segundos} s.\n"

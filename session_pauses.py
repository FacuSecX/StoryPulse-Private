# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Política de pausas explícitas, independiente de cookies y errores de Instagram."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


def ahora_utc() -> datetime:
    return datetime.now(timezone.utc)


def fecha_utc(valor: str) -> datetime:
    if not isinstance(valor, str):
        raise ValueError('La fecha de reanudación debe ser un texto ISO con zona horaria.')
    try:
        fecha = datetime.fromisoformat(valor.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('Fecha de reanudación inválida.') from None
    if fecha.tzinfo is None or fecha.utcoffset() is None:
        raise ValueError('La fecha de reanudación necesita zona horaria.')
    return fecha.astimezone(timezone.utc)


def validar_pausas(valor: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(valor, dict):
        raise ValueError('El registro de pausas debe ser un objeto.')
    for identificador, fila in valor.items():
        if not isinstance(identificador, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', identificador):
            raise ValueError('El registro de pausas contiene un ID inválido.')
        if not isinstance(fila, dict) or fila.get('pausada') is not True:
            raise ValueError('El registro contiene una pausa inválida.')
        fecha_utc(fila.get('pausada_en'))
        if fila.get('reanudar_en') is not None:
            fecha_utc(fila['reanudar_en'])
    return valor


def pausa_vigente(pausas: dict, identificador: str, *, ahora: datetime | None = None) -> dict | None:
    fila = validar_pausas(pausas).get(identificador)
    if fila is None:
        return None
    ahora = ahora or ahora_utc()
    if fila.get('reanudar_en') is not None and fecha_utc(fila['reanudar_en']) <= ahora:
        return None
    return dict(fila)


def crear_pausa(hasta: datetime | None = None, *, ahora: datetime | None = None) -> dict:
    ahora = ahora or ahora_utc()
    if hasta is not None:
        if not isinstance(hasta, datetime) or hasta.tzinfo is None or hasta.utcoffset() is None:
            raise ValueError('La reanudación necesita fecha y zona horaria.')
        hasta = hasta.astimezone(timezone.utc)
        if hasta <= ahora:
            raise ValueError('La fecha de reanudación debe estar en el futuro.')
    return {'pausada': True, 'pausada_en': ahora.isoformat(),
            'reanudar_en': hasta.isoformat() if hasta is not None else None}


def vencidas(pausas: dict, *, ahora: datetime | None = None) -> list[str]:
    ahora = ahora or ahora_utc()
    return [identificador for identificador, fila in validar_pausas(pausas).items()
            if fila.get('reanudar_en') is not None and fecha_utc(fila['reanudar_en']) <= ahora]

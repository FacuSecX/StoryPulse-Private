# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Detención cooperativa de un lote, sin cancelar navegadores ni envíos activos."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import asyncio
import threading
import uuid


class LoteDetenido(RuntimeError):
    pass


@dataclass
class ControlLote:
    chat_id: int
    tipo: str
    titulo: str
    total: int
    grupo_id: int | None = None
    token: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    detener: threading.Event = field(default_factory=threading.Event)
    edicion: asyncio.Lock = field(default_factory=asyncio.Lock)


_ACTUAL = ContextVar('lote_historias_actual', default=None)


@contextmanager
def usar_lote(control):
    token = _ACTUAL.set(control)
    try:
        yield
    finally:
        _ACTUAL.reset(token)


def comprobar_lote_no_detenido():
    control = _ACTUAL.get()
    if control is not None and control.detener.is_set():
        raise LoteDetenido('Se detuvo el lote antes de iniciar el siguiente perfil.')

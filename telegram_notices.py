# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Borrado diferido de avisos de texto; nunca agenda multimedia ni controles."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError
import database as db

logger = logging.getLogger('storypulse-web')
AVISO_TTL_SEGUNDOS = 60
PREFIX = 'aviso_temporal:'
MAX_REINTENTOS = 2


def _queue(context):
    application = getattr(context, 'application', None)
    return getattr(application, 'job_queue', None) or getattr(context, 'job_queue', None)


def cancelar_borrado_aviso(context, chat_id, message_id: int | None = None) -> None:
    if message_id is None:
        mensaje = chat_id
        if mensaje is None:
            return
        chat_id, message_id = mensaje.chat.id, mensaje.message_id
    queue = _queue(context)
    buscar = getattr(queue, 'get_jobs_by_name', None)
    if not callable(buscar):
        return
    try:
        for job in buscar(f'{PREFIX}{int(chat_id)}:{int(message_id)}'):
            job.schedule_removal()
    except Exception:
        logger.exception('No se pudo cancelar el borrado del aviso message_id=%s.', message_id)


def programar_borrado_aviso(context, mensaje, *, segundos=AVISO_TTL_SEGUNDOS,
                          controles_retirados: bool = False) -> bool:
    if not isinstance(getattr(mensaje, 'text', None), str):
        return False
    if getattr(mensaje, 'effective_attachment', None) is not None:
        return False
    if not controles_retirados and getattr(mensaje, 'reply_markup', None) is not None:
        return False
    queue = _queue(context)
    if queue is None:
        return False
    chat_id, message_id = int(mensaje.chat.id), int(mensaje.message_id)
    try:
        cancelar_borrado_aviso(context, chat_id, message_id)
        queue.run_once(
            borrar_aviso_programado, when=max(1, int(segundos)),
            data={'chat_id': chat_id, 'message_id': message_id, 'intento': 0},
            name=f'{PREFIX}{chat_id}:{message_id}',
        )
    except Exception:
        logger.exception('No se pudo programar el borrado del aviso message_id=%s.', message_id)
        return False
    return True


async def borrar_aviso_programado(context) -> None:
    data = dict(context.job.data)
    chat_id, message_id = int(data['chat_id']), int(data['message_id'])
    try:
        protegidos = await asyncio.to_thread(db.ids_multimedia_protegida_chat, chat_id)
        if message_id in protegidos:
            return
        borrado = await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        if not borrado:
            raise NetworkError('Telegram no confirmó el borrado del aviso.')
    except BadRequest as error:
        if not any(texto in str(error).lower() for texto in (
            'message to delete not found', 'message not found',
        )):
            logger.warning('No se pudo borrar el aviso message_id=%s: %s', message_id, error)
            return
    except (RetryAfter, NetworkError) as error:
        intento = int(data.get('intento', 0))
        if intento >= MAX_REINTENTOS:
            logger.warning('Borrado automático pendiente message_id=%s tras reintentos.', message_id)
            return
        if isinstance(error, RetryAfter):
            espera = error.retry_after
            espera = espera.total_seconds() if isinstance(espera, timedelta) else float(espera)
            espera = max(1.0, espera) + 0.1
        else:
            espera = 5 * (intento + 1)
        data['intento'] = intento + 1
        queue = _queue(context)
        if queue is not None:
            queue.run_once(borrar_aviso_programado, when=espera, data=data,
                           name=f'{PREFIX}{chat_id}:{message_id}')
        return
    except TelegramError as error:
        logger.warning('No se pudo borrar el aviso message_id=%s: %s', message_id, error)
        return
    except Exception:
        logger.exception('Fallo preparando el borrado del aviso message_id=%s.', message_id)
        return
    try:
        await asyncio.to_thread(db.eliminar_registros_chat_limpiables, chat_id, [message_id])
    except Exception:
        logger.exception('No se pudo retirar el registro del aviso borrado message_id=%s.', message_id)

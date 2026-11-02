# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Interfaz de backups. El trabajo pesado pertenece al servicio independiente.

El bot únicamente lee estado y entrega solicitudes sin credenciales al spool.
Sus callbacks y textos deben despacharse después de la autorización del bot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError

STATUS_FILE = Path('/var/lib/storypulse-backups/status.json')
REQUEST_SPOOL = Path('/var/spool/storypulse-backups')
NOTICES_FILE = Path(__file__).resolve().parent / '.backup-notices.json'
BACKUP_STATE = 'backup_state'
RUNTIME_KEY = 'storypulse_backups'
JOB_NAME = 'storypulse-backups-status'
POLL_SECONDS = 5
logger = logging.getLogger(__name__)


class BackendUnavailable(OSError):
    pass


def _read_json(path: Path) -> dict | None:
    """No seguir symlinks ni leer archivos arbitrariamente grandes."""
    try:
        if path.is_symlink():
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 131_072:
                return None
            value = json.load(stream)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _read_status() -> dict | None:
    value = _read_json(STATUS_FILE)
    if value is None or value.get('state') not in {'idle', 'running', 'error', 'success'}:
        return None
    return value


def _write_request(payload: dict) -> None:
    """Publicar un JSON completo en un directorio privado del usuario del bot."""
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW'):
        raise BackendUnavailable('Backend disponible únicamente en el servidor.')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(REQUEST_SPOOL, flags)
    temporary = '.' + payload['id'] + '.tmp'
    try:
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise BackendUnavailable('Directorio de solicitudes inseguro.')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=True, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, payload['id'] + '.json',
                      src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory)


def _request(action: str, **values) -> str:
    request_id = uuid.uuid4().hex
    _write_request({'id': request_id, 'action': action, **values})
    return request_id


def _load_notice(chat_id: int) -> str | None:
    try:
        info = NOTICES_FILE.lstat()
        if not stat.S_ISREG(info.st_mode):
            return None
        if os.name == 'posix' and (info.st_uid != os.geteuid()
                                  or stat.S_IMODE(info.st_mode) != 0o600):
            return None
    except OSError:
        return None
    value = _read_json(NOTICES_FILE) or {}
    if value.get('owner_chat_id') != chat_id:
        return None
    run_id = value.get('run_id')
    return run_id if isinstance(run_id, str) and len(run_id) <= 128 else None


def _save_notice(chat_id: int, run_id: str) -> None:
    temporary = NOTICES_FILE.with_name('.backup-notices.' + uuid.uuid4().hex + '.tmp')
    if NOTICES_FILE.exists() or NOTICES_FILE.is_symlink():
        info = NOTICES_FILE.lstat()
        if not stat.S_ISREG(info.st_mode) or (os.name == 'posix' and info.st_uid != os.geteuid()):
            raise OSError('Archivo de avisos inseguro.')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'owner_chat_id': chat_id, 'run_id': run_id}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, NOTICES_FILE)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _runtime(context) -> dict:
    return context.application.bot_data[RUNTIME_KEY]


def _buttons(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=data) for label, data in row]
        for row in rows
    ])


def _download_url(status: dict | None) -> str | None:
    """Only a validated MEGA file link from the last completed backup is usable."""
    if not isinstance(status, dict):
        return None
    last = status.get('last_success')
    if not isinstance(last, dict):
        return None
    value = last.get('download_url')
    if not isinstance(value, str):
        return None
    if re.fullmatch(r'https://mega\.nz/file/[A-Za-z0-9_-]{8}#[A-Za-z0-9_-]{43}', value):
        return value
    return None


def menu(status: dict | None = None) -> InlineKeyboardMarkup:
    keyboard = _buttons([
        [('▶ Crear backup ahora', 'backups_run')],
        [('🕒 Programar por intervalo', 'backups_interval')],
        [('📅 Definir fechas', 'backups_dates')],
        [('📋 Ver estado y programación', 'backups_status')],
        [('🗑 Borrar programación de backups', 'backups_clear')],
        [('⬅ Menú principal', 'menu')],
    ])
    link = _download_url(status)
    download = (InlineKeyboardButton('⬇ Descargar último Backup', url=link) if link
                else InlineKeyboardButton('⬇ Descargar último Backup', callback_data='backups_download'))
    rows = list(keyboard.inline_keyboard)
    rows.insert(1, [download])
    return InlineKeyboardMarkup(rows)


def _back() -> InlineKeyboardMarkup:
    return _buttons([[('⬅ Backups', 'backups_menu')]])


def _date(value, tz) -> str:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return 'no disponible'
        return parsed.astimezone(tz).strftime('%d/%m/%Y %H:%M')
    except (TypeError, ValueError, OverflowError):
        return 'no disponible'


def _zone(tz) -> str:
    return str(getattr(tz, 'key', tz))


def _filename(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_. -]{0,179}', value):
        return value
    return None


def _progress(status: dict, tz) -> str:
    try:
        percent = max(0, min(100, int(status.get('progress') or 0)))
    except (ValueError, TypeError):
        percent = 0
    stages = {
        'prepare': 'Preparando', 'preparing': 'Preparando', 'snapshot': 'Copiando archivos',
        'copying': 'Copiando archivos', 'copy': 'Copiando archivos',
        'sizing': 'Calculando tamaño', 'creating': 'Creando contenedor',
        'container': 'Creando contenedor', 'formatting': 'Preparando contenedor',
        'encrypt': 'Copiando y verificando historys en el contenedor cifrado',
        'encrypting': 'Copiando y verificando historys en el contenedor cifrado',
        'mounting': 'Abriendo contenedor',
        'unmounting': 'Cerrando contenedor', 'uploading': 'Subiendo a MEGA',
        'upload': 'Subiendo a MEGA', 'verify': 'Verificando', 'verifying': 'Verificando',
        'link': 'Preparando enlace de descarga',
        'retention': 'Revisando copias anteriores', 'cleanup': 'Finalizando',
        'complete': 'Completado', 'completed': 'Completado',
    }
    stage = stages.get(status.get('stage'), 'Procesando backup')
    text = f'⏳ Backup en curso · {percent}%\n{stage}'
    if status.get('started_at'):
        text += '\nInicio: ' + _date(status['started_at'], tz)
    return text


def _finished(status: dict, tz) -> str:
    if status.get('state') != 'success':
        safe_errors = {
            'No hay espacio local suficiente para crear la copia.':
                '❌ No hay espacio libre suficiente en el servidor para crear el backup.',
            'No hay espacio suficiente en MEGA; la copia anterior se conserva.':
                '❌ No hay espacio libre suficiente en MEGA. Se conserva el backup anterior.',
            'El origen cambió durante la copia.':
                '❌ La carpeta historys cambió durante el backup. Intentá nuevamente.',
            'El origen contiene enlaces o archivos especiales; revise historys.':
                '❌ Hay enlaces o archivos especiales dentro de historys que requieren revisión.',
            'MEGA no confirmó el archivo completo; se conserva la copia anterior.':
                '❌ MEGA no confirmó el archivo completo. Se conserva el backup anterior.',
            'No se pudo verificar la copia remota; se conserva la copia anterior.':
                '❌ No se pudo verificar el archivo en MEGA. Se conserva el backup anterior.',
            'No se pudo preparar el enlace de descarga; se conserva la copia anterior.':
                '❌ No se pudo preparar el enlace de descarga en MEGA. Se conserva el backup anterior.',
            'La copia nueva está verificada; falta completar la limpieza de copias anteriores.':
                '⚠️ El backup nuevo está verificado en MEGA. Falta limpiar copias anteriores.',
        }
        message = status.get('message')
        return safe_errors.get(message if isinstance(message, str) else '',
                               '❌ No se pudo completar el backup. Revisá el servicio de backups en el servidor.')
    text = '✅ Backup completado y subido a MEGA.'
    name = _filename(status.get('filename'))
    if name:
        text += '\nArchivo: ' + name
    if status.get('finished_at'):
        text += '\nFinalizado: ' + _date(status['finished_at'], tz)
    return text


def _status_text(status: dict | None, tz) -> str:
    if status is None:
        return ('💾 Backups\n\nEl servicio de backups no está disponible en este equipo. '
                'La ejecución y la programación se administran en el servidor.')
    state = status.get('state')
    if state == 'running':
        text = _progress(status, tz)
    elif state in {'success', 'error'}:
        text = _finished(status, tz)
    else:
        text = '💾 Backups\n\nSin backups en curso.'
    schedule = status.get('schedule') or {}
    if not isinstance(schedule, dict):
        schedule = {}
    text += '\n\nProgramación de backups:'
    if not schedule.get('enabled'):
        text += '\nDesactivada.'
    else:
        hours = schedule.get('interval_hours')
        if isinstance(hours, int) and 1 <= hours <= 8760:
            text += f'\nIntervalo: cada {hours} hora(s).'
        dates = schedule.get('dates')
        if isinstance(dates, list):
            for value in dates[:50]:
                text += '\n• ' + _date(value, tz)
        if schedule.get('next_run_at'):
            text += '\nPróximo backup: ' + _date(schedule['next_run_at'], tz)
    last = status.get('last_success') or {}
    if isinstance(last, dict) and last.get('at'):
        text += '\n\nÚltimo backup correcto: ' + _date(last['at'], tz)
        name = _filename(last.get('filename'))
        if name:
            text += '\n' + name
        size = last.get('bytes')
        if type(size) is int and size > 0:
            text += f'\nTamaño: {size / (1024 ** 3):.2f} GiB'
    return text + '\n\nZona horaria: ' + _zone(tz)


def parse_dates(text: str, tz, now: datetime | None = None) -> list[str]:
    entries = [part.strip() for part in re.split(r'[\n;,]+', text) if part.strip()]
    if not entries or len(entries) > 50:
        raise ValueError('Ingresá entre 1 y 50 fechas, una por línea.')
    now = now or datetime.now(timezone.utc)
    result = set()
    for entry in entries:
        if not re.fullmatch(r'\d{2}/\d{2}/\d{4} \d{2}:\d{2}', entry):
            raise ValueError('Usá DD/MM/AAAA HH:MM, una fecha por línea.')
        try:
            naive = datetime.strptime(entry, '%d/%m/%Y %H:%M')
            local = naive.replace(tzinfo=tz)
            if local.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
                raise ValueError('La hora no existe en la zona horaria configurada.')
            if local <= now:
                raise ValueError('Todas las fechas deben ser futuras.')
        except ValueError as error:
            if str(error) in {'Todas las fechas deben ser futuras.',
                              'La hora no existe en la zona horaria configurada.'}:
                raise
            raise ValueError('Hay una fecha inválida. Usá DD/MM/AAAA HH:MM.') from None
        result.add(local.isoformat())
    return sorted(result, key=lambda value: datetime.fromisoformat(value).timestamp())


def _schedule_values(status: dict | None) -> dict:
    schedule = (status or {}).get('schedule') or {}
    if not isinstance(schedule, dict):
        schedule = {}
    hours = schedule.get('interval_hours')
    hours = hours if type(hours) is int and 1 <= hours <= 8760 else None
    dates = []
    for value in schedule.get('dates', []) if isinstance(schedule.get('dates'), list) else []:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None and parsed > datetime.now(timezone.utc):
                dates.append(parsed.isoformat())
        except (TypeError, ValueError, OverflowError):
            pass
    return {'interval_hours': hours, 'dates': dates[:50], 'enabled': True}


async def _edit(query, text: str, reply_markup=None) -> None:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as error:
        if 'message is not modified' not in str(error).lower():
            raise


async def _submit(action: str, **values) -> str | None:
    try:
        return await asyncio.to_thread(_request, action, **values)
    except (OSError, ValueError):
        logger.warning('No se pudo entregar una solicitud al servicio de backups.')
        return None


async def callback(context, query, data: str) -> bool:
    if not data.startswith('backups_'):
        return False
    runtime = _runtime(context)
    navigation_generation = _generation(runtime, query)
    tz = runtime['tz']
    user_data = context.user_data
    if data in {'backups_menu', 'backups_status'}:
        if data == 'backups_menu':
            user_data.clear()
        else:
            user_data.pop(BACKUP_STATE, None)
        status = await asyncio.to_thread(_read_status)
        await _edit(query, _status_text(status, tz), menu(status))
        _remember_download_menu(runtime, query, status, navigation_generation)
    elif data == 'backups_download':
        status = await asyncio.to_thread(_read_status)
        if _download_url(status):
            text = ('⬇ Último backup disponible\n\n'
                    'Tocá «Descargar último Backup» para abrir su página de descarga en MEGA.')
        else:
            text = ('Todavía no hay un enlace de descarga disponible para el último backup.\n\n'
                    'La copia anterior se conserva si una nueva no termina correctamente. '
                    'Volvé a consultar el estado después de que se complete un backup.')
        await _edit(query, text, menu(status))
        _remember_download_menu(runtime, query, status, navigation_generation)
    elif data == 'backups_run':
        status = await asyncio.to_thread(_read_status)
        if status and status.get('state') == 'running':
            await _edit(query, _progress(status, tz), _back())
        else:
            token = uuid.uuid4().hex[:12]
            user_data[BACKUP_STATE] = {'mode': 'confirm_run', 'token': token}
            await _edit(query, '¿Crear un backup cifrado y subirlo a MEGA ahora?',
                        _buttons([[('✅ Sí, crear backup', 'backups_run_confirm:' + token)],
                                  [('Cancelar', 'backups_menu')]]))
    elif data.startswith('backups_run_confirm:'):
        state = user_data.get(BACKUP_STATE) or {}
        if state != {'mode': 'confirm_run', 'token': data.split(':', 1)[1]}:
            await _edit(query, 'La confirmación venció. Volvé a elegir Crear backup ahora.', _back())
            return True
        user_data.pop(BACKUP_STATE, None)
        request_id = await _submit('run')
        if request_id:
            message = query.message
            runtime['pending'][request_id] = {
                'chat_id': runtime['chat_id'], 'message_id': message.message_id,
                'last_text': '⏳ Solicitud recibida. Esperando al servicio de backups.',
            }
            if navigation_generation is not None:
                runtime['pending'][request_id]['navigation_generation'] = navigation_generation
            runtime['acks'][request_id] = {**runtime['pending'][request_id], 'action': 'run'}
            await _edit(query, runtime['pending'][request_id]['last_text'], _back())
        else:
            await _edit(query, 'No se pudo iniciar el backup. El servicio no está disponible.', _back())
    elif data == 'backups_interval':
        user_data[BACKUP_STATE] = {'mode': 'interval'}
        await _edit(query, '¿Cada cuántas horas querés un backup?\n'
                    'Enviá un número entero entre 1 y 8760. Se conservarán las fechas puntuales.', _back())
    elif data == 'backups_dates':
        user_data[BACKUP_STATE] = {'mode': 'dates'}
        await _edit(query, 'Enviá hasta 50 fechas futuras, una por línea:\n'
                    'DD/MM/AAAA HH:MM\n\nZona horaria: ' + _zone(tz)
                    + '\nSe reemplazarán las fechas puntuales y se conservará el intervalo.', _back())
    elif data == 'backups_clear':
        token = uuid.uuid4().hex[:12]
        user_data[BACKUP_STATE] = {'mode': 'confirm_clear', 'token': token}
        await _edit(query, '¿Borrar el intervalo y todas las fechas de backups?',
                    _buttons([[('🗑 Sí, borrar programación', 'backups_clear_confirm:' + token)],
                              [('Cancelar', 'backups_menu')]]))
    elif data.startswith('backups_clear_confirm:'):
        state = user_data.get(BACKUP_STATE) or {}
        if state != {'mode': 'confirm_clear', 'token': data.split(':', 1)[1]}:
            await _edit(query, 'La confirmación venció. Volvé a elegir Borrar programación.', _back())
            return True
        user_data.pop(BACKUP_STATE, None)
        request_id = await _submit('clear_schedule')
        text = ('⏳ Se solicitó borrar la programación de backups. Consultá el estado para verificarlo.'
                if request_id else 'No se pudo borrar la programación. El servicio no está disponible.')
        await _edit(query, text, _back())
        if request_id:
            runtime['acks'][request_id] = {'chat_id': runtime['chat_id'],
                                          'message_id': query.message.message_id,
                                          'action': 'clear_schedule', 'last_text': text}
            if navigation_generation is not None:
                runtime['acks'][request_id]['navigation_generation'] = navigation_generation
    else:
        await _edit(query, 'Esta opción venció. Abrí nuevamente Backups.', _back())
    return True


async def recibir_texto(update, context, texto: str) -> bool:
    state = context.user_data.get(BACKUP_STATE) or {}
    mode = state.get('mode')
    if mode not in {'interval', 'dates'}:
        return False
    runtime = _runtime(context)
    navigation_generation = _generation(runtime)
    try:
        if mode == 'interval':
            if not re.fullmatch(r'[0-9]{1,4}', texto.strip()):
                raise ValueError('Enviá un número entero de horas entre 1 y 8760.')
            hours = int(texto)
            if not 1 <= hours <= 8760:
                raise ValueError('El intervalo debe estar entre 1 y 8760 horas.')
        else:
            dates = parse_dates(texto, runtime['tz'])
    except ValueError as error:
        await _reply(update, context, str(error), reply_markup=_back())
        return True
    status = await asyncio.to_thread(_read_status)
    if status is None:
        await _reply(update, context, 'El servicio de backups no está disponible. Probá desde el servidor.',
                     reply_markup=_back())
        return True
    values = _schedule_values(status)
    values['interval_hours' if mode == 'interval' else 'dates'] = hours if mode == 'interval' else dates
    request_id = await _submit('schedule', **values)
    if request_id:
        context.user_data.pop(BACKUP_STATE, None)
    text = ('⏳ Programación enviada. Consultá el estado para verificar que se haya guardado.'
            if request_id else 'No se pudo guardar la programación. Intentá nuevamente.')
    message = await _reply(update, context, text, reply_markup=_back())
    if request_id and message is not None:
        runtime['acks'][request_id] = {'chat_id': runtime['chat_id'], 'message_id': message.message_id,
                                      'action': 'schedule', 'last_text': text}
        if navigation_generation is not None:
            runtime['acks'][request_id]['navigation_generation'] = navigation_generation
    return True


def configurar_navegacion(application, *, responder_texto, is_active, enviar_aviso,
                         actualizar_menu=None, navigation_generation=None) -> None:
    """Conectar sólo la presentación; no altera solicitudes, cifrado ni retención."""
    if not all(callable(value) for value in (responder_texto, is_active, enviar_aviso)):
        raise TypeError('Los adaptadores de navegación deben ser funciones.')
    if actualizar_menu is not None and not callable(actualizar_menu):
        raise TypeError('El adaptador de progreso debe ser una función.')
    if navigation_generation is not None and not callable(navigation_generation):
        raise TypeError('El adaptador de versión debe ser una función.')
    runtime = application.bot_data[RUNTIME_KEY]
    runtime['navigation_reply'] = responder_texto
    runtime['navigation_is_active'] = is_active
    runtime['navigation_notice'] = enviar_aviso
    runtime['navigation_edit_active'] = actualizar_menu
    runtime['navigation_generation'] = navigation_generation


def _generation(runtime, view=None):
    """Capturar la pantalla antes de esperar respuestas de otros servicios."""
    value = getattr(view, 'generation', None)
    if not isinstance(value, int) or isinstance(value, bool):
        getter = runtime.get('navigation_generation')
        value = getter(runtime['chat_id']) if callable(getter) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


async def _reply(update, context, text: str, **options):
    presenter = _runtime(context).get('navigation_reply')
    if callable(presenter):
        return await presenter(update, context, text, **options)
    return await update.effective_message.reply_text(text, **options)


async def _notice(context, text: str, **options):
    """Los avisos de trabajos no abren otro menú ni reemplazan el actual."""
    runtime = _runtime(context)
    sender = runtime.get('navigation_notice')
    if callable(sender):
        return await sender(context, text, **options)
    return await context.application.bot.send_message(
        chat_id=runtime['chat_id'], text=text, reply_markup=_back(), **options,
    )


def _remember_download_menu(runtime, query, status, generation) -> None:
    """Remember only the screen that was actually rendered by this interaction."""
    message_id = getattr(getattr(query, 'message', None), 'message_id', None)
    if not isinstance(generation, int) or isinstance(generation, bool):
        return
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        return
    current = runtime.get('navigation_generation')
    if callable(current) and current(runtime['chat_id']) != generation:
        return
    previous = runtime.get('download_menu')
    if isinstance(previous, dict) and previous.get('generation', -1) > generation:
        return
    runtime['download_menu'] = {
        'chat_id': runtime['chat_id'], 'message_id': message_id,
        'generation': generation,
        'signature': (_status_text(status, runtime['tz']), _download_url(status)),
        'dirty': False,
    }


async def _refresh_download_menu(application, status) -> None:
    """Replace the URL only in the current Backups screen, never an older menu."""
    runtime = application.bot_data.get(RUNTIME_KEY, {})
    watch = runtime.get('download_menu')
    editor = runtime.get('navigation_edit_active')
    if not isinstance(watch, dict) or not callable(editor):
        return
    signature = (_status_text(status, runtime['tz']), _download_url(status))
    if not watch.get('dirty') and watch.get('signature') == signature:
        return
    try:
        updated = await editor(watch['chat_id'], watch['message_id'], signature[0],
                               expected_generation=watch['generation'], reply_markup=menu(status))
    except TelegramError:
        return
    if runtime.get('download_menu') is not watch:
        return
    if updated:
        watch['signature'], watch['dirty'] = signature, False
    else:
        runtime.pop('download_menu', None)


async def _edit_progress(application, watch: dict, text: str) -> None:
    if watch.get('last_text') == text:
        return
    runtime = application.bot_data.get(RUNTIME_KEY, {})
    active = runtime.get('navigation_is_active')
    editor = runtime.get('navigation_edit_active')
    download_menu = runtime.get('download_menu')
    if (isinstance(download_menu, dict)
            and download_menu.get('message_id') == watch.get('message_id')
            and download_menu.get('generation') == watch.get('navigation_generation')):
        download_menu['dirty'] = True
    if callable(editor) and watch.get('navigation', True):
        generation = watch.get('navigation_generation')
        if not isinstance(generation, int) or isinstance(generation, bool):
            watch['last_text'] = text
            return
        try:
            await editor(watch['chat_id'], watch['message_id'], text,
                         expected_generation=generation, reply_markup=_back())
            watch['last_text'] = text
        except TelegramError:
            pass
        return
    if callable(active) and watch.get('navigation', True):
        if not active(watch['chat_id'], watch['message_id']):
            watch['last_text'] = text
            return
    try:
        await application.bot.edit_message_text(chat_id=watch['chat_id'],
                                               message_id=watch['message_id'], text=text,
                                               reply_markup=(_back() if not callable(active)
                                                             or watch.get('navigation', True) else None))
        watch['last_text'] = text
    except TelegramError:
        pass


async def comprobar_estado(context) -> None:
    application = context.application
    runtime = application.bot_data[RUNTIME_KEY]
    async with runtime['lock']:
        status = await asyncio.to_thread(_read_status)
        if status is None:
            return
        try:
            ack_id = status.get('last_request_id')
            ack = runtime['acks'].get(ack_id) if isinstance(ack_id, str) else None
            result = status.get('last_request_result')
            if ack and result in {'accepted', 'busy', 'applied'}:
                if ack['action'] == 'run' and result == 'busy':
                    await _edit_progress(application, ack, '⏳ Ya hay un backup en curso. No se inició otro.')
                    runtime['pending'].pop(ack_id, None)
                elif result == 'applied' and ack['action'] in {'schedule', 'clear_schedule'}:
                    text = ('✅ Programación de backups guardada.' if ack['action'] == 'schedule'
                            else '✅ Programación de backups eliminada.')
                    await _edit_progress(application, ack, text)
                runtime['acks'].pop(ack_id, None)
            run_id = status.get('run_id')
            if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
                return
            state = status.get('state')
            watch = runtime.get('watch')
            if state == 'running':
                if not watch or watch.get('run_id') != run_id:
                    watch = runtime['pending'].pop(status.get('request_id'), None)
                    if watch is None:
                        try:
                            message = await _notice(context, _progress(status, runtime['tz']))
                        except TelegramError:
                            return
                        watch = {'chat_id': runtime['chat_id'], 'message_id': message.message_id,
                                 'navigation': not callable(runtime.get('navigation_notice')),
                                 'last_text': _progress(status, runtime['tz'])}
                    watch['run_id'] = run_id
                    runtime['watch'] = watch
                await _edit_progress(application, watch, _progress(status, runtime['tz']))
            elif state in {'success', 'error'} and run_id != runtime['last_notified']:
                text = _finished(status, runtime['tz'])
                if not watch or watch.get('run_id') != run_id:
                    watch = runtime['pending'].pop(status.get('request_id'), None)
                if watch and watch.get('run_id') == run_id:
                    await _edit_progress(application, watch, text)
                elif watch and 'run_id' not in watch:
                    await _edit_progress(application, watch, text)
                try:
                    await _notice(context, text, disable_notification=False)
                except TelegramError:
                    return
                runtime['last_notified'] = run_id
                runtime['watch'] = None
                runtime['pending'].pop(status.get('request_id'), None)
                try:
                    await asyncio.to_thread(_save_notice, runtime['chat_id'], run_id)
                except OSError:
                    logger.warning('No se pudo guardar la deduplicación del aviso de backups.')
        finally:
            await _refresh_download_menu(application, status)


async def iniciar(application, authorized_chat_id: int, tz) -> None:
    """Instalar sólo el monitor propio; jamás modificar jobs de Instagram."""
    chat_id = int(authorized_chat_id)
    saved = await asyncio.to_thread(_load_notice, chat_id)
    status = await asyncio.to_thread(_read_status)
    if status and status.get('state') in {'success', 'error'}:
        saved = status.get('run_id') or saved
    application.bot_data[RUNTIME_KEY] = {
        'chat_id': chat_id, 'tz': tz, 'last_notified': saved,
        'watch': None, 'pending': {}, 'acks': {}, 'lock': asyncio.Lock(),
    }
    queue = getattr(application, 'job_queue', None)
    if queue is None:
        logger.warning('El monitor de backups requiere JobQueue.')
        return
    for job in queue.get_jobs_by_name(JOB_NAME):
        job.schedule_removal()
    queue.run_repeating(comprobar_estado, interval=POLL_SECONDS, first=POLL_SECONDS, name=JOB_NAME)

# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""One navigation surface per chat, independent from form state and work controls.

The view is a presentation port shared by typed commands and callbacks. It is
not a manufactured Telegram Update/CallbackQuery. Telegram/network operations
are injected through the bot so this module can be exercised without a token.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import stat
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger('storypulse-navigation')
_CURRENT: ContextVar[tuple | None] = ContextVar('storypulse_navigation_view', default=None)


class StaleMenu(ValueError):
    """A retired navigation keyboard must not execute another operation."""


def control_markup(markup) -> bool:
    """Progress STOP controls belong to their job, never to navigation."""
    rows = getattr(markup, 'inline_keyboard', ()) or ()
    return any(str(getattr(button, 'callback_data', '') or '').startswith('batch_stop:')
               for row in rows for button in row)


def _message_id(message):
    value = getattr(message, 'message_id', None)
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _has_media(message) -> bool:
    return message is not None and (getattr(message, 'effective_attachment', None) is not None
                                    or not isinstance(getattr(message, 'text', None), str))


@dataclass
class _Chat:
    generation: int = 0
    message_id: int | None = None
    message: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    retired: set[int] = field(default_factory=set)
    controls: dict[int, int] = field(default_factory=dict)
    pending: dict[int, object] = field(default_factory=dict)


class NavigationManager:
    def __init__(self, bot, persist_path=None, *, on_track=None, on_retire=None):
        self.bot = bot
        self.persist_path = Path(persist_path) if persist_path else None
        self.on_track = on_track
        self.on_retire = on_retire
        self._chats: dict[int, _Chat] = {}
        self._load()

    def _chat(self, chat_id):
        return self._chats.setdefault(int(chat_id), _Chat())

    def active_message_id(self, chat_id):
        return self._chat(chat_id).message_id

    def active_generation(self, chat_id):
        return self._chat(chat_id).generation

    def is_active(self, chat_id, message_id):
        return self.active_message_id(chat_id) == int(message_id)

    async def editar_si_activo(self, chat_id, message_id, text, *,
                              expected_generation=None, **options):
        """Atomically update an owned progress screen without taking navigation.

        Long-running watches should retain their creation generation: another
        screen may be edited into the same message ID by a subsequent callback.
        """
        chat_id, message_id = int(chat_id), int(message_id)
        state = self._chat(chat_id)
        async with state.lock:
            if state.message_id != message_id or (expected_generation is not None
                    and state.generation != expected_generation):
                return False
            try:
                result = await self.bot.edit_message_text(chat_id=chat_id,
                    message_id=message_id, text=text, **options)
            except Exception as error:
                if 'message is not modified' not in str(error).casefold():
                    raise
                return True
            if _message_id(result) == message_id:
                state.message = result
                await self._hook(self.on_track, result)
            return True

    def current_view(self, chat_id):
        current = _CURRENT.get()
        if current and current[0] is self and current[1].chat_id == int(chat_id):
            return current[1]
        return None

    async def _hook(self, hook, *args):
        if hook is None:
            return
        try:
            result = hook(*args)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception('No se pudo actualizar el registro de navegación.')

    def _load(self):
        path = self.persist_path
        if path is None:
            return
        try:
            if path.is_symlink():
                return
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
                    return
                value = json.load(stream)
            if not isinstance(value, dict) or value.get('version') != 1:
                return
            chats = value.get('chats')
            if not isinstance(chats, dict) or len(chats) > 64:
                return
            for chat, message_id in chats.items():
                if (isinstance(chat, str) and chat.lstrip('-').isascii()
                        and chat.lstrip('-').isdigit() and len(chat) <= 21
                        and isinstance(message_id, int) and not isinstance(message_id, bool)
                        and 0 < message_id < 2**63):
                    self._chats[int(chat)] = _Chat(message_id=message_id)
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError):
            logger.warning('No se pudo recuperar el último menú de navegación.')

    def _save(self):
        path = self.persist_path
        if path is None:
            return
        temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            if path.is_symlink():
                raise OSError('Navigation state is a symbolic link')
            payload = {'version': 1, 'chats': {str(chat): state.message_id
                       for chat, state in self._chats.items() if state.message_id is not None}}
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, separators=(',', ':'))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            logger.warning('No se pudo guardar el último menú de navegación.')
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    async def begin(self, chat_id, source_message=None, *, callback=None,
                    force_new=False, control=False):
        """Begin an authorized interaction. Caller remains responsible for auth.

        Incoming user messages must use force_new=True; source_message otherwise
        represents a real callback's message. A STOP callback never replaces the
        navigation lease or increments its generation.
        """
        chat_id = int(chat_id)
        state = self._chat(chat_id)
        mid = _message_id(source_message)
        control = bool(control or (mid in state.controls) or
                       control_markup(getattr(source_message, 'reply_markup', None)))
        async with state.lock:
            if control:
                generation = state.controls.get(mid, -1)
            else:
                if not force_new and source_message is not None:
                    if mid in state.retired:
                        raise StaleMenu('Ese menú ya fue reemplazado. Usá el menú actual.')
                    if not _has_media(source_message):
                        if state.message_id is not None and mid != state.message_id:
                            raise StaleMenu('Ese menú ya fue reemplazado. Usá el menú actual.')
                        if mid is not None:
                            state.message_id, state.message = mid, source_message
                state.generation += 1
                generation = state.generation
            view = NavigationView(self, chat_id, generation, source_message=source_message,
                                  callback=callback, force_new=force_new, control=control)
            if control and mid is not None:
                state.controls.setdefault(mid, generation)
            self._save()
            _CURRENT.set((self, view))
            return view

    async def _retire(self, chat_id, message_id, message=None, *, finished_control=False):
        if message_id is None:
            return
        state = self._chat(chat_id)
        if message_id in state.controls and not finished_control:
            return
        is_media = _has_media(message) if message is not None else False
        deleted = False
        if not is_media:
            try:
                deleted = bool(await self.bot.delete_message(chat_id=chat_id, message_id=message_id))
            except Exception as error:
                detail = str(error).casefold()
                deleted = 'message to delete not found' in detail or 'message not found' in detail
                if not deleted:
                    logger.info('Telegram no permitió borrar el menú anterior; se retiran los botones.')
        if deleted:
            state.pending.pop(message_id, None)
            await self._hook(self.on_retire, chat_id, message_id)
        else:
            try:
                await self.bot.edit_message_reply_markup(chat_id=chat_id,
                                                         message_id=message_id, reply_markup=None)
                state.pending.pop(message_id, None)
            except Exception:
                state.pending[message_id] = message
                logger.warning('Quedó pendiente retirar los controles del menú anterior.')
        state.retired.add(message_id)
        if len(state.retired) > 256:
            state.retired = set(sorted(state.retired)[-128:])
        if finished_control:
            state.controls.pop(message_id, None)

    async def send(self, chat_id, text, **kwargs):
        """Wrapper hook for existing text sends carrying navigation buttons.

        Background notifications without a lease can retain their result text,
        but cannot introduce another navigation keyboard.
        """
        view = self.current_view(chat_id)
        if view is not None:
            return await view.send(text, **kwargs)
        options = dict(kwargs)
        if not control_markup(options.get('reply_markup')):
            options.pop('reply_markup', None)
        result = await self.bot.send_message(chat_id=int(chat_id), text=text, **options)
        await self._hook(self.on_track, result)
        return result


class NavigationView:
    def __init__(self, manager, chat_id, generation, *, source_message=None,
                 callback=None, force_new=False, control=False):
        self.manager, self.chat_id, self.generation = manager, chat_id, generation
        self.message = source_message
        self.callback = callback
        self.force_new = force_new or _has_media(source_message)
        self.control = control
        self.data = getattr(callback, 'data', None)

    @property
    def is_current(self):
        return self.manager._chat(self.chat_id).generation == self.generation

    async def answer(self, *args, **kwargs):
        if self.callback is not None:
            return await self.callback.answer(*args, **kwargs)
        return None

    async def _edit(self, text, options):
        mid = _message_id(self.message)
        try:
            result = await self.manager.bot.edit_message_text(chat_id=self.chat_id,
                                                             message_id=mid, text=text, **options)
        except Exception as error:
            if 'message is not modified' not in str(error).casefold():
                raise
            return self.message
        if getattr(result, 'message_id', None) is not None:
            self.message = result
        return self.message

    async def edit_message_text(self, text, **kwargs):
        state = self.manager._chat(self.chat_id)
        async with state.lock:
            markup = kwargs.get('reply_markup')
            is_control = control_markup(markup)
            if self.control:
                if not is_control and markup is not None:
                    return await self._send_locked(text, dict(kwargs), move_to_end=True)
                if _message_id(self.message) is not None:
                    return await self._edit(text, kwargs)
            if not self.is_current:
                if is_control:
                    return await self._send_locked(text, dict(kwargs), move_to_end=True)
                return self.message
            if self.force_new or _message_id(self.message) is None:
                return await self._send_locked(text, dict(kwargs), move_to_end=True)
            result = await self._edit(text, kwargs)
            mid = _message_id(self.message)
            if is_control:
                self.control = True
                state.controls[mid] = self.generation
                if state.message_id == mid:
                    state.message_id, state.message = None, None
            else:
                state.message_id, state.message = mid, self.message
            self.manager._save()
            await self.manager._hook(self.manager.on_track, result)
            return result

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        state = self.manager._chat(self.chat_id)
        async with state.lock:
            if not self.is_current and not self.control:
                return self.message
            mid = _message_id(self.message)
            if mid is None:
                return self.message
            if self.control and not self.is_current and not control_markup(reply_markup):
                reply_markup = None
            try:
                result = await self.manager.bot.edit_message_reply_markup(
                    chat_id=self.chat_id, message_id=mid, reply_markup=reply_markup, **kwargs)
            except Exception as error:
                if 'message is not modified' not in str(error).casefold():
                    raise
                return self.message
            if getattr(result, 'message_id', None) is not None:
                self.message = result
            return self.message

    async def send(self, text, *, move_to_end=True, **kwargs):
        state = self.manager._chat(self.chat_id)
        async with state.lock:
            return await self._send_locked(text, dict(kwargs), move_to_end=move_to_end)

    async def _send_locked(self, text, options, *, move_to_end):
        state = self.manager._chat(self.chat_id)
        is_control = control_markup(options.get('reply_markup'))
        if not self.is_current and not is_control:
            if self.control:
                options.pop('reply_markup', None)
                result = await self.manager.bot.send_message(chat_id=self.chat_id, text=text, **options)
                await self.manager._hook(self.manager.on_track, result)
                mid = _message_id(self.message)
                if mid is not None:
                    try:
                        await self.manager.bot.edit_message_reply_markup(
                            chat_id=self.chat_id, message_id=mid, reply_markup=None)
                    except Exception:
                        logger.warning('No se pudo retirar el control de trabajo finalizado.')
                    state.controls.pop(mid, None)
                return result
            return self.message
        if not move_to_end and self.message is not None and not self.force_new:
            return await self._edit(text, options)
        old_id, old_message = state.message_id, state.message
        source_id, source_message = _message_id(self.message), self.message
        was_control = self.control
        result = await self.manager.bot.send_message(chat_id=self.chat_id, text=text, **options)
        new_id = _message_id(result)
        if self.is_current:
            state.message_id, state.message = (None, None) if is_control else (new_id, result)
        self.message, self.force_new = result, False
        self.control = is_control
        if is_control and new_id is not None:
            state.controls[new_id] = self.generation
        self.manager._save()
        await self.manager._hook(self.manager.on_track, result)
        if self.is_current and old_id is not None and old_id != new_id:
            await self.manager._retire(self.chat_id, old_id, old_message)
        if source_id is not None and source_id not in (new_id, old_id):
            await self.manager._retire(self.chat_id, source_id, source_message,
                                       finished_control=was_control and not is_control)
        elif was_control and not is_control and source_id is not None and source_id != new_id:
            await self.manager._retire(self.chat_id, source_id, source_message, finished_control=True)
        for mid, message in list(state.pending.items())[:5]:
            if mid != new_id:
                await self.manager._retire(self.chat_id, mid, message)
        return result

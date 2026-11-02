# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Lectura autenticada de Stories sin navegador, visor ni consultas previas."""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from instagram_sessions import ErrorConsultaInstagram, ErrorSesionInstagram, validar_datos_instagram

class ErrorProtocoloHistorias(ErrorConsultaInstagram):
    pass

class ErrorTransitorioHistorias(ErrorConsultaInstagram):
    pass

_HOST = 'https://i.instagram.com'
_PATH = '/api/v1/feed/reels_media/'
_USER_AGENT = ('Instagram 361.0.0.35.82 (iPad13,8; iOS 18_0; en_US; en-US; '
               'scale=2.00; 2048x2732; 674117118) AppleWebKit/420+')


class _ContextoHistorias:
    def __init__(self, request, estado):
        self.request = request
        self._estado = estado

    def storage_state(self, **kwargs):
        estado = copy.deepcopy(self._estado)
        estado['cookies'] = self.request.storage_state()['cookies']
        return estado


@contextmanager
def crear_contexto_historias(playwright, ruta: Path):
    try:
        estado = json.loads(Path(ruta).read_text(encoding='utf-8-sig'))
        if not isinstance(estado, dict) or not isinstance(estado.get('cookies'), list):
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise ErrorSesionInstagram('archivo', 'No se pudo leer el JSON de la sesión seleccionada.') from None
    try:
        request = playwright.request.new_context(storage_state={'cookies': estado['cookies'], 'origins': []})
    except Exception:
        raise ErrorProtocoloHistorias('No se pudo preparar el lector autenticado de Stories.') from None
    try:
        yield _ContextoHistorias(request, estado)
    finally:
        request.dispose()


def _numero(valor):
    if isinstance(valor, bool) or not isinstance(valor, (str, int)):
        return None
    texto = str(valor)
    return texto if re.fullmatch(r'[1-9][0-9]{0,39}', texto) else None


def _cabeceras(context, *, path=_PATH):
    if not isinstance(path, str) or not re.fullmatch(r'/api/v1/feed/(?:reels_media/|user/[1-9][0-9]{0,39}/story/)', path):
        raise ErrorProtocoloHistorias('La ruta interna del lector de Stories no es válida.')
    valores = {}
    for cookie in context.request.storage_state().get('cookies', []):
        if not isinstance(cookie, dict) or cookie.get('name') not in ('sessionid', 'ds_user_id'):
            continue
        if str(cookie.get('domain', '')).lstrip('.') not in ('instagram.com', 'i.instagram.com'):
            continue
        ruta = str(cookie.get('path') or '/')
        if not path.startswith(ruta) or not (ruta.endswith('/') or len(ruta) == len(path) or path[len(ruta)] == '/'):
            continue
        try:
            if float(cookie.get('expires', -1)) > 0 and float(cookie['expires']) <= time.time():
                continue
        except (TypeError, ValueError):
            continue
        valor = cookie.get('value')
        if not isinstance(valor, str) or not valor or len(valor) > 16384 or re.search(r'[\x00-\x1f\x7f]', valor):
            continue
        anterior = valores.setdefault(cookie['name'], valor)
        if anterior != valor:
            raise ErrorSesionInstagram('archivo', 'La sesión contiene cookies de autenticación incompatibles.')
    if not valores.get('sessionid') or not _numero(valores.get('ds_user_id')):
        raise ErrorSesionInstagram('archivo', 'El JSON seleccionado no contiene las cookies de autenticación necesarias.')
    agente = os.getenv('INSTAGRAM_STORIES_USER_AGENT', _USER_AGENT).strip()
    app_id = os.getenv('INSTAGRAM_STORIES_APP_ID', '124024574287414').strip()
    if not agente or len(agente) > 512 or re.search(r'[\x00-\x1f\x7f]', agente) or not re.fullmatch(r'[0-9]{1,20}', app_id):
        raise ErrorProtocoloHistorias('La configuración del cliente de Stories no es válida.')
    token = base64.b64encode(json.dumps(
        {'ds_user_id': valores['ds_user_id'], 'sessionid': valores['sessionid']},
        separators=(',', ':'),
    ).encode()).decode()
    return {'Accept': 'application/json', 'User-Agent': agente, 'X-IG-App-ID': app_id,
            'IG-Intended-User-ID': valores['ds_user_id'], 'Authorization': 'Bearer IGT:2:' + token}


def _identidad_usuario(usuario, esperado):
    if not isinstance(usuario, dict):
        return False
    ids = [usuario[k] for k in ('pk', 'id') if k in usuario]
    return bool(ids) and all(_numero(valor) == esperado for valor in ids)


def _normalizar_items(data, user_id):
    if not isinstance(data, dict) or data.get('status') != 'ok':
        raise ErrorProtocoloHistorias('Instagram no confirmó una respuesta válida de Stories.')
    esperado = str(user_id)
    if 'reels' in data:
        mapa = data['reels']
        if not isinstance(mapa, dict):
            raise ErrorProtocoloHistorias('Instagram devolvió un formato de Stories no reconocido.')
        if not mapa:
            return []
        if esperado not in mapa or not isinstance(mapa[esperado], dict):
            raise ErrorProtocoloHistorias('La respuesta de Stories no corresponde al perfil solicitado.')
        reels = [mapa[esperado]]
    elif isinstance(data.get('reels_media'), list):
        reels = data['reels_media']
        if not all(isinstance(reel, dict) and _identidad_usuario(reel.get('user'), esperado) for reel in reels):
            raise ErrorProtocoloHistorias('La respuesta de Stories no corresponde al perfil solicitado.')
    else:
        raise ErrorProtocoloHistorias('Instagram devolvió un formato de Stories no reconocido.')
    return _normalizar_reels(reels, esperado)


def _normalizar_reels(reels, esperado, reel_id=None):
    encontrados = {}
    for reel in reels:
        if not _identidad_usuario(reel.get('user'), esperado):
            raise ErrorProtocoloHistorias('La respuesta de Stories no corresponde al perfil solicitado.')
        if 'id' in reel and (str(reel['id']) != reel_id if reel_id is not None else _numero(reel['id']) != esperado):
            raise ErrorProtocoloHistorias('La respuesta contiene un reel de otro perfil.')
        items = reel.get('items')
        if not isinstance(items, list):
            raise ErrorProtocoloHistorias('Instagram no incluyó una lista válida de historias.')
        for item in items:
            if not isinstance(item, dict):
                raise ErrorProtocoloHistorias('Instagram devolvió una historia con formato inválido.')
            pk = _numero(item.get('pk'))
            raw_id = item.get('id')
            if raw_id is not None:
                match = re.fullmatch(r'([1-9][0-9]{0,39})(?:_([1-9][0-9]{0,39}))?', str(raw_id))
                if not match or match[2] is not None and match[2] != esperado or pk is not None and match[1] != pk:
                    raise ErrorProtocoloHistorias('La identidad de una historia no coincide con el perfil solicitado.')
                pk = pk or match[1]
            if pk is None or 'pk' in item and _numero(item['pk']) is None:
                raise ErrorProtocoloHistorias('Instagram devolvió una historia sin identificador válido.')
            if 'user' in item and not _identidad_usuario(item['user'], esperado):
                raise ErrorProtocoloHistorias('Una historia pertenece a otro perfil.')
            normalizado = {**item, 'id': pk, 'pk': pk}
            anterior = encontrados.setdefault(pk, normalizado)
            if anterior != normalizado:
                raise ErrorProtocoloHistorias('Instagram devolvió datos incompatibles para una misma historia.')
    return list(encontrados.values())


def _consultar(context, reel_id):
    cabeceras = _cabeceras(context)
    try:
        response = context.request.get(
            _HOST + _PATH, params={'reel_ids': reel_id}, headers=cabeceras,
            timeout=30_000, max_redirects=0, fail_on_status_code=False,
        )
    except Exception:
        raise ErrorTransitorioHistorias('No se pudo consultar Stories por un fallo de conexión o tiempo de espera.') from None
    try:
        status = int(response.status)
        try:
            data = response.json()
        except Exception:
            data = None
        validar_datos_instagram(data)
        if 300 <= status < 400:
            destino = urlsplit(urljoin(_HOST + _PATH, response.headers.get('location', '')))
            ruta = destino.path.lower() if destino.hostname in ('instagram.com', 'www.instagram.com', 'i.instagram.com') else ''
            if ruta.startswith('/accounts/login'):
                raise ErrorSesionInstagram('login', 'Instagram requiere iniciar sesión nuevamente.')
            if any(x in ruta for x in ('/challenge', '/checkpoint', '/auth_platform/', '/accounts/confirm_email', '/accounts/confirm_phone')):
                raise ErrorSesionInstagram('verificacion', 'Instagram requiere verificación/CAPTCHA manual.')
            raise ErrorProtocoloHistorias('Instagram redirigió la consulta de Stories; no se interpretó la redirección como historias.')
        if status in (401, 403, 429):
            tipo = {401: 'login', 403: 'rechazada', 429: 'rate_limit'}[status]
            raise ErrorSesionInstagram(tipo, f'Instagram respondió HTTP {status} al consultar Stories.', http_status=status)
        if status >= 500:
            raise ErrorTransitorioHistorias(f'Instagram respondió HTTP {status} al consultar Stories.')
        if status != 200:
            raise ErrorProtocoloHistorias(f'Instagram respondió HTTP {status} al consultar Stories.')
        tipo = response.headers.get('content-type', '').lower()
        if 'json' not in tipo or data is None:
            raise ErrorProtocoloHistorias('Instagram no entregó los metadatos JSON de Stories; no se modificó la sesión.')
        return data
    finally:
        response.dispose()


def consultar_historias(context, username: str, user_id: int):
    uid = _numero(user_id)
    if uid is None or not re.fullmatch(r'[a-zA-Z0-9._]{1,30}', username):
        raise ErrorProtocoloHistorias('El perfil solicitado para Stories no es válido.')
    return _normalizar_items(_consultar(context, uid), uid)


def consultar_destacada(context, username: str, highlight_id: str):
    identificador = _numero(highlight_id)
    if identificador is None or not re.fullmatch(r'[a-zA-Z0-9._]{1,30}', username):
        raise ErrorProtocoloHistorias('La destacada solicitada no es válida.')
    clave = 'highlight:' + identificador
    data = _consultar(context, clave)
    if not isinstance(data, dict) or data.get('status') != 'ok':
        raise ErrorProtocoloHistorias('Instagram no confirmó una respuesta válida de la destacada.')
    mapa = data.get('reels')
    reel = mapa.get(clave) if isinstance(mapa, dict) else None
    if not isinstance(reel, dict):
        raise ErrorProtocoloHistorias('Instagram no devolvió el grupo destacado solicitado.')
    usuario = reel.get('user')
    if not isinstance(usuario, dict) or str(usuario.get('username', '')).casefold() != username.casefold():
        raise ErrorProtocoloHistorias('La destacada pertenece a otro perfil.')
    propietario = _numero(usuario.get('pk') if 'pk' in usuario else usuario.get('id'))
    if propietario is None:
        raise ErrorProtocoloHistorias('Instagram no identificó al propietario de la destacada.')
    return _normalizar_reels([reel], propietario, reel_id=clave)

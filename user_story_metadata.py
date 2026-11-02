# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Read-only individual-user Stories metadata, independent of reels_media/web.

All results use the existing canonical normalizer and downloader. No browser,
viewer, seen operation, account rotation, or persistent state is created here.
"""
from __future__ import annotations
from datetime import datetime, timezone
import math
import re
from urllib.parse import urljoin,urlsplit

import mobile_metadata as mobile
from instagram_sessions import ErrorSesionInstagram,validar_datos_instagram


def _valid_date(item):
    dates=[]
    for key in ('taken_at_timestamp','taken_at','taken_at_ts'):
        value=item.get(key)
        if value is None:continue
        if isinstance(value,bool):return False
        try:
            number=float(value)
            if not math.isfinite(number) or number<=0:return False
            if number>10_000_000_000:number/=1000
            dates.append(datetime.fromtimestamp(number,tz=timezone.utc))
        except (TypeError,ValueError,OverflowError,OSError):return False
    return bool(dates) and all(value==dates[0] for value in dates)


def _has_media(item):
    video=None;image=None
    resources=item.get('video_resources') or []
    if isinstance(resources,list):
        video=next((str(e['src']) for e in reversed(resources) if isinstance(e,dict) and e.get('src')),None)
    versions=item.get('video_versions') or []
    if video is None and isinstance(versions,list):
        video=next((str(e['url']) for e in versions if isinstance(e,dict) and e.get('url')),None)
    if video is None and item.get('video_url'):video=str(item['video_url'])
    image=next((str(item[key]) for key in ('display_url','thumbnail_src') if item.get(key)),None)
    images=item.get('image_versions2')
    if image is None and isinstance(images,dict) and isinstance(images.get('candidates'),list):
        image=next((str(e['url']) for e in images['candidates'] if isinstance(e,dict) and e.get('url')),None)
    if not video and not image or item.get('media_type')==2 and not video:return False
    for url in (video,image):
        if url is None:continue
        if re.search(r'[\x00-\x1f\x7f]',url):return False
        try:
            parsed=urlsplit(url)
            if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:return False
        except ValueError:return False
    return True


def normalizar_respuesta_usuario(data,uid):
    if not isinstance(data,dict) or data.get('status')!='ok':
        raise mobile.ErrorProtocoloHistorias('Instagram no confirmó la respuesta del recorrido individual.')
    if data.get('more_available') is True or data.get('has_more') is True:
        raise mobile.ErrorProtocoloHistorias('El recorrido individual indicó que faltan datos.')
    reel=data.get('reel')
    if not isinstance(reel,dict):
        raise mobile.ErrorProtocoloHistorias('El recorrido individual no devolvió un reel identificado.')
    if reel.get('more_available') is True or reel.get('has_more') is True:
        raise mobile.ErrorProtocoloHistorias('El recorrido individual devolvió datos incompletos.')
    items=mobile._normalizar_reels([reel],str(uid))
    if any(not _valid_date(item) or not _has_media(item) for item in items):
        raise mobile.ErrorProtocoloHistorias('El recorrido individual no incluyó fechas o archivos válidos.')
    return items


def consultar_historias_por_usuario(context,username,user_id):
    uid=mobile._numero(user_id)
    if uid is None or not isinstance(username,str) or not re.fullmatch(r'[A-Za-z0-9._]{1,30}',username):
        raise mobile.ErrorProtocoloHistorias('El perfil del recorrido individual no es válido.')
    path='/api/v1/feed/user/'+uid+'/story/'
    try:
        headers=mobile._cabeceras(context,path=path)
    except ErrorSesionInstagram as error:
        if error.tipo=='archivo':
            mobile._cabeceras(context)
            raise mobile.ErrorProtocoloHistorias('La sesión no autoriza este recorrido de lectura.') from None
        raise
    url='https://i.instagram.com'+path
    try:
        response=context.request.get(url,headers=headers,timeout=30_000,
                                     max_redirects=0,fail_on_status_code=False)
    except Exception:
        raise mobile.ErrorTransitorioHistorias('No se pudo completar la consulta individual de Stories.') from None
    try:
        status=int(response.status)
        try:data=response.json()
        except Exception:data=None
        validar_datos_instagram(data)
        if 300<=status<400:
            location=urlsplit(urljoin(url,response.headers.get('location','')))
            target=location.path.lower() if location.hostname in ('instagram.com','www.instagram.com','i.instagram.com') else ''
            if target.startswith('/accounts/login'):
                raise ErrorSesionInstagram('login','Instagram requiere iniciar sesión nuevamente.')
            if any(x in target for x in ('/challenge','/checkpoint','/auth_platform/','/accounts/confirm_email','/accounts/confirm_phone')):
                raise ErrorSesionInstagram('verificacion','Instagram requiere verificación/CAPTCHA manual.')
            raise mobile.ErrorProtocoloHistorias('Instagram redirigió la consulta individual de Stories.')
        if status in (401,403,429):
            kind={401:'login',403:'rechazada',429:'rate_limit'}[status]
            raise ErrorSesionInstagram(kind,f'Instagram respondió HTTP {status} en el recorrido individual.',http_status=status)
        if status>=500:
            raise mobile.ErrorTransitorioHistorias('Instagram devolvió un error temporal en el recorrido individual.')
        if status!=200:
            raise mobile.ErrorProtocoloHistorias('Instagram rechazó el formato de la consulta individual.')
        if data is None:
            raise mobile.ErrorProtocoloHistorias('Instagram no devolvió los metadatos JSON del recorrido individual.')
        return normalizar_respuesta_usuario(data,uid)
    finally:response.dispose()

# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Isolated facade: unchanged session rotation surrounds job-scoped recovery."""
from contextlib import contextmanager

import mobile_metadata as mobile
from user_story_metadata import consultar_historias_por_usuario
from job_fallback import (ClassifiedFailure,FailureKind,RecoveryPolicy,run_metadata)
from instagram_sessions import ErrorConsultaInstagram,ErrorSesionInstagram
from web_metadata import WebMetadataReader

_numero=mobile._numero
_cabeceras=mobile._cabeceras
_normalizar_items=mobile._normalizar_items
_normalizar_reels=mobile._normalizar_reels


@contextmanager
def crear_contexto_historias(playwright,ruta):
    with mobile.crear_contexto_historias(playwright,ruta) as context:
        context._fallback_playwright=playwright
        yield context


def classify(error):
    if isinstance(error,mobile.ErrorProtocoloHistorias):return FailureKind.PROTOCOL
    if isinstance(error,mobile.ErrorTransitorioHistorias):return FailureKind.TRANSIENT
    if isinstance(error,ClassifiedFailure):return error.kind
    if isinstance(error,ErrorSesionInstagram):
        if error.tipo=='rate_limit':return FailureKind.RATE_LIMIT
        if error.tipo=='rechazada':return FailureKind.PERMISSION
        return FailureKind.AUTH
    return FailureKind.UNKNOWN


def _alternative(context,username,owner_id,reel_id=None):
    playwright=getattr(context,'_fallback_playwright',None)
    if playwright is None:
        playwright=getattr(context,'_isolated_fallback_playwright',None)
    if playwright is None:
        raise mobile.ErrorProtocoloHistorias('El contexto no dispone del lector web alternativo.')
    import history
    try:
        with WebMetadataReader(playwright,history._crear_contexto,mobile._normalizar_reels) as web:
            return web.consultar(username,owner_id,reel_id)
    except ClassifiedFailure as error:
        if error.kind is FailureKind.RATE_LIMIT:
            raise ErrorSesionInstagram('rate_limit','Instagram solicitó una pausa en el lector web.') from None
        if error.kind is FailureKind.AUTH:
            raise ErrorSesionInstagram('login','Instagram requiere renovar o verificar la sesión web.') from None
        if error.kind is FailureKind.PERMISSION:
            raise ErrorSesionInstagram('rechazada','Instagram rechazó la consulta web.') from None
        raise


def _run(primary,alternative,third=None):
    try:
        return run_metadata(primary,alternative,third=third,classify=classify)
    except ClassifiedFailure:
        raise ErrorConsultaInstagram('Los recorridos de consulta disponibles no pudieron completar la lectura.') from None


def consultar_historias(context,username,user_id):
    return _run(lambda:mobile.consultar_historias(context,username,user_id),
                lambda:_alternative(context,username,user_id),
                lambda:consultar_historias_por_usuario(context,username,user_id))


def consultar_destacada(context,username,highlight_id):
    import history
    owner=history._cargar_cache().get(username)
    alternative=lambda:_alternative(context,username,owner,'highlight:'+str(highlight_id))
    return _run(lambda:mobile.consultar_destacada(context,username,highlight_id),alternative)

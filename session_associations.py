# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Reconciliación expresa de permisos, independiente de las descargas de Stories."""
import copy
import logging
import threading
import time
import uuid
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import instagram_sessions as sesiones

logger = logging.getLogger(__name__)


def repartir_preferidas_confirmadas(confirmadas):
    """Asignar entre accesos comprobados, priorizando habituales y menor carga.

    Los perfiles con menos opciones se asignan primero. Una preferida antigua
    sólo desempata entre candidatos con la misma cantidad de perfiles.
    """
    with sesiones.LOCK:
        disponibles = sesiones.listar_sesiones(incluir_pausadas=True)
        vigentes = {s.id: s for s in disponibles if sesiones._indisponibilidad(s) is None}
        posiciones = {s.id: i for i, s in enumerate(sesiones.ordenar_sesiones_por_prioridad(disponibles))}
        registro = sesiones._registro()
        candidatos = {}
        for username, verificadas in confirmadas.items():
            vinculo = registro['perfiles'].get(username, {})
            asociados = {f.get('id') for f in vinculo.get('sesiones_con_acceso', []) if isinstance(f, dict)}
            opciones = [vigentes[s.id] for s in verificadas
                        if s.id in vigentes and vigentes[s.id] == s and s.id in asociados]
            opciones = list({s.id: s for s in opciones}.values())
            normales = [s for s in opciones if not s.reserva]
            if normales or opciones:
                candidatos[username] = normales or opciones
        cargas = {ident: 0 for ident in vigentes}
        for username, vinculo in registro['perfiles'].items():
            if username not in candidatos and vinculo.get('sesion_id') in cargas:
                cargas[vinculo['sesion_id']] += 1
        resumen = {'preferidas_repartidas': 0, 'preferidas_cambiadas': 0,
                   'preferidas_reserva': 0, 'reparto_preferidas': {}}
        for username in sorted(candidatos, key=lambda u: (len(candidatos[u]), u)):
            vinculo = registro['perfiles'][username]
            anterior = vinculo.get('sesion_id')
            elegida = min(candidatos[username], key=lambda s:
                (cargas[s.id], s.id != anterior, posiciones[s.id]))
            vinculo.update(sesion_id=elegida.id, cuenta=elegida.username,
                           preferida_manual=False, preferida_balanceada=True,
                           preferida_balanceada_en=sesiones._ahora())
            cargas[elegida.id] += 1
            resumen['preferidas_repartidas'] += 1
            resumen['preferidas_cambiadas'] += int(anterior != elegida.id)
            resumen['preferidas_reserva'] += int(elegida.reserva)
            resumen['reparto_preferidas'][elegida.id] = resumen['reparto_preferidas'].get(elegida.id, 0) + 1
        if candidatos:
            sesiones.escribir_json_atomico(sesiones.ruta_registro(), registro)
        return resumen


def _alcance_asociaciones(informe):
    """Legacy matrices without scope represent the original all-sessions flow."""
    alcance = informe.get('alcance', {'tipo': 'todas'}) if isinstance(informe, dict) else {'tipo': 'todas'}
    if not isinstance(alcance, dict):
        raise sesiones.ConfiguracionSesionesError('El alcance guardado de las asociaciones es inválido.')
    if alcance == {'tipo': 'todas'}:
        return dict(alcance)
    if (set(alcance) != {'tipo', 'sesion_id', 'username'}
            or alcance.get('tipo') != 'individual'
            or not isinstance(alcance.get('sesion_id'), str) or not alcance['sesion_id']
            or alcance.get('username') is not None and not isinstance(alcance['username'], str)):
        raise sesiones.ConfiguracionSesionesError('El alcance guardado de las asociaciones es inválido.')
    return dict(alcance)


def sesion_para_reintentar_asociaciones():
    """Return the one configured session of a saved individual matrix, or None."""
    with sesiones.LOCK:
        informe = sesiones._registro().get('sincronizacion_asociaciones')
        alcance = _alcance_asociaciones(informe)
        if alcance['tipo'] == 'todas':
            return None
        elegida = next((s for s in sesiones.listar_sesiones(incluir_deshabilitadas=True)
                        if s.id == alcance['sesion_id']), None)
        if elegida is None or elegida.username != alcance['username']:
            raise sesiones.ConfiguracionSesionesError(
                'La sesión de la actualización pendiente cambió o ya no existe. Elegí una sesión nuevamente.')
        return elegida


_SINCRONIZACION_LOCK = threading.Lock()
_DEFINITIVOS = {'acceso', 'sin_acceso'}


def sincronizar_asociaciones(usernames, crear_verificador, *, detener=None,
                            progreso=None, demora_entre_perfiles=1.0,
                            max_reintentos=1, demora_reintento=5.0,
                            reintentar_pendientes=False, sesion_esperada=None):
    """Guardar cada pareja y reintentar sólo errores temporales, con límite.

    Retomar pendientes reutiliza el mismo registro. No vuelve a consultar los
    accesos ni las denegaciones ya comprobados. CAPTCHA/login/429 no se
    reintentan automáticamente; nunca se eliminan asociaciones por un timeout.
    """
    if not isinstance(usernames, list):
        raise ValueError('La sincronización necesita una lista de perfiles.')
    objetivos = list(dict.fromkeys(sesiones._normalizar_perfil_explicito(u) for u in usernames))
    if not isinstance(demora_entre_perfiles, (int, float)) or not 0 <= demora_entre_perfiles <= 10:
        raise ValueError('Separación entre comprobaciones inválida.')
    if type(max_reintentos) is not int or not 0 <= max_reintentos <= 2:
        raise ValueError('Cantidad de reintentos inválida.')
    if not isinstance(demora_reintento, (int, float)) or not 0 <= demora_reintento <= 300:
        raise ValueError('Espera entre reintentos inválida.')
    if not _SINCRONIZACION_LOCK.acquire(blocking=False):
        raise sesiones.ConfiguracionSesionesError('Ya hay una actualización de asociaciones en curso.')
    try:
        return _sincronizar(objetivos, crear_verificador, detener, progreso,
                            demora_entre_perfiles, max_reintentos,
                            demora_reintento, reintentar_pendientes, sesion_esperada)
    finally:
        _SINCRONIZACION_LOCK.release()


def _sincronizar(objetivos, crear_verificador, detener, progreso, demora,
                 max_reintentos, demora_reintento, retomar, sesion_esperada=None):
    with sesiones.LOCK:
        if sesiones.sesion_actual() is not None or sesiones._VERIFICACION_MANUAL.get() is not None:
            raise sesiones.ConfiguracionSesionesError('La sincronización debe iniciarse fuera de una consulta activa.')
        todas = sesiones.listar_sesiones(incluir_deshabilitadas=True)
        habilitadas = sesiones.listar_sesiones(incluir_pausadas=True)
        disponibles, omitidas = [], []
        for sesion in todas:
            motivo = ({'tipo': 'deshabilitada'} if sesion not in habilitadas
                      else sesiones._indisponibilidad(sesion))
            if motivo:
                omitidas.append({'id': sesion.id, 'motivo': motivo['tipo']})
            else:
                disponibles.append(sesion)
        anterior = sesiones._registro().get('sincronizacion_asociaciones')
        if sesion_esperada is not None:
            if not isinstance(sesion_esperada, sesiones.SesionInstagram):
                raise sesiones.ConfiguracionSesionesError('La sesión elegida es inválida.')
            elegida = next((s for s in todas if s.id == sesion_esperada.id), None)
            if elegida is None or elegida != sesion_esperada:
                raise sesiones.ConfiguracionSesionesError(
                    'La sesión elegida cambió desde que se abrió el menú. Volvé a seleccionarla.')
            if elegida not in disponibles:
                raise sesiones.ConfiguracionSesionesError(
                    'La sesión elegida está pausada, deshabilitada o requiere renovación.')
            disponibles = [elegida]
            omitidas = []
        alcance = ({'tipo': 'individual', 'sesion_id': sesion_esperada.id,
                    'username': sesion_esperada.username} if sesion_esperada is not None
                   else {'tipo': 'todas'})
    disponibles = sesiones.ordenar_sesiones_por_prioridad(disponibles)
    informe = {'version': 2, 'id': uuid.uuid4().hex, 'objetivos': objetivos, 'alcance': alcance,
               'sesiones_plan': {s.id: s.username for s in disponibles},
               'comprobaciones': {s.id: {u: {'estado': 'pendiente', 'intentos': 0}
                                            for u in objetivos} for s in disponibles},
               'añadidas': 0, 'eliminadas': 0, 'sesiones_procesadas': 0,
               'sesiones_omitidas': omitidas, 'username_actual': None, 'sesion_actual': None,
               'etapa': 'preparando', 'detenida': False, 'interrumpida': False, 'motivo': '',
               'pasada': 0, 'intentos_ejecucion': 0, 'reintentos_ejecucion': 0,
               'preferidas_repartidas': 0, 'preferidas_cambiadas': 0,
               'preferidas_reserva': 0, 'reparto_preferidas': {}}
    if retomar:
        if (not isinstance(anterior, dict) or anterior.get('version') != 2
                or not isinstance(anterior.get('comprobaciones'), dict)
                or not isinstance(anterior.get('sesiones_plan'), dict)):
            raise sesiones.ConfiguracionSesionesError(
                'La actualización anterior no tiene un registro detallado de pendientes. '
                'No se iniciará una comprobación completa automáticamente.')
        if _alcance_asociaciones(anterior) != alcance:
            raise sesiones.ConfiguracionSesionesError(
                'La actualización pendiente corresponde a otro alcance o sesión. Volvé al menú de asociaciones.')
        filas = anterior['comprobaciones']
        if any(not isinstance(matriz, dict) or any(not isinstance(f, dict)
                or f.get('estado') not in _DEFINITIVOS | {'pendiente', 'error'}
                or type(f.get('intentos')) is not int or f['intentos'] < 0
                for f in matriz.values()) for matriz in filas.values()):
            raise sesiones.ConfiguracionSesionesError('El registro de comprobaciones es inválido.')
        vigentes = {s.id: s for s in todas}
        plan = {sid: cuenta for sid, cuenta in anterior['sesiones_plan'].items()
                if sid in vigentes and vigentes[sid].username == cuenta and sid in filas}
        objetivos = [u for u in objetivos if u in anterior.get('objetivos', [])]
        informe.update(id=anterior.get('id') or informe['id'], objetivos=objetivos,
            sesiones_plan=plan,
            comprobaciones={sid: {u: copy.deepcopy(filas[sid][u]) for u in objetivos if u in filas[sid]}
                            for sid in plan},
            añadidas=anterior.get('añadidas', 0), eliminadas=anterior.get('eliminadas', 0))
        disponibles = [s for s in disponibles if s.id in plan]
    matriz = informe['comprobaciones']
    bloqueadas = set()
    procesadas = set()
    intentadas = set()
    confirmadas = {u: [] for u in objetivos}
    for sesion in disponibles:
        for u, fila in matriz[sesion.id].items():
            if fila['estado'] == 'acceso':
                confirmadas[u].append(sesion)

    def parada():
        return detener is not None and detener.is_set()

    def esperar(segundos):
        if segundos:
            if detener is not None:
                detener.wait(segundos)
            else:
                time.sleep(segundos)

    def resumir():
        filas = [f for pares in matriz.values() for f in pares.values()]
        informe.update(total=len(filas), sesiones_total=len(matriz),
            comprobadas=sum(f['intentos'] > 0 for f in filas),
            confirmadas=sum(f['estado'] == 'acceso' for f in filas),
            sin_acceso=sum(f['estado'] == 'sin_acceso' for f in filas),
            fallidas=sum(f['estado'] == 'error' for f in filas),
            pendientes=sum(f['estado'] not in _DEFINITIVOS for f in filas),
            intentos_total=sum(f['intentos'] for f in filas),
            sesiones_procesadas=len(procesadas), actualizada_en=sesiones._ahora())

    def guardar(registro=None):
        resumir()
        with sesiones.LOCK:
            registro = sesiones._registro() if registro is None else registro
            registro['sincronizacion_asociaciones'] = copy.deepcopy(informe)
            sesiones.escribir_json_atomico(sesiones.ruta_registro(), registro)

    def informar():
        guardar()
        if progreso is not None:
            progreso(copy.deepcopy(informe))

    def omitir(sesion, motivo):
        bloqueadas.add(sesion.id)
        if not any(f['id'] == sesion.id and f['motivo'] == motivo for f in informe['sesiones_omitidas']):
            informe['sesiones_omitidas'].append({'id': sesion.id, 'motivo': motivo})

    def resultado(username, sesion, estado, tipo='', detalle='', reintentable=False):
        fila = matriz[sesion.id][username]
        if estado == 'error':
            logger.warning('Comprobación de asociaciones pendiente: sesion=%s perfil=%s tipo=%s intento=%s',
                           sesion.id, username, tipo, fila['intentos'])
        with sesiones.LOCK:
            registro = sesiones._registro()
            if estado in _DEFINITIVOS:
                sesiones.comprobar_sesion_habilitada(sesion)
                previo = registro['perfiles'].get(username, {})
                if not isinstance(previo, dict) or not isinstance(previo.get('sesiones_con_acceso', []), list):
                    raise sesiones.ConfiguracionSesionesError('El vínculo del perfil es inválido.')
                filas = list(previo.get('sesiones_con_acceso', []))
                existe = any(isinstance(f, dict) and f.get('id') == sesion.id for f in filas)
                nuevo = dict(previo)
                if estado == 'acceso':
                    if not existe:
                        filas.append({'id': sesion.id, 'username': sesion.username})
                        informe['añadidas'] += 1
                    if sesion not in confirmadas[username]:
                        confirmadas[username].append(sesion)
                    if not nuevo.get('sesion_id'):
                        nuevo.update(sesion_id=sesion.id, cuenta=sesion.username, preferida_manual=False)
                else:
                    era_preferida = nuevo.get('sesion_id') == sesion.id
                    informe['eliminadas'] += int(existe or era_preferida)
                    filas = [f for f in filas if not isinstance(f, dict) or f.get('id') != sesion.id]
                    if era_preferida:
                        nuevo.update(sesion_id=None, cuenta=None, preferida_manual=False)
                        reemplazo = next((s for s in confirmadas[username]
                                          if sesiones._indisponibilidad(s) is None), None)
                        if reemplazo:
                            nuevo.update(sesion_id=reemplazo.id, cuenta=reemplazo.username)
                prioridades = {s.id: (s.reserva, s.orden_reserva)
                               for s in sesiones.listar_sesiones(incluir_deshabilitadas=True)}
                filas.sort(key=lambda f: prioridades.get(f.get('id'), (False, 0)) if isinstance(f, dict) else (False, 0))
                nuevo.update(sesiones_con_acceso=filas, asociaciones_actualizadas_en=sesiones._ahora())
                registro['perfiles'][username] = nuevo
            fila.update(estado=estado, tipo=tipo, detalle=detalle,
                        reintentable=reintentable, actualizada_en=sesiones._ahora())
            fila.setdefault('historial', []).append({'intento': fila['intentos'],
                'estado': estado, 'tipo': tipo, 'detalle': detalle, 'fecha': fila['actualizada_en']})
            guardar(registro)

    def seleccion(sesion, pasada):
        return [u for u, fila in matriz.get(sesion.id, {}).items()
                if fila['estado'] not in _DEFINITIVOS and
                (pasada == 0 or fila.get('reintentable', True))]

    informar()
    for pasada in range(max_reintentos + 1):
        candidatas = [s for s in disponibles if s.id not in bloqueadas and seleccion(s, pasada)]
        if not candidatas or parada() or informe['interrumpida']:
            break
        informe.update(pasada=pasada + 1, etapa='reintentando' if pasada else 'comprobando')
        if pasada:
            informar()
            esperar(demora_reintento)
        for sesion in candidatas:
            if parada() or informe['interrumpida']:
                break
            with sesiones.LOCK:
                motivo = ({'tipo': 'deshabilitada'} if sesion not in sesiones.listar_sesiones(incluir_pausadas=True)
                          else sesiones._indisponibilidad(sesion))
            if motivo:
                omitir(sesion, motivo['tipo'])
                informar()
                continue
            informe.update(sesion_actual=sesion.etiqueta, username_actual=None)
            error_sesion = None
            try:
                with sesiones.usar_sesion(sesion):
                    sesiones.comprobar_archivo_sesion()
                    with crear_verificador() as verificar:
                        for username in seleccion(sesion, pasada):
                            if parada():
                                break
                            fila = matriz[sesion.id][username]
                            informe.update(username_actual=username)
                            consultada = False
                            try:
                                with sesiones.LOCK:
                                    sesiones.comprobar_sesion_habilitada(sesion)
                                    fila.update(estado='pendiente', intentos=fila['intentos'] + 1,
                                                ultimo_intento_en=sesiones._ahora(), reintentable=True)
                                    informe['intentos_ejecucion'] += 1
                                    informe['reintentos_ejecucion'] += int((sesion.id, username) in intentadas or fila['intentos'] > 1)
                                    intentadas.add((sesion.id, username))
                                    consultada = True
                                    informar()
                                    token = sesiones._VERIFICACION_MANUAL.set((username, sesion.id))
                                    try:
                                        perfil = verificar(username)
                                    finally:
                                        sesiones._VERIFICACION_MANUAL.reset(token)
                                    sesiones._validar_acceso_confirmado(perfil, username, sesion)
                                    sesiones.comprobar_sesion_habilitada(sesion)
                                with sesiones.LOCK:
                                    sesiones._exito(sesion, username, False)
                                resultado(username, sesion, 'acceso')
                            except sesiones.PerfilPrivado:
                                resultado(username, sesion, 'sin_acceso', 'privado_sin_acceso', 'Acceso denegado de forma explícita.')
                            except (TimeoutError, PlaywrightTimeoutError):
                                resultado(username, sesion, 'error', 'timeout', 'La comprobación agotó el tiempo de espera.', True)
                            except sesiones.PerfilNoEncontrado:
                                resultado(username, sesion, 'error', 'perfil_no_encontrado', 'No se pudo verificar la disponibilidad del perfil.', True)
                            except sesiones.ErrorConsultaInstagram:
                                resultado(username, sesion, 'error', 'consulta_incompleta', 'La respuesta no permitió confirmar el acceso.', True)
                            except sesiones.ErrorSesionInstagram as error:
                                error_sesion = error
                                if consultada:
                                    resultado(username, sesion, 'error', error.tipo, 'La sesión no está disponible para consultar.', False)
                            informar()
                            if error_sesion:
                                break
                            esperar(demora)
            except sesiones.ErrorSesionInstagram as error:
                error_sesion = error
            except (TimeoutError, PlaywrightTimeoutError, sesiones.ErrorConsultaInstagram, sesiones.PerfilNoEncontrado):
                logger.warning('Navegador de asociaciones interrumpido: sesion=%s', sesion.id)
            finally:
                if error_sesion:
                    with sesiones.LOCK:
                        sesiones.registrar_error_sesion(sesion, error_sesion)
                    omitir(sesion, error_sesion.tipo)
                    if error_sesion.tipo == 'rate_limit' or error_sesion.http_status == 429:
                        informe.update(interrumpida=True, motivo='Instagram indicó un límite de consultas; sincronización interrumpida.')
                procesadas.add(sesion.id)
                informar()
    if sesion_esperada is None and not parada() and not informe['interrumpida']:
        informe.update(repartir_preferidas_confirmadas(confirmadas))
    resumir()
    informe.update(detenida=parada(), username_actual=None, sesion_actual=None)
    informe['etapa'] = ('detenida' if informe['detenida'] else 'interrumpida' if informe['interrumpida']
                        else 'parcial' if informe['pendientes'] else 'completada')
    informar()
    return informe

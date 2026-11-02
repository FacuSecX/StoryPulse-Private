# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Alias de sesión y migración explícita sin red ni modificación de cookies."""
from __future__ import annotations

import copy
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone

import session_pauses

ID = re.compile(r'[a-z0-9][a-z0-9_.-]{0,63}')


def validar_aliases(config):
    aliases = config.get('aliases_sesiones', {})
    filas = config.get('sesiones', [])
    if not isinstance(aliases, dict) or not isinstance(filas, list):
        raise ValueError('La configuración de alias de sesiones es inválida.')
    ids = {str(f.get('id', '')).strip().lower() for f in filas if isinstance(f, dict)}
    for alias, destino in aliases.items():
        if (not isinstance(alias, str) or not ID.fullmatch(alias)
                or not isinstance(destino, str) or not ID.fullmatch(destino)
                or alias == destino or destino in aliases or destino not in ids):
            raise ValueError('Un alias de sesión tiene un destino inválido.')
    return dict(aliases)


def _resumir_checkpoint(sync):
    pares = [f for filas in sync['comprobaciones'].values() for f in filas.values()]
    sync.update(total=len(pares), sesiones_total=len(sync['comprobaciones']),
        comprobadas=sum(f.get('intentos', 0) > 0 for f in pares),
        confirmadas=sum(f.get('estado') == 'acceso' for f in pares),
        sin_acceso=sum(f.get('estado') == 'sin_acceso' for f in pares),
        fallidas=sum(f.get('estado') == 'error' for f in pares),
        pendientes=sum(f.get('estado') not in {'acceso', 'sin_acceso'} for f in pares),
        intentos_total=sum(f.get('intentos', 0) for f in pares))


def migrar_alias(config, registro, origen, destino, *, identidad_coincide,
                 disponibles=None, fecha=None):
    """Retirar el alias conservando el estado real y los accesos del destino.

    Si los archivos guardan identidades distintas, los accesos exclusivos del
    origen quedan pendientes. Nunca se inventa acceso confirmado para destino.
    La preferida de esos perfiles se elige entre sus respaldos ya conocidos.
    """
    fecha = fecha or datetime.now(timezone.utc).isoformat()
    ahora = session_pauses.fecha_utc(fecha)
    aliases = validar_aliases(config)
    filas = {f['id']: f for f in config['sesiones']}
    if origen == destino or destino not in filas:
        raise ValueError('El destino de la migración no existe o coincide con el origen.')
    if origen not in filas:
        if aliases.get(origen) == destino:
            return copy.deepcopy(config), copy.deepcopy(registro), {'ya_migrada': True, 'perfiles_pendientes': []}
        raise ValueError('La sesión de origen no existe.')
    if origen in aliases and aliases[origen] != destino:
        raise ValueError('El origen ya tiene otro destino configurado.')
    if not isinstance(registro.get('perfiles'), dict) or not isinstance(registro.get('sesiones'), dict):
        raise ValueError('El registro de sesiones es inválido.')
    nuevo_config, nuevo = copy.deepcopy(config), copy.deepcopy(registro)
    nuevo_config['sesiones'] = [f for f in nuevo_config['sesiones'] if f['id'] != origen]
    destino_fila = next(f for f in nuevo_config['sesiones'] if f['id'] == destino)
    destino_fila['reserva'] = True
    if not destino_fila.get('orden_reserva'):
        destino_fila['orden_reserva'] = max((f.get('orden_reserva', 0) for f in nuevo_config['sesiones']), default=0) + 1
    nuevo_config.setdefault('aliases_sesiones', {}).update({origen: destino})
    for alias, target in list(nuevo_config['aliases_sesiones'].items()):
        if target == origen:
            nuevo_config['aliases_sesiones'][alias] = destino
    validar_aliases(nuevo_config)
    filas = {f['id']: f for f in nuevo_config['sesiones']}
    prioridad = {sid: (bool(f.get('reserva', False)), f.get('orden_reserva', 0)) for sid, f in filas.items()}
    disponibles = set(filas) if disponibles is None else set(disponibles) & set(filas)
    pendientes, afectados = [], set()
    para_repartir = {}
    resumen = {'ya_migrada': False, 'origen': origen, 'destino': destino,
               'preferidas_trasladadas': 0, 'respaldos_retirados': 0,
               'accesos_trasladados': 0, 'preferidas_reasignadas': 0}
    for username, perfil in nuevo['perfiles'].items():
        if not isinstance(perfil, dict) or not isinstance(perfil.get('sesiones_con_acceso', []), list):
            raise ValueError('Un vínculo de perfil es inválido.')
        accesos = perfil.get('sesiones_con_acceso', [])
        if any(not isinstance(a, dict) or not isinstance(a.get('id'), str) for a in accesos):
            raise ValueError('Un acceso del perfil es inválido.')
        tenia_origen = any(a['id'] == origen for a in accesos)
        preferida_origen = perfil.get('sesion_id') == origen
        conocida_destino = any(a['id'] == destino for a in accesos)
        if tenia_origen or preferida_origen:
            afectados.add(username)
            if not conocida_destino:
                if identidad_coincide:
                    accesos.append({'id': destino, 'username': destino_fila.get('username')})
                    resumen['accesos_trasladados'] += 1
                    conocida_destino = True
                else:
                    pendientes.append(username)
        if tenia_origen:
            resumen['respaldos_retirados'] += 1
        vistos = set()
        perfil['sesiones_con_acceso'] = [a for a in accesos if a['id'] != origen
            and not (a['id'] in vistos or vistos.add(a['id']))]
        perfil['sesiones_con_acceso'].sort(key=lambda a: prioridad.get(a['id'], (False, 0)))
        if preferida_origen:
            resumen['preferidas_trasladadas'] += 1
            perfil.update(sesion_id=destino if conocida_destino else None,
                          cuenta=destino_fila.get('username') if conocida_destino else None,
                          preferida_manual=False)
            perfil.pop('preferida_manual_actualizada', None)
        if perfil.get('ultima_sesion_exitosa') == origen:
            if identidad_coincide and conocida_destino:
                perfil['ultima_sesion_exitosa'] = destino
            else:
                perfil.pop('ultima_sesion_exitosa', None)
        opciones = [a['id'] for a in perfil['sesiones_con_acceso'] if a['id'] in disponibles]
        habituales = [sid for sid in opciones if not filas[sid].get('reserva', False)]
        actual = perfil.get('sesion_id')
        if habituales and (preferida_origen or actual in filas and filas[actual].get('reserva', False)):
            para_repartir[username] = habituales
        elif preferida_origen and (actual is None or actual not in disponibles):
            if opciones:
                para_repartir[username] = habituales or opciones
        if username in afectados:
            perfil['alias_migrado_en'] = fecha
    cargas = Counter(p.get('sesion_id') for u, p in nuevo['perfiles'].items() if u not in para_repartir)
    for username in sorted(para_repartir, key=lambda u: (len(para_repartir[u]), u)):
        perfil = nuevo['perfiles'][username]
        anterior = perfil.get('sesion_id')
        elegida = min(para_repartir[username], key=lambda sid: (cargas[sid], sid != anterior, prioridad[sid], sid))
        perfil.update(sesion_id=elegida, cuenta=filas[elegida].get('username'),
                      preferida_manual=False, preferida_balanceada=True, preferida_balanceada_en=fecha)
        cargas[elegida] += 1
        resumen['preferidas_reasignadas'] += int(elegida != anterior)
    nuevo['sesiones'].pop(origen, None)
    nuevo.get('actualizaciones_sesiones', {}).pop(origen, None)
    pausas = nuevo.get('pausas_sesiones', {})
    session_pauses.validar_pausas(pausas)
    anterior_pausa = pausas.pop(origen, None)
    if anterior_pausa and session_pauses.pausa_vigente({origen: anterior_pausa}, origen, ahora=ahora):
        actual = session_pauses.pausa_vigente(pausas, destino, ahora=ahora)
        if (actual is None or anterior_pausa['reanudar_en'] is None
                or actual['reanudar_en'] is not None and
                session_pauses.fecha_utc(anterior_pausa['reanudar_en']) > session_pauses.fecha_utc(actual['reanudar_en'])):
            pausas[destino] = anterior_pausa
    sync = nuevo.get('sincronizacion_asociaciones')
    if isinstance(sync, dict) and sync.get('version') == 2:
        sync.get('comprobaciones', {}).pop(origen, None)
        sync.get('sesiones_plan', {}).pop(origen, None)
        sync.get('reparto_preferidas', {}).pop(origen, None)
        sync['sesiones_omitidas'] = [s for s in sync.get('sesiones_omitidas', []) if s.get('id') != origen]
    elif pendientes:
        if sync is not None:
            nuevo['sincronizacion_anterior_migracion'] = copy.deepcopy(sync)
        sync = {'version': 2, 'id': uuid.uuid4().hex,
                'objetivos': [], 'sesiones_plan': {}, 'comprobaciones': {},
                'añadidas': 0, 'eliminadas': 0, 'sesiones_procesadas': 0,
                'sesiones_omitidas': [], 'detenida': False, 'interrumpida': False,
                'motivo': 'Accesos del alias antiguo pendientes de confirmar con la sesión canónica.'}
        nuevo['sincronizacion_asociaciones'] = sync
    if isinstance(sync, dict) and sync.get('version') == 2:
        if pendientes:
            sync.setdefault('sesiones_plan', {})[destino] = destino_fila.get('username')
            pares = sync.setdefault('comprobaciones', {}).setdefault(destino, {})
            for u in pendientes:
                pares[u] = {'estado': 'pendiente', 'intentos': 0, 'reintentable': True,
                            'tipo': 'alias_sin_confirmar', 'actualizada_en': fecha}
                for acceso in nuevo['perfiles'][u]['sesiones_con_acceso']:
                    sid = acceso['id']
                    if sid not in filas or sid == destino:
                        continue
                    sync['sesiones_plan'][sid] = filas[sid].get('username')
                    sync['comprobaciones'].setdefault(sid, {}).setdefault(u,
                        {'estado': 'acceso', 'intentos': 1, 'origen': 'asociacion_guardada',
                         'actualizada_en': fecha, 'reintentable': False})
            sync['objetivos'] = list(dict.fromkeys(sync.get('objetivos', []) + pendientes))
        sync.update(username_actual=None, sesion_actual=None, actualizada_en=fecha)
        _resumir_checkpoint(sync)
        sync['etapa'] = 'parcial' if sync['pendientes'] else 'completada'
    resumen.update(perfiles_afectados=len(afectados), perfiles_pendientes=pendientes,
                   sesiones_finales=len(nuevo_config['sesiones']))
    nuevo.setdefault('migraciones_aliases_sesiones', []).append({**resumen, 'fecha': fecha})
    return nuevo_config, nuevo, resumen


def migrar_rotaciones(conn, origen, destino):
    """Cambiar únicamente IDs de sesión; horarios, estado y antirrepetición quedan intactos."""
    tablas = {f[0] for f in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    cambiadas = 0
    if 'programaciones' in tablas:
        columnas = {f[1] for f in conn.execute('PRAGMA table_info(programaciones)')}
        if 'sesiones_rotacion_json' in columnas:
            cursor = conn.execute('SELECT rowid,sesiones_rotacion_json FROM programaciones')
            for clave, raw in cursor.fetchall():
                valores = json.loads(raw or '[]')
                if not isinstance(valores, list) or any(not isinstance(v, str) for v in valores):
                    raise ValueError('Una programación tiene sesiones de rotación inválidas.')
                nuevos = list(dict.fromkeys(destino if v == origen else v for v in valores))
                if nuevos != valores:
                    conn.execute('UPDATE programaciones SET sesiones_rotacion_json=? WHERE rowid=?',
                                 (json.dumps(nuevos, ensure_ascii=False), clave))
                    cambiadas += 1
        if 'ultima_sesion_rotacion' in columnas:
            conn.execute('UPDATE programaciones SET ultima_sesion_rotacion=? WHERE ultima_sesion_rotacion=?', (destino, origen))
    if 'rotaciones_programacion_grupo' in tablas:
        conn.execute('UPDATE rotaciones_programacion_grupo SET ultima_sesion=? WHERE ultima_sesion=?', (destino, origen))
    return cambiadas

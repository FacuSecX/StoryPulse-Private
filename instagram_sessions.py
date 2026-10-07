# StoryPulse v2.0
# Created by FacuSecX https://github.com/FacuSecX/StoryPulse-Private



from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent


def _cargar_entorno_sesiones_sin_dotenv(ruta: Path) -> None:
    """Configuración básica para el exportador, que sólo necesita Playwright.

    El bot completo sigue usando python-dotenv de requirements.txt. Aquí sólo
    se leen las rutas de sesiones, sin importar sus dependencias.
    """
    if not ruta.exists():
        return
    claves = {
        "INSTAGRAM_STORAGE_STATE", "INSTAGRAM_SESSIONS_CONFIG",
        "INSTAGRAM_SESSIONS_REGISTRY",
    }
    for linea in ruta.read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*", linea)
        if match is None or match.group(1) not in claves:
            continue
        nombre, valor = match.groups()
        if nombre in os.environ:
            continue
        if valor.startswith(("'", '"')):
            citado = re.fullmatch(r"(['\"])(.*)\1(?:\s*#.*)?", valor)
            if citado is None:
                raise RuntimeError(f"Valor entre comillas inválido para {nombre} en .env.")
            valor = citado.group(2)
        else:
            valor = re.split(r"\s+#", valor, maxsplit=1)[0].strip()
        # No usar una ruta incorrecta si la configuración requiere interpolación.
        if "${" in valor:
            raise RuntimeError(
                f"La variable {nombre} usa interpolación en .env. "
                "Instalá python-dotenv con: python -m pip install python-dotenv"
            )
        os.environ[nombre] = valor


try:
    from dotenv import load_dotenv
except ModuleNotFoundError as error:
    if error.name != "dotenv":
        raise
    _cargar_entorno_sesiones_sin_dotenv(BASE_DIR / ".env")
else:
    load_dotenv(BASE_DIR / ".env")

LOCK = threading.RLock()
logger = logging.getLogger("storypulse.sesiones")
_PLAYWRIGHT_DEL_HILO = threading.local()


@contextmanager
def usar_playwright_sincronico(iniciar: Callable[[], Any]):
    """Un solo ciclo de Playwright por hilo durante una operación anidada.

    Telegram ya ejecuta las consultas mediante asyncio.to_thread. Dentro de
    ese hilo, una consulta vacía puede necesitar comprobar el perfil; iniciar
    otro sync_playwright mientras el primero vive provoca el error de asyncio.
    Reutilizar la instancia permite que cada consulta cree y cierre sus propios
    navegadores/contextos sin detener el ciclo de la operación exterior.
    """
    activo = getattr(_PLAYWRIGHT_DEL_HILO, "activo", None)
    if activo is not None:
        yield activo
        return
    with iniciar() as playwright:
        _PLAYWRIGHT_DEL_HILO.activo = playwright
        try:
            yield playwright
        finally:
            del _PLAYWRIGHT_DEL_HILO.activo


class SinHistoriasDisponibles(RuntimeError):
    pass


class PerfilPrivado(RuntimeError):
    pass


class PerfilNoEncontrado(RuntimeError):
    pass


class ConfiguracionSesionesError(RuntimeError):
    pass


class ErrorConsultaInstagram(RuntimeError):
    """Respuesta remota incompleta/inesperada; otra sesión puede funcionar."""


class ErrorSesionInstagram(RuntimeError):
    def __init__(self, tipo: str, detalle: str, *, http_status: int | None = None):
        self.tipo = tipo
        self.http_status = http_status
        super().__init__(detalle)


class SesionesAgotadas(RuntimeError):
    def __init__(self, username: str, fallos: list[dict[str, str]]):
        self.username = username
        self.fallos = fallos
        self.tipos = {fallo["tipo"] for fallo in fallos}
        detalle = "\n".join(
            f"• {fallo['sesion']}: {fallo['detalle']}" for fallo in fallos
        )
        super().__init__(
            f"Ninguna sesión disponible pudo consultar @{username}.\n{detalle}"
        )


@dataclass(frozen=True)
class SesionInstagram:
    id: str
    username: str | None
    archivo: Path

    @property
    def etiqueta(self) -> str:
        return f"@{self.username} ({self.id})" if self.username else self.id


_ACTUAL: ContextVar[SesionInstagram | None] = ContextVar("sesion_instagram", default=None)
_VERIFICACION_MANUAL: ContextVar[tuple[str, str] | None] = ContextVar(
    "verificacion_manual_instagram", default=None)


def _ruta_env(nombre: str, defecto: str) -> Path:
    ruta = Path(os.getenv(nombre, defecto).strip()).expanduser()
    return ruta if ruta.is_absolute() else BASE_DIR / ruta


def ruta_configuracion() -> Path:
    return _ruta_env("INSTAGRAM_SESSIONS_CONFIG", "sesiones_instagram.json")


def ruta_registro() -> Path:
    return _ruta_env("INSTAGRAM_SESSIONS_REGISTRY", "estado_sesiones_instagram.json")


def _leer_json(ruta: Path) -> Any:
    try:
        return json.loads(ruta.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ConfiguracionSesionesError(f"No se puede leer el JSON {ruta.name}.") from error


def escribir_json_atomico(ruta: Path, contenido: Any) -> None:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, nombre = tempfile.mkstemp(prefix=f".{ruta.name}.", suffix=".tmp", dir=ruta.parent)
    temporal = Path(nombre)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(contenido, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporal.chmod(0o600)
        os.replace(temporal, ruta)
    finally:
        temporal.unlink(missing_ok=True)


def listar_sesiones(*, incluir_deshabilitadas: bool = False) -> list[SesionInstagram]:
    ruta = ruta_configuracion()
    if not ruta.exists():
        filas = [{"id": "principal", "archivo": None}]
    else:
        config = _leer_json(ruta)
        if not isinstance(config, dict) or config.get("version") != 1:
            raise ConfiguracionSesionesError("sesiones_instagram.json necesita version=1.")
        filas = config.get("sesiones")
        if not isinstance(filas, list):
            raise ConfiguracionSesionesError("La configuración necesita una lista sesiones.")

    resultado = []
    ids: set[str] = set()
    archivos: set[str] = set()
    for fila in filas:
        if not isinstance(fila, dict):
            raise ConfiguracionSesionesError("Cada sesión debe ser un objeto JSON.")
        habilitada = fila.get("habilitada", True)
        if not isinstance(habilitada, bool):
            raise ConfiguracionSesionesError("habilitada debe ser true o false.")
        identificador = str(fila.get("id", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", identificador):
            raise ConfiguracionSesionesError("ID de sesión inválido.")
        if identificador in ids:
            raise ConfiguracionSesionesError(f"ID de sesión duplicado: {identificador}.")
        ids.add(identificador)
        username = str(fila.get("username") or "").strip().lstrip("@").lower() or None
        if username and not re.fullmatch(r"[a-z0-9._]{1,30}", username):
            raise ConfiguracionSesionesError(f"Username inválido en la sesión {identificador}.")
        archivo = fila.get("archivo")
        if archivo is None:
            ruta_state = _ruta_env("INSTAGRAM_STORAGE_STATE", "instagram_state.json")
        elif isinstance(archivo, str) and archivo.strip():
            ruta_state = Path(archivo).expanduser()
            if not ruta_state.is_absolute():
                ruta_state = BASE_DIR / ruta_state
        else:
            raise ConfiguracionSesionesError(f"Archivo inválido en la sesión {identificador}.")
        clave_archivo = os.path.normcase(str(ruta_state.resolve()))
        if clave_archivo in archivos:
            raise ConfiguracionSesionesError("Dos sesiones apuntan al mismo archivo de cookies.")
        archivos.add(clave_archivo)
        if habilitada or incluir_deshabilitadas:
            resultado.append(SesionInstagram(identificador, username, ruta_state))
    if not resultado and not incluir_deshabilitadas:
        raise ConfiguracionSesionesError("No hay sesiones de Instagram habilitadas.")
    return resultado


def sesion_actual() -> SesionInstagram | None:
    return _ACTUAL.get()


def obtener_ruta_sesion() -> Path:
    actual = sesion_actual()
    return actual.archivo if actual else listar_sesiones()[0].archivo


@contextmanager
def usar_sesion(sesion: SesionInstagram):
    token = _ACTUAL.set(sesion)
    try:
        yield
    finally:
        _ACTUAL.reset(token)


def comprobar_archivo_sesion() -> dict[str, Any]:
    ruta = obtener_ruta_sesion()
    try:
        data = json.loads(ruta.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ErrorSesionInstagram("archivo", f"Archivo de sesión ausente o inválido: {ruta.name}.") from error
    cookies = data.get("cookies") if isinstance(data, dict) else None
    if not isinstance(cookies, list):
        raise ErrorSesionInstagram("archivo", f"Formato de sesión inválido: {ruta.name}.")
    cookies_ig = [c for c in cookies if isinstance(c, dict) and
                  str(c.get("domain", "")).lstrip(".").lower() in
                  ("instagram.com", "www.instagram.com")]
    cookie = next((c for c in cookies_ig if c.get("name") == "sessionid" and c.get("value")), None)
    if cookie is None:
        raise ErrorSesionInstagram("archivo", f"{ruta.name} no contiene sessionid de Instagram.")
    expires = cookie.get("expires", -1)
    if isinstance(expires, (int, float)) and expires > 0 and expires <= time.time():
        raise ErrorSesionInstagram("login", f"La cookie de sesión venció en {ruta.name}.")
    return {"session_file": str(ruta.resolve()), "sessionid_presente": True,
            "dominios": sorted({str(c.get("domain", "")) for c in cookies_ig})}


def guardar_estado_contexto(context) -> None:
    # Pedir el estado en memoria evita un temporal compartido entre cuentas.
    try:
        estado = context.storage_state(indexed_db=True)
    except TypeError:
        estado = context.storage_state()
    escribir_json_atomico(obtener_ruta_sesion(), estado)


def _tipo_verificacion(valor: Any) -> str | None:
    # Sólo campos de errores: nunca buscar "challenge" en contenido del perfil.
    if not isinstance(valor, dict):
        return None
    textos = [str(valor.get(k, "")) for k in
              ("message", "error_type", "error", "checkpoint_url", "challenge_url")]
    if valor.get("challenge") or valor.get("checkpoint_url"):
        return "verificacion"
    texto = " ".join(textos).lower()
    if any(x in texto for x in ("challenge_required", "checkpoint_required", "captcha", "consent_required")):
        return "verificacion"
    if "login_required" in texto or "not logged in" in texto:
        return "login"
    if any(x in texto for x in ("feedback_required", "rate limit", "too many requests", "please wait a few minutes")):
        return "rate_limit"
    for error in valor.get("errors", []) if isinstance(valor.get("errors"), list) else []:
        tipo = _tipo_verificacion(error)
        if tipo:
            return tipo
    return None


def validar_datos_instagram(data: Any) -> None:
    tipo = _tipo_verificacion(data)
    if tipo:
        mensajes = {"verificacion": "Instagram requiere verificación/CAPTCHA manual.",
                    "login": "Instagram requiere iniciar sesión nuevamente.",
                    "rate_limit": "Instagram indicó rate limit / límite temporal."}
        raise ErrorSesionInstagram(tipo, mensajes[tipo])


def validar_pagina_instagram(page) -> None:
    url = str(page.url).lower()
    if "/accounts/login" in url:
        raise ErrorSesionInstagram("login", "Instagram redirigió al login.")
    if any(ruta in url for ruta in ("/challenge", "/checkpoint", "/auth_platform/",
                                    "/accounts/confirm_email", "/accounts/confirm_phone")):
        raise ErrorSesionInstagram("verificacion", "Instagram abrió una verificación/CAPTCHA manual.")


def validar_respuesta_instagram(page, response, operacion: str) -> None:
    validar_pagina_instagram(page)
    if response is None:
        raise ErrorConsultaInstagram(f"Instagram no devolvió respuesta al {operacion}.")
    status = int(response.status)
    # El cuerpo puede indicar challenge_required incluso con HTTP 200 o 400.
    if status >= 400:
        try:
            data = response.json()
        except Exception:
            data = None
        validar_datos_instagram(data)
        tipos = {400: "http_400", 401: "login", 403: "rechazada", 429: "rate_limit"}
        tipo = tipos.get(status)
        if tipo:
            raise ErrorSesionInstagram(tipo, f"Instagram respondió HTTP {status} al {operacion}.", http_status=status)
        if status == 404:
            raise PerfilNoEncontrado("Instagram no encontró el perfil solicitado (HTTP 404).")
        raise ErrorConsultaInstagram(f"Instagram respondió HTTP {status} al {operacion}.")


def _huella(sesion: SesionInstagram) -> str:
    try:
        stat = sesion.archivo.stat()
        digest = hashlib.sha256(sesion.archivo.read_bytes()).hexdigest()
        return f"{stat.st_mtime_ns}:{digest}"
    except OSError:
        return "ausente"


def _registro() -> dict[str, Any]:
    ruta = ruta_registro()
    if not ruta.exists():
        return {"version": 1, "perfiles": {}, "sesiones": {}}
    registro = _leer_json(ruta)
    if (not isinstance(registro, dict) or registro.get("version") != 1 or
            not isinstance(registro.get("perfiles"), dict) or
            not isinstance(registro.get("sesiones"), dict)):
        raise ConfiguracionSesionesError("Registro de sesiones inválido; revisar estado_sesiones_instagram.json.")
    # Retirar los temporizadores de versiones anteriores, preservando accesos
    # y requisitos reales de renovación. Ningún error HTTP fija una espera.
    cambios = "pausa_preventiva" in registro
    registro.pop("pausa_preventiva", None)
    for estado in registro["sesiones"].values():
        if isinstance(estado, dict) and "suspendida_hasta" in estado:
            estado.pop("suspendida_hasta")
            cambios = True
    if cambios:
        escribir_json_atomico(ruta, registro)
    return registro


def obtener_vinculo(username: str) -> dict[str, Any] | None:
    with LOCK:
        return _registro()["perfiles"].get(username.lower().lstrip("@"))


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exito(sesion: SesionInstagram, username: str, vincular: bool, *,
           conservar_preferida: bool = False) -> None:
    registro = _registro()
    registro["sesiones"][sesion.id] = {"huella": _huella(sesion), "ultimo_exito": _ahora(), "tipo": "disponible"}
    if vincular:
        anterior = registro["perfiles"].get(username, {})
        mantener_preferida = (conservar_preferida or anterior.get("preferida_manual") is True) and anterior.get("sesion_id")
        registro["perfiles"][username] = {
            **anterior,
            "sesion_id": anterior["sesion_id"] if mantener_preferida else sesion.id,
            "cuenta": anterior.get("cuenta") if mantener_preferida else sesion.username,
            "ultimo_exito": _ahora(), "ultima_sesion_exitosa": sesion.id,
            "sesiones_con_acceso": anterior.get("sesiones_con_acceso", []),
        }
        logger.info("Consulta de @%s exitosa con sesión %s", username, sesion.etiqueta)
    escribir_json_atomico(ruta_registro(), registro)


def registrar_sesion_disponible(sesion: SesionInstagram) -> None:
    """Limpia un error anterior después de verificar el feed autenticado."""
    with LOCK:
        _exito(sesion, "", False)


def registrar_error_sesion(sesion: SesionInstagram, error: ErrorSesionInstagram) -> None:
    """Guardar el error; sólo login/CAPTCHA/archivo inválido requieren renovación."""
    estado = {"tipo": error.tipo, "huella": _huella(sesion), "ultimo_error": _ahora()}
    if error.http_status is not None:
        estado["http_status"] = error.http_status
    if error.tipo in ("verificacion", "login", "archivo"):
        estado["requiere_renovar"] = True
    registro = _registro()
    registro["sesiones"][sesion.id] = estado
    escribir_json_atomico(ruta_registro(), registro)
    logger.warning("Error en sesión %s: %s", sesion.etiqueta, error.tipo)


# Compatibilidad con utilidades anteriores: ya no crea ninguna suspensión.
_suspender = registrar_error_sesion


def _indisponibilidad(sesion: SesionInstagram) -> dict[str, str] | None:
    estado = _registro()["sesiones"].get(sesion.id, {})
    if estado.get("huella") != _huella(sesion):
        return None  # Un JSON reemplazado permite reintentar automáticamente.
    tipo = estado.get("tipo", "disponible")
    if estado.get("requiere_renovar"):
        detalle = "requiere renovar el archivo de sesión"
    else:
        return None
    return {"sesion": sesion.etiqueta, "tipo": tipo, "detalle": detalle}


def ejecutar_con_sesiones(username: str, operacion: Callable[[], Any], *,
                          verificar_todas: bool = False, vincular: bool = True,
                          orden_sesion_ids: list[str] | None = None,
                          conservar_preferida: bool = False) -> Any:
    username = username.strip().lower().lstrip("@")
    with LOCK:
        registro_previo = _registro()
        vinculo = registro_previo["perfiles"].get(username)
        perfil_vinculado = username in registro_previo["perfiles"]
        preferida = vinculo.get("sesion_id") if isinstance(vinculo, dict) else None
        autorizadas = set()
        acceso_previo = vinculo.get("sesiones_con_acceso", []) if isinstance(vinculo, dict) else []
        if isinstance(preferida, str) and preferida:
            autorizadas.add(preferida)
        if isinstance(acceso_previo, list):
            autorizadas.update(fila["id"] for fila in acceso_previo
                               if isinstance(fila, dict) and isinstance(fila.get("id"), str) and fila["id"])
        orden = None
        if orden_sesion_ids is not None:
            if not isinstance(orden_sesion_ids, list) or any(
                    not isinstance(ident, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", ident)
                    for ident in orden_sesion_ids):
                raise ConfiguracionSesionesError("El orden de sesiones necesita una lista de IDs válidos.")
            orden = list(orden_sesion_ids)
            if len(orden) != len(set(orden)):
                raise ConfiguracionSesionesError("El orden de sesiones no puede contener IDs repetidos.")
            if not orden or not perfil_vinculado or any(ident not in autorizadas for ident in orden):
                raise SesionesAgotadas(username, [{"sesion": "sesiones elegidas", "tipo": "vinculo",
                    "detalle": "el orden debe incluir únicamente sesiones con acceso registrado al perfil"}])
        actual = sesion_actual()
        comprobacion_manual = _VERIFICACION_MANUAL.get()
        if comprobacion_manual is not None and (
                actual is None or comprobacion_manual != (username, actual.id)):
            raise SesionesAgotadas(username, [{"sesion": "comprobación manual", "tipo": "vinculo",
                "detalle": "sólo se puede comprobar el perfil y la sesión elegidos"}])
        if actual is not None:
            if orden is not None and actual.id not in orden:
                raise SesionesAgotadas(username, [{"sesion": actual.etiqueta, "tipo": "vinculo",
                    "detalle": "la sesión actual no pertenece al orden elegido"}])
            if perfil_vinculado and actual.id not in autorizadas and comprobacion_manual is None:
                raise SesionesAgotadas(username, [{"sesion": actual.etiqueta, "tipo": "vinculo",
                    "detalle": "esta sesión no tiene acceso registrado a este perfil"}])
            return operacion()  # Las llamadas anidadas conservan la cuenta autorizada.
        sesiones = listar_sesiones()
        if orden is not None:
            por_id = {sesion.id: sesion for sesion in sesiones}
            sesiones = [por_id[ident] for ident in orden if ident in por_id]
            if not sesiones:
                raise SesionesAgotadas(username, [{"sesion": "sesiones elegidas", "tipo": "vinculo",
                    "detalle": "ninguna sesión del orden elegido está habilitada"}])
        elif perfil_vinculado:
            sesiones = [s for s in sesiones if s.id in autorizadas]
            if not sesiones:
                raise SesionesAgotadas(username, [{"sesion": "sesiones autorizadas", "tipo": "vinculo",
                    "detalle": "no hay sesiones habilitadas con acceso registrado a este perfil"}])
        if orden is None:
            sesiones.sort(key=lambda s: s.id != preferida)
        explorar_todas = verificar_todas and not perfil_vinculado
        fallos = []
        exitos: list[tuple[SesionInstagram, Any]] = []
        verificadas = 0
        for sesion in sesiones:
            indisponible = _indisponibilidad(sesion)
            if indisponible:
                fallos.append(indisponible)
                continue
            verificadas += 1
            try:
                with usar_sesion(sesion):
                    comprobar_archivo_sesion()
                    resultado = operacion()
            except SinHistoriasDisponibles:
                _exito(sesion, username, vincular, conservar_preferida=conservar_preferida)
                raise  # Vacío y acceso confirmado: no hay motivo para rotar.
            except (PerfilPrivado, PerfilNoEncontrado) as error:
                tipo = "privado" if isinstance(error, PerfilPrivado) else "no_encontrado"
                detalle = "perfil privado sin acceso" if tipo == "privado" else "perfil no encontrado"
                fallos.append({"sesion": sesion.etiqueta, "tipo": tipo, "detalle": detalle})
            except ErrorSesionInstagram as error:
                registrar_error_sesion(sesion, error)
                detalle = str(error)  # Sólo errores construidos localmente, sin cuerpos/cookies.
                fallos.append({"sesion": sesion.etiqueta, "tipo": error.tipo, "detalle": detalle})
            except ErrorConsultaInstagram as error:
                fallos.append({"sesion": sesion.etiqueta, "tipo": "consulta", "detalle": str(error)[:240]})
            except Exception as error:
                # Fallos de disco/código se propagan; cambiar de cuenta no los arregla.
                if error.__class__.__name__ != "TimeoutError":
                    raise
                fallos.append({"sesion": sesion.etiqueta, "tipo": "timeout", "detalle": "tiempo de espera agotado"})
            else:
                _exito(sesion, username, vincular and not verificar_todas,
                       conservar_preferida=conservar_preferida)
                exitos.append((sesion, resultado))
                if not verificar_todas:
                    return resultado
                if not explorar_todas:
                    break  # Un vínculo existente se valida con la primera sesión autorizada que funciona.

        if exitos:
            elegida, resultado = exitos[0]
            _exito(elegida, username, vincular, conservar_preferida=conservar_preferida)
            acceso = [{"id": sesion.id, "username": sesion.username} for sesion, _ in exitos]
            if perfil_vinculado and isinstance(acceso_previo, list):
                observadas = {fila["id"] for fila in acceso}
                acceso += [fila for fila in acceso_previo if isinstance(fila, dict)
                           and isinstance(fila.get("id"), str) and fila["id"] not in observadas]
            if vincular:
                registro = _registro()
                registro["perfiles"][username]["sesiones_con_acceso"] = acceso
                escribir_json_atomico(ruta_registro(), registro)
            return {**resultado, "sesion_id": elegida.id, "sesion_username": elegida.username,
                    "sesiones_con_acceso": acceso, "sesiones_verificadas": verificadas,
                    "sesiones_con_error": fallos}
        if fallos and all(fallo["tipo"] == "privado" for fallo in fallos):
            raise PerfilPrivado(f"Ninguna de las {len(sesiones)} sesiones tiene acceso a @{username}.")
        if fallos and all(fallo["tipo"] == "no_encontrado" for fallo in fallos):
            raise PerfilNoEncontrado(f"No se encontró @{username} con ninguna sesión.")
        raise SesionesAgotadas(username, fallos)


def _normalizar_perfil_explicito(username: str) -> str:
    normalizado = str(username).strip()
    match = re.search(r"instagram\.com/(?:stories/)?([^/?#]+)", normalizado, re.I)
    if match:
        normalizado = match.group(1)
    username = normalizado.lstrip("@").strip("/").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", username):
        raise ValueError("Username inválido.")
    return username


def _validar_acceso_confirmado(perfil: Any, username: str, sesion: SesionInstagram) -> None:
    if not isinstance(perfil, dict):
        raise ErrorConsultaInstagram("No se confirmó el acceso al perfil.")
    validar_datos_instagram(perfil)
    if perfil.get("is_private") is True and perfil.get("following") is not True:
        raise PerfilPrivado(f"La sesión elegida no tiene acceso a @{username}.")
    if perfil.get("acceso_confirmado") is not True:
        raise ErrorConsultaInstagram("No se confirmó el acceso al perfil.")
    if "username" in perfil and str(perfil["username"]).lower().lstrip("@") != username:
        raise ErrorConsultaInstagram("La comprobación devolvió otro perfil.")
    if "sesion_id" in perfil and perfil["sesion_id"] != sesion.id:
        raise ErrorConsultaInstagram("La comprobación devolvió otra sesión.")


def verificar_accesos_perfil(username: str,
                            verificar_acceso: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Comprobar expresamente el perfil con todas las cuentas habilitadas.

    El callback consulta sólo el perfil y su relación con cada sesión, sin
    descargar Stories. La lista sesiones_confirmadas contiene únicamente los
    éxitos de esta comprobación; los accesos anteriores y la preferida se
    conservan en el registro. Los errores nunca autorizan una cuenta nueva.
    """
    username = _normalizar_perfil_explicito(username)
    with LOCK:
        if _VERIFICACION_MANUAL.get() is not None or sesion_actual() is not None:
            raise ConfiguracionSesionesError("La comprobación debe iniciarse fuera de una consulta activa.")
        sesiones = listar_sesiones()
        registro = _registro()
        anterior = registro["perfiles"].get(username, {})
        if not isinstance(anterior, dict) or not isinstance(anterior.get("sesiones_con_acceso", []), list):
            raise ConfiguracionSesionesError("El vínculo de este perfil es inválido.")
        exitos: list[tuple[SesionInstagram, dict[str, Any]]] = []
        fallos: list[dict[str, str]] = []
        verificadas = 0
        for sesion in sesiones:
            if not isinstance(_registro()["sesiones"].get(sesion.id, {}), dict):
                raise ConfiguracionSesionesError("El estado de la sesión elegida es inválido.")
            indisponible = _indisponibilidad(sesion)
            if indisponible:
                fallos.append(indisponible)
                continue
            verificadas += 1
            try:
                with usar_sesion(sesion):
                    comprobar_archivo_sesion()
                    token = _VERIFICACION_MANUAL.set((username, sesion.id))
                    try:
                        perfil = verificar_acceso()
                    finally:
                        _VERIFICACION_MANUAL.reset(token)
                    _validar_acceso_confirmado(perfil, username, sesion)
                    if not any(actual == sesion for actual in listar_sesiones()):
                        raise ConfiguracionSesionesError("La sesión elegida cambió durante la comprobación.")
                    comprobar_archivo_sesion()
            except (PerfilPrivado, PerfilNoEncontrado) as error:
                tipo = "privado" if isinstance(error, PerfilPrivado) else "no_encontrado"
                detalle = "perfil privado sin acceso" if tipo == "privado" else "perfil no encontrado"
                fallos.append({"sesion": sesion.etiqueta, "tipo": tipo, "detalle": detalle})
            except ErrorSesionInstagram as error:
                registrar_error_sesion(sesion, error)
                fallos.append({"sesion": sesion.etiqueta, "tipo": error.tipo, "detalle": str(error)})
            except ErrorConsultaInstagram as error:
                fallos.append({"sesion": sesion.etiqueta, "tipo": "consulta", "detalle": str(error)[:240]})
            except Exception as error:
                if error.__class__.__name__ != "TimeoutError":
                    raise
                fallos.append({"sesion": sesion.etiqueta, "tipo": "timeout", "detalle": "tiempo de espera agotado"})
            else:
                _exito(sesion, username, False)
                exitos.append((sesion, perfil))

        if not exitos:
            if fallos and all(fallo["tipo"] == "privado" for fallo in fallos):
                raise PerfilPrivado(f"Ninguna sesión tiene acceso a @{username}.")
            raise SesionesAgotadas(username, fallos)

        habilitadas = listar_sesiones()
        if any(sesion not in habilitadas for sesion, _ in exitos):
            raise ConfiguracionSesionesError("Una sesión confirmada cambió durante la comprobación.")
        registro = _registro()
        anterior = registro["perfiles"].get(username, {})
        if not isinstance(anterior, dict) or not isinstance(anterior.get("sesiones_con_acceso", []), list):
            raise ConfiguracionSesionesError("El vínculo de este perfil es inválido.")
        accesos = list(anterior.get("sesiones_con_acceso", []))
        confirmadas = [{"id": sesion.id, "username": sesion.username} for sesion, _ in exitos]
        for fila in confirmadas:
            if not any(isinstance(previa, dict) and previa.get("id") == fila["id"] for previa in accesos):
                accesos.append(fila)
        primera, perfil = exitos[0]
        ahora = _ahora()
        vinculo = {**anterior, "sesiones_con_acceso": accesos,
                   "ultimo_exito": ahora, "ultima_sesion_exitosa": exitos[-1][0].id}
        if not anterior.get("sesion_id"):
            vinculo.update(sesion_id=primera.id, cuenta=primera.username)
        registro["perfiles"][username] = vinculo
        escribir_json_atomico(ruta_registro(), registro)
        return {**perfil, "sesion_id": primera.id, "sesion_username": primera.username,
                "sesiones_confirmadas": confirmadas, "sesiones_con_error": fallos,
                "sesiones_verificadas": verificadas}


_ACTUALIZACIONES_EN_CURSO: set[str] = set()


def listar_sesiones_nuevas() -> list[SesionInstagram]:
    """Cuentas sin asociaciones y comprobaciones parciales que pueden continuar.

    La preferida legacy también es una asociación, aun sin una lista de
    sesiones_con_acceso. Un lote interrumpido puede tener ya algunos accesos;
    se mantiene visible para terminar solamente sus perfiles pendientes.
    """
    with LOCK:
        registro = _registro()
        asociadas: set[str] = set()
        for vinculo in registro["perfiles"].values():
            if not isinstance(vinculo, dict):
                raise ConfiguracionSesionesError("El vínculo de un perfil es inválido.")
            preferida = vinculo.get("sesion_id")
            if isinstance(preferida, str) and preferida:
                asociadas.add(preferida)
            accesos = vinculo.get("sesiones_con_acceso", [])
            if not isinstance(accesos, list):
                raise ConfiguracionSesionesError("La lista de accesos de un perfil es inválida.")
            asociadas.update(fila["id"] for fila in accesos if isinstance(fila, dict)
                             and isinstance(fila.get("id"), str) and fila["id"])
        lotes = registro.get("actualizaciones_sesiones", {})
        if not isinstance(lotes, dict):
            raise ConfiguracionSesionesError("El registro de actualización de sesiones es inválido.")
        return [sesion for sesion in listar_sesiones() if sesion.id not in asociadas or
                isinstance(lotes.get(sesion.id), dict) and
                lotes[sesion.id].get("completada") is False]


def actualizar_accesos_sesion(sesion_id: str, usernames: list[str],
                              verificar_acceso: Callable[[str], dict[str, Any]], *,
                              sesion_esperada: SesionInstagram | None = None,
                              progreso: Callable[[dict[str, Any]], None] | None = None,
                              demora_entre_perfiles: float = 1.0) -> dict[str, Any]:
    """Añadir una sola cuenta como respaldo sin modificar ninguna preferida.

    El callback debe comprobar el perfil y sus permisos, sin consultar Stories.
    Sólo se ejecuta bajo la cuenta elegida; los resolvers anidados conservan
    esa cuenta. Se guardan los éxitos uno a uno. Tras una interrupción se
    reutilizan las comprobaciones definitivas y se retoman las pendientes.
    Login, verificación, archivo inválido, rechazo HTTP y rate limit detienen el lote sin
    fijar suspensiones ni temporizadores de indisponibilidad.
    """
    if not isinstance(sesion_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_.-]{0,63}", sesion_id.strip().lower()):
        raise ConfiguracionSesionesError("ID de sesión inválido.")
    sesion_id = sesion_id.strip().lower()
    if not isinstance(usernames, list):
        raise ValueError("La actualización necesita una lista de perfiles.")
    objetivos = list(dict.fromkeys(_normalizar_perfil_explicito(username) for username in usernames))
    if (not isinstance(demora_entre_perfiles, (int, float)) or
            isinstance(demora_entre_perfiles, bool) or not 0 <= demora_entre_perfiles <= 10):
        raise ValueError("La separación entre comprobaciones debe estar entre 0 y 10 segundos.")

    with LOCK:
        if sesion_actual() is not None or _VERIFICACION_MANUAL.get() is not None:
            raise ConfiguracionSesionesError("La actualización debe iniciarse fuera de una consulta activa.")
        elegida = next((sesion for sesion in listar_sesiones() if sesion.id == sesion_id), None)
        if elegida is None:
            raise ConfiguracionSesionesError("La sesión elegida no existe o está deshabilitada.")
        if sesion_esperada is not None and elegida != sesion_esperada:
            raise ConfiguracionSesionesError("La sesión elegida cambió desde que se abrió el menú.")
        if sesion_id in _ACTUALIZACIONES_EN_CURSO:
            raise ConfiguracionSesionesError("Esta sesión ya tiene una actualización en curso.")
        if not any(sesion == elegida for sesion in listar_sesiones_nuevas()):
            raise ConfiguracionSesionesError("La sesión ya está asociada y no tiene una actualización pendiente.")
        registro = _registro()
        lotes = registro.setdefault("actualizaciones_sesiones", {})
        anterior = lotes.get(sesion_id, {})
        anteriores = (anterior.get("perfiles", {}) if isinstance(anterior, dict)
                      and anterior.get("completada") is False else {})
        if not isinstance(anteriores, dict):
            raise ConfiguracionSesionesError("Las comprobaciones guardadas de la sesión son inválidas.")
        resultados: dict[str, dict[str, str]] = {
            username: dict(anteriores[username]) for username in objetivos
            if isinstance(anteriores.get(username), dict) and
            anteriores[username].get("tipo") in ("acceso", "privado", "no_encontrado")}
        _ACTUALIZACIONES_EN_CURSO.add(sesion_id)

    informe: dict[str, Any] = {
        "sesion_id": elegida.id, "sesion_username": elegida.username,
        "total": len(objetivos), "interrumpida": False, "motivo": "",
        "username_actual": None, "etapa": "preparando"}

    def guardar_y_informar() -> None:
        informe.update(
            procesados=len(resultados),
            añadidos=sum(fila["tipo"] == "acceso" for fila in resultados.values()),
            sin_acceso=sum(fila["tipo"] in ("privado", "no_encontrado") for fila in resultados.values()),
            fallidas=sum(fila["tipo"] not in ("acceso", "privado", "no_encontrado")
                         for fila in resultados.values()),
            pendientes=max(0, len(objetivos) - sum(
                fila["tipo"] in ("acceso", "privado", "no_encontrado") for fila in resultados.values())),
            sin_revisar=max(0, len(objetivos) - len(resultados)),
            perfiles=[dict(resultados[username]) for username in objetivos if username in resultados])
        with LOCK:
            registro = _registro()
            registro.setdefault("actualizaciones_sesiones", {})[sesion_id] = {
                "username": elegida.username, "actualizada_en": _ahora(),
                "completada": informe["etapa"] == "completada",
                "interrumpida": informe["interrumpida"], "motivo": informe["motivo"],
                "objetivos": objetivos, "perfiles": {key: dict(value) for key, value in resultados.items()}}
            escribir_json_atomico(ruta_registro(), registro)
        if progreso is not None:
            try:
                progreso({**informe, "perfiles": [dict(fila) for fila in informe["perfiles"]]})
            except Exception as error:
                logger.warning("No se pudo informar el progreso de sesiones: %s", error.__class__.__name__)

    try:
        guardar_y_informar()
        for username in objetivos:
            if username in resultados:
                continue
            informe.update(username_actual=username, etapa="verificando")
            guardar_y_informar()
            with LOCK:
                if not any(sesion == elegida for sesion in listar_sesiones()):
                    raise ConfiguracionSesionesError("La sesión elegida cambió durante la actualización.")
                try:
                    with usar_sesion(elegida):
                        comprobar_archivo_sesion()
                        token = _VERIFICACION_MANUAL.set((username, elegida.id))
                        try:
                            perfil = verificar_acceso(username)
                        finally:
                            _VERIFICACION_MANUAL.reset(token)
                        _validar_acceso_confirmado(perfil, username, elegida)
                        if not any(sesion == elegida for sesion in listar_sesiones()):
                            raise ConfiguracionSesionesError("La sesión elegida cambió durante la comprobación.")
                        comprobar_archivo_sesion()
                except (PerfilPrivado, PerfilNoEncontrado) as error:
                    tipo = "privado" if isinstance(error, PerfilPrivado) else "no_encontrado"
                    detalle = "perfil privado sin acceso" if tipo == "privado" else "perfil no encontrado"
                    resultados[username] = {"username": username, "tipo": tipo, "detalle": detalle}
                except ErrorSesionInstagram as error:
                    registrar_error_sesion(elegida, error)
                    resultados[username] = {"username": username, "tipo": error.tipo, "detalle": str(error)[:240]}
                    if (error.tipo in ("archivo", "login", "verificacion", "rate_limit", "http_400", "rechazada")
                            or error.http_status in (400, 401, 403, 429)):
                        informe.update(interrumpida=True, motivo=str(error)[:240], etapa="interrumpida")
                except ErrorConsultaInstagram as error:
                    resultados[username] = {"username": username, "tipo": "consulta", "detalle": str(error)[:240]}
                except Exception as error:
                    if error.__class__.__name__ != "TimeoutError":
                        raise
                    resultados[username] = {"username": username, "tipo": "timeout", "detalle": "tiempo de espera agotado"}
                else:
                    registro = _registro()
                    vinculo = registro["perfiles"].get(username, {})
                    if not isinstance(vinculo, dict) or not isinstance(vinculo.get("sesiones_con_acceso", []), list):
                        raise ConfiguracionSesionesError("El vínculo de este perfil es inválido.")
                    accesos = list(vinculo.get("sesiones_con_acceso", []))
                    if not any(isinstance(fila, dict) and fila.get("id") == elegida.id for fila in accesos):
                        accesos.append({"id": elegida.id, "username": elegida.username})
                    registro["perfiles"][username] = {**vinculo, "sesiones_con_acceso": accesos}
                    escribir_json_atomico(ruta_registro(), registro)
                    _exito(elegida, username, False)
                    resultados[username] = {"username": username, "tipo": "acceso", "detalle": "añadida como respaldo"}
            guardar_y_informar()
            if informe["interrumpida"]:
                break
            if demora_entre_perfiles and len(resultados) < len(objetivos):
                time.sleep(demora_entre_perfiles)
        if not informe["interrumpida"]:
            # Las respuestas incompletas o timeouts pueden reintentarse sin
            # repetir los accesos y denegaciones ya confirmados.
            hay_errores = any(fila["tipo"] not in ("acceso", "privado", "no_encontrado")
                             for fila in resultados.values())
            informe.update(username_actual=None, etapa="parcial" if hay_errores else "completada")
        guardar_y_informar()
        return {**informe, "perfiles": [dict(fila) for fila in informe["perfiles"]]}
    finally:
        with LOCK:
            _ACTUALIZACIONES_EN_CURSO.discard(sesion_id)


def cambiar_sesion_preferida(username: str, sesion_id: str,
                            verificar_acceso: Callable[[], dict[str, Any]], *,
                            sesion_esperada: SesionInstagram | None = None) -> dict[str, Any]:
    """Elegir una cuenta sin ampliar accesos hasta confirmar esa pareja exacta.

    Una cuenta con acceso registrado sólo necesita validación local. Para una
    cuenta nueva, el callback se ejecuta una vez bajo la sesión elegida y debe
    devolver acceso_confirmado=True. Los resolvers anidados del mismo perfil
    pueden usar esa cuenta; las consultas a otros perfiles siguen bloqueadas.
    Los errores conservan la preferida y todos los accesos anteriores.
    """
    username = _normalizar_perfil_explicito(username)
    if not isinstance(sesion_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_.-]{0,63}", sesion_id.strip().lower()):
        raise ConfiguracionSesionesError("ID de sesión inválido.")
    sesion_id = sesion_id.strip().lower()

    with LOCK:
        if _VERIFICACION_MANUAL.get() is not None:
            raise ConfiguracionSesionesError("Ya hay una comprobación manual en curso.")
        sesiones = listar_sesiones()
        elegida = next((sesion for sesion in sesiones if sesion.id == sesion_id), None)
        if elegida is None:
            raise ConfiguracionSesionesError("La sesión elegida no existe o está deshabilitada.")
        if sesion_esperada is not None and elegida != sesion_esperada:
            raise ConfiguracionSesionesError("La sesión elegida cambió desde que se abrió el menú.")
        registro = _registro()
        anterior = registro["perfiles"].get(username, {})
        if not isinstance(anterior, dict):
            raise ConfiguracionSesionesError("El vínculo de este perfil es inválido.")
        accesos = anterior.get("sesiones_con_acceso", [])
        if not isinstance(accesos, list):
            raise ConfiguracionSesionesError("La lista de accesos de este perfil es inválida.")
        estado = registro["sesiones"].get(elegida.id, {})
        if not isinstance(estado, dict):
            raise ConfiguracionSesionesError("El estado de la sesión elegida es inválido.")
        conocida = anterior.get("sesion_id") == elegida.id or any(
            isinstance(fila, dict) and fila.get("id") == elegida.id for fila in accesos)

        try:
            with usar_sesion(elegida):
                comprobar_archivo_sesion()
                indisponible = _indisponibilidad(elegida)
                if indisponible:
                    raise ErrorSesionInstagram(indisponible["tipo"],
                        f"La sesión {elegida.etiqueta} requiere renovar su archivo.")
                if not conocida:
                    token = _VERIFICACION_MANUAL.set((username, elegida.id))
                    try:
                        perfil = verificar_acceso()
                    finally:
                        _VERIFICACION_MANUAL.reset(token)
                    _validar_acceso_confirmado(perfil, username, elegida)
                # El verificador puede renovar sus cookies; validar el archivo
                # final y una configuración que pudo cambiar durante la consulta.
                if not any(sesion == elegida for sesion in listar_sesiones()):
                    raise ConfiguracionSesionesError("La sesión elegida cambió durante la comprobación.")
                comprobar_archivo_sesion()
        except ErrorSesionInstagram as error:
            registrar_error_sesion(elegida, error)
            raise

        # Conservar cada fila autorizada y su metadata; añadir únicamente las
        # cuentas que ya estaban autorizadas o acaban de confirmar acceso.
        accesos_finales = list(accesos)
        anterior_id = anterior.get("sesion_id")
        if isinstance(anterior_id, str) and anterior_id and not any(
                isinstance(fila, dict) and fila.get("id") == anterior_id for fila in accesos_finales):
            anterior_sesion = next((sesion for sesion in sesiones if sesion.id == anterior_id), None)
            accesos_finales.append({"id": anterior_id, "username": anterior.get("cuenta") or
                (anterior_sesion.username if anterior_sesion else None)})
        if not any(isinstance(fila, dict) and fila.get("id") == elegida.id for fila in accesos_finales):
            accesos_finales.append({"id": elegida.id, "username": elegida.username})
        ahora = _ahora()
        vinculo = {**anterior, "sesion_id": elegida.id, "cuenta": elegida.username,
                   "sesiones_con_acceso": accesos_finales,
                   "preferida_manual": True, "preferida_manual_actualizada": ahora}
        if not conocida:
            vinculo.update({"ultimo_exito": ahora, "ultima_sesion_exitosa": elegida.id})
            registro["sesiones"][elegida.id] = {
                "huella": _huella(elegida), "ultimo_exito": ahora, "tipo": "disponible"}
        registro["perfiles"][username] = vinculo
        escribir_json_atomico(ruta_registro(), registro)
        logger.info("Sesión preferida manual de @%s: %s", username, elegida.etiqueta)
        return vinculo


def con_sesiones(*, verificar_todas: bool = False, vincular: bool = True):
    def decorar(funcion):
        @wraps(funcion)
        def ejecutar(username: str, *args, **kwargs):
            # Normaliza antes de consultar: URLs y @ deben compartir vínculo.
            normalizado = str(username).strip()
            match = re.search(r"instagram\.com/(?:stories/)?([^/?#]+)", normalizado, re.I)
            if match:
                normalizado = match.group(1)
            normalizado = normalizado.lstrip("@").strip("/").lower()
            if not re.fullmatch(r"[a-z0-9._]+", normalizado):
                raise ValueError("Username inválido.")
            return ejecutar_con_sesiones(normalizado,
                lambda: funcion(normalizado, *args, **kwargs),
                verificar_todas=verificar_todas, vincular=vincular)
        return ejecutar
    return decorar


def registrar_sesion(identificador: str, username: str | None, archivo: Path) -> None:
    with LOCK:
        ruta = ruta_configuracion()
        config = _leer_json(ruta) if ruta.exists() else {
            "version": 1, "sesiones": [{"id": "principal", "username": None, "archivo": None, "habilitada": True}]
        }
        # Valida el contenido existente antes de modificarlo.
        listar_sesiones(incluir_deshabilitadas=True)
        try:
            archivo_relativo = archivo.resolve().relative_to(BASE_DIR).as_posix()
        except ValueError:
            archivo_relativo = str(archivo.resolve())
        fila = {"id": identificador, "username": username,
                "archivo": archivo_relativo, "habilitada": True}
        existentes = config["sesiones"]
        for indice, anterior in enumerate(existentes):
            if anterior["id"] == identificador:
                existentes[indice] = fila
                break
        else:
            existentes.append(fila)
        escribir_json_atomico(ruta, config)


def estado_sesiones_local() -> list[dict[str, Any]]:
    with LOCK:
        resultado = []
        for sesion in listar_sesiones():
            indisponible = _indisponibilidad(sesion)
            error = None
            try:
                with usar_sesion(sesion):
                    comprobar_archivo_sesion()
            except ErrorSesionInstagram as exc:
                error = str(exc)
            ultimo = _registro()["sesiones"].get(sesion.id, {})
            ultimo_tipo = ultimo.get("tipo") if isinstance(ultimo, dict) else None
            if ultimo_tipo == "disponible" or ultimo.get("huella") != _huella(sesion):
                ultimo_tipo = None
            detalle = error or (indisponible["detalle"] if indisponible else
                               "Archivo de sesión válido; sin comprobación remota.")
            errores = {"rate_limit": "Instagram rechazó la última consulta por límite de solicitudes (HTTP 429).",
                       "http_400": "Instagram rechazó la última consulta (HTTP 400).",
                       "rechazada": "Instagram rechazó la última consulta (HTTP 403)."}
            if not error and not indisponible and ultimo_tipo in errores:
                detalle += "\n" + errores[ultimo_tipo]
            resultado.append({"id": sesion.id, "username": sesion.username,
                              "etiqueta": sesion.etiqueta,
                              "disponible": indisponible is None and error is None,
                              "ultimo_error_tipo": ultimo_tipo,
                              "detalle": detalle})
        return resultado

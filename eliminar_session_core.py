# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Limpieza transaccional de una sesión. Sólo biblioteca estándar; sin red."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from pathlib import Path

ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
TX_RE = re.compile(r"[a-f0-9]{32}")


def leer_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise RuntimeError(f"No se pudo leer {Path(path).name}; no se eliminará nada.") from None


def guardar_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, nombre = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(nombre, 0o600)
        os.replace(nombre, path)
    finally:
        Path(nombre).unlink(missing_ok=True)


def enlace(path):
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) &
                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
    except FileNotFoundError:
        return False


def ruta_segura(base, path):
    """Rechaza enlaces/junctions y comprueba el destino absoluto antes de mover."""
    base = Path(base).resolve()
    path = Path(path)
    path = path if path.is_absolute() else base / path
    if ".." in path.parts:
        raise RuntimeError("Una ruta contiene '..'; no se eliminará nada.")
    actual = path
    while actual != base and actual != actual.parent:
        if enlace(actual):
            raise RuntimeError("Una ruta usa un enlace o junction; no se eliminará nada.")
        actual = actual.parent
    resolved = path.resolve()
    if resolved == base or not resolved.is_relative_to(base):
        raise RuntimeError("Una ruta sale del proyecto; no se eliminará nada.")
    return resolved


def comprobar_arbol(base, path):
    path = ruta_segura(base, path)
    if path.is_dir():
        for root, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                ruta_segura(base, Path(root) / name)
    return path


def uid_archivo(path):
    try:
        data = leer_json(path)
    except RuntimeError:
        return None
    cookies = data.get("cookies", []) if isinstance(data, dict) else []
    if not isinstance(cookies, list):
        return None
    return next((str(c.get("value")) for c in cookies if isinstance(c, dict)
                 and c.get("name") == "ds_user_id"
                 and str(c.get("value", "")).isdigit()
                 and str(c.get("domain", "")).lstrip(".").lower() in
                 ("instagram.com", "www.instagram.com")), None)


class Proyecto:
    def __init__(self, base, config=None, registro=None, state=None, db=None):
        self.base = Path(base).resolve()
        self.config = ruta_segura(self.base, config or "sesiones_instagram.json")
        self.registro = ruta_segura(self.base, registro or "estado_sesiones_instagram.json")
        self.state = ruta_segura(self.base, state or "instagram_state.json")
        self.db = ruta_segura(self.base, db or "bot_historias.db")
        if len({self.config, self.registro, self.state, self.db}) != 4:
            raise RuntimeError("Las rutas de configuración se superponen.")
        self.root = ruta_segura(self.base, ".eliminacion_session")
        self.lock = ruta_segura(self.base, ".eliminar_session.lock")

    def relativa(self, path):
        return ruta_segura(self.base, path).relative_to(self.base).as_posix()

    def crear_root(self):
        ruta_segura(self.base, self.root).mkdir(mode=0o700, exist_ok=True)
        ignore = ruta_segura(self.base, self.root / ".gitignore")
        if not ignore.exists():
            descriptor = os.open(ignore, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write("*\n")

    def configuracion(self):
        if self.config.exists():
            data = leer_json(self.config)
        else:
            data = {"version": 1, "sesiones": [
                {"id": "principal", "username": None, "archivo": None, "habilitada": True}]}
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("sesiones"), list):
            raise RuntimeError("La configuración de sesiones es inválida.")
        ids, paths = set(), set()
        for row in data["sesiones"]:
            if not isinstance(row, dict):
                raise RuntimeError("La configuración contiene una fila inválida.")
            ident = str(row.get("id", "")).strip().lower()
            username = row.get("username")
            if (not ID_RE.fullmatch(ident) or ident in ids or
                    not isinstance(row.get("habilitada", True), bool) or
                    (username is not None and (not isinstance(username, str) or
                     not re.fullmatch(r"[a-zA-Z0-9._]{1,30}", username)))):
                raise RuntimeError("La configuración contiene sesiones inválidas o duplicadas.")
            path = self.archivo_fila(row)
            if path in paths or path in (self.config, self.registro, self.db):
                raise RuntimeError("La configuración contiene archivos superpuestos.")
            ids.add(ident)
            paths.add(path)
        return data

    def archivo_fila(self, row):
        value = row.get("archivo")
        if value is None:
            return self.state
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("Una sesión tiene una ruta inválida.")
        path = ruta_segura(self.base, value)
        folder = ruta_segura(self.base, "sesiones_instagram")
        if path.suffix.lower() != ".json" or (path != self.state and not path.is_relative_to(folder)):
            raise RuntimeError("Una sesión apunta fuera de sesiones_instagram; requiere revisión manual.")
        return path

    def registro_actual(self):
        if not self.registro.exists():
            return {"version": 1, "perfiles": {}, "sesiones": {}}
        data = leer_json(self.registro)
        if (not isinstance(data, dict) or data.get("version") != 1 or
                not isinstance(data.get("perfiles"), dict) or
                not isinstance(data.get("sesiones"), dict) or
                not isinstance(data.get("actualizaciones_sesiones", {}), dict)):
            raise RuntimeError("El registro de vínculos es inválido.")
        for row in data["perfiles"].values():
            if not isinstance(row, dict) or not isinstance(row.get("sesiones_con_acceso", []), list):
                raise RuntimeError("Un vínculo de perfil es inválido.")
            if any(not isinstance(r, dict) or not isinstance(r.get("id"), str)
                   for r in row.get("sesiones_con_acceso", [])):
                raise RuntimeError("Una lista de respaldos es inválida.")
        return data

    def inventario(self):
        config = self.configuracion()
        result, paths = {}, set()
        for row in config["sesiones"]:
            ident = row["id"].strip().lower()
            path = self.archivo_fila(row)
            paths.add(path)
            result[ident] = {"id": ident, "username": row.get("username"),
                "archivo": self.relativa(path), "existe": path.is_file(),
                "habilitada": row.get("habilitada", True), "registrada": True,
                "uid": uid_archivo(path) if path.is_file() else None}
        folder = ruta_segura(self.base, "sesiones_instagram")
        for path in sorted(folder.glob("*.json")):
            path = ruta_segura(self.base, path)
            if path in paths or not path.is_file():
                continue
            ident = path.stem.lower()
            if not ID_RE.fullmatch(ident) or ident in result:
                raise RuntimeError("Un JSON sin registrar tiene un nombre ambiguo; requiere revisión manual.")
            result[ident] = {"id": ident, "username": ident, "archivo": self.relativa(path),
                "existe": True, "habilitada": False, "registrada": False, "uid": uid_archivo(path)}
        registry = self.registro_actual()
        refs = set(registry["sesiones"]) | set(registry.get("actualizaciones_sesiones", {})) | set(registry.get("pausas_sesiones", {}))
        for row in registry["perfiles"].values():
            refs.update(r["id"] for r in row.get("sesiones_con_acceso", []))
            refs.update(row.get(key) for key in ("sesion_id", "ultima_sesion_exitosa") if row.get(key))
        for ident in refs:
            if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
                raise RuntimeError("El registro contiene un identificador de sesión inválido.")
            result.setdefault(ident, {"id": ident, "username": None, "archivo": None,
                "existe": False, "habilitada": False, "registrada": False, "uid": None})
        schedules, cursors = self.filas_db()
        for row in schedules:
            try:
                refs_db = json.loads(row.get("sesiones_rotacion_json", "[]"))
            except (TypeError, ValueError):
                raise RuntimeError("Una programación tiene una rotación inválida.") from None
            if not isinstance(refs_db, list):
                raise RuntimeError("Una programación tiene una rotación inválida.")
            refs_db = refs_db + ([row["ultima_sesion_rotacion"]] if row.get("ultima_sesion_rotacion") else [])
            for ident in refs_db:
                if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
                    raise RuntimeError("Una programación contiene un ID de sesión inválido.")
                result.setdefault(ident, {"id": ident, "username": None, "archivo": None,
                    "existe": False, "habilitada": False, "registrada": False, "uid": None})
        for row in cursors:
            ident = row.get("ultima_sesion")
            if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
                raise RuntimeError("Una rotación contiene un ID de sesión inválido.")
            result.setdefault(ident, {"id": ident, "username": None, "archivo": None,
                "existe": False, "habilitada": False, "registrada": False, "uid": None})
        return sorted(result.values(), key=lambda r: r["id"])

    def filas_db(self):
        if not self.db.exists():
            return [], []
        with closing(sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=15)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            schedules = ([dict(r) for r in conn.execute("SELECT * FROM programaciones ORDER BY chat_id, username")]
                         if "programaciones" in tables else [])
            cursors = ([dict(r) for r in conn.execute("SELECT * FROM rotaciones_programacion_grupo ORDER BY chat_id, grupo_id, username")]
                       if "rotaciones_programacion_grupo" in tables else [])
        return schedules, cursors

    def token(self, ident):
        files = [self.config, self.registro]
        chosen = next((r for r in self.inventario() if r["id"] == ident), None)
        if chosen and chosen["archivo"]:
            files.append(self.base / chosen["archivo"])
        data = {self.relativa(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None for p in files}
        schedules, cursors = self.filas_db()
        data["programaciones"] = [{k: r.get(k) for k in ("chat_id", "username", "activa", "grupo_id",
            "modalidad_sesiones", "sesiones_rotacion_json", "ultima_sesion_rotacion")} for r in schedules]
        data["rotaciones"] = cursors
        return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def plan(self, ident):
        if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
            raise RuntimeError("El ID de sesión es inválido.")
        inventory = self.inventario()
        target = next((r for r in inventory if r["id"] == ident), None)
        config = self.configuracion()
        registro = self.registro_actual()
        if target is None:
            return {"presente": False, "id": ident, "token": self.token(ident)}
        nuevas = copy.deepcopy(config)
        nuevas["sesiones"] = [r for r in nuevas["sesiones"] if r["id"].strip().lower() != ident]
        if isinstance(nuevas.get('aliases_sesiones'), dict):
            nuevas['aliases_sesiones'] = {alias: destino for alias, destino in nuevas['aliases_sesiones'].items()
                                          if alias != ident and destino != ident}
        enabled = {r["id"] for r in inventory if r["id"] != ident and r["registrada"] and r["habilitada"] and r["existe"]}
        actualizado = copy.deepcopy(registro)
        actualizado["sesiones"].pop(ident, None)
        actualizado.get("actualizaciones_sesiones", {}).pop(ident, None)
        actualizado.get("pausas_sesiones", {}).pop(ident, None)
        sincronizacion = actualizado.get("sincronizacion_asociaciones", {})
        if isinstance(sincronizacion, dict):
            for campo in ("comprobaciones", "sesiones_plan", "reparto_preferidas"):
                if isinstance(sincronizacion.get(campo), dict):
                    sincronizacion[campo].pop(ident, None)
            sincronizacion["sesiones_omitidas"] = [f for f in sincronizacion.get("sesiones_omitidas", [])
                                                    if f.get("id") != ident]
            if isinstance(sincronizacion.get("comprobaciones"), dict):
                pares = [f for filas in sincronizacion["comprobaciones"].values() for f in filas.values()]
                sincronizacion.update(total=len(pares),
                    sesiones_total=len(sincronizacion["comprobaciones"]),
                    comprobadas=sum(f.get("intentos", 0) > 0 for f in pares),
                    confirmadas=sum(f.get("estado") == "acceso" for f in pares),
                    sin_acceso=sum(f.get("estado") == "sin_acceso" for f in pares),
                    fallidas=sum(f.get("estado") == "error" for f in pares),
                    pendientes=sum(f.get("estado") not in {"acceso", "sin_acceso"} for f in pares),
                    intentos_total=sum(f.get("intentos", 0) for f in pares))
        affected, sin_acceso, preferidas = [], [], []
        for username, row in actualizado["perfiles"].items():
            original = registro["perfiles"][username]
            backup = any(r["id"] == ident for r in row.get("sesiones_con_acceso", []))
            principal = row.get("sesion_id") == ident
            if not (backup or principal or row.get("ultima_sesion_exitosa") == ident):
                continue
            affected.append(username)
            row["sesiones_con_acceso"] = [r for r in row.get("sesiones_con_acceso", []) if r["id"] != ident]
            if row.get("ultima_sesion_exitosa") == ident:
                row.pop("ultima_sesion_exitosa", None)
            if principal:
                for key in ("sesion_id", "cuenta", "preferida_manual", "preferida_manual_actualizada"):
                    row.pop(key, None)
                replacement = next((r for r in row["sesiones_con_acceso"] if r["id"] in enabled), None)
                if replacement:
                    row.update(sesion_id=replacement["id"], cuenta=replacement.get("username"))
                preferidas.append({"perfil": username, "nueva": replacement["id"] if replacement else None})
            autorizadas = {r["id"] for r in row["sesiones_con_acceso"]}
            if row.get("sesion_id"):
                autorizadas.add(row["sesion_id"])
            if (backup or principal) and not (autorizadas & enabled):
                sin_acceso.append(username)
            if original == row:
                continue
        schedules, cursors = self.filas_db()
        updates, pausadas = [], []
        for row in schedules:
            changes = {}
            if "sesiones_rotacion_json" in row:
                try:
                    rotation = json.loads(row["sesiones_rotacion_json"])
                except (TypeError, ValueError):
                    raise RuntimeError("Una programación tiene una rotación inválida.") from None
                if not isinstance(rotation, list) or any(not isinstance(s, str) for s in rotation):
                    raise RuntimeError("Una programación tiene una rotación inválida.")
                if ident in rotation:
                    rotation = [s for s in rotation if s != ident]
                    changes["sesiones_rotacion_json"] = json.dumps(rotation, ensure_ascii=False)
                    if row.get("modalidad_sesiones") == "variable" and not rotation:
                        changes["activa"] = 0
            if row.get("ultima_sesion_rotacion") == ident:
                changes["ultima_sesion_rotacion"] = None
            if row.get("grupo_id") is None and row["username"] in sin_acceso:
                changes["activa"] = 0
            if not enabled:
                changes["activa"] = 0
            if changes:
                updates.append({"chat_id": row["chat_id"], "username": row["username"], "cambios": changes})
                if changes.get("activa") == 0 and row.get("activa"):
                    pausadas.append(row["username"])
        remove = []
        if target["archivo"] and target["existe"]:
            remove.append(target["archivo"])
            renewal = ruta_segura(self.base, self.base / ".renovacion_backup" / Path(target["archivo"]).name)
            if renewal.is_file() and target["uid"] and uid_archivo(renewal) == target["uid"]:
                remove.append(self.relativa(renewal))
        def nombres(row):
            values = (row.get("username"), row.get("id"), Path(row["archivo"]).stem if row.get("archivo") else None)
            return {v for v in values if isinstance(v, str) and re.fullmatch(r"[a-z0-9._]{1,30}", v)}
        candidates = {n + suffix for n in nombres(target) for suffix in ("", "_chromium", "_chrome", "_msedge", "_edge")}
        shared = {n + suffix for r in inventory if r["id"] != ident for n in nombres(r)
                  for suffix in ("", "_chromium", "_chrome", "_msedge", "_edge")}
        perfiles, omitidos = [], []
        if target.get("archivo") == self.relativa(self.state) and (self.base / "perfil_instagram_playwright").is_dir():
            omitidos.append("perfil_instagram_playwright")
        folder = ruta_segura(self.base, "perfiles_instagram")
        for name in sorted(candidates):
            path = ruta_segura(self.base, folder / name)
            if not path.is_dir():
                continue
            if name in shared:
                omitidos.append(name)
                continue
            comprobar_arbol(self.base, path)
            perfiles.append(self.relativa(path))
            remove.append(self.relativa(path))
        writes = {}
        if config != nuevas:
            writes[self.relativa(self.config)] = nuevas
        if registro != actualizado:
            writes[self.relativa(self.registro)] = actualizado
        return {"presente": True, "id": ident, "target": target, "token": self.token(ident),
            "json": writes, "retirar": remove, "db_updates": updates,
            "db_cursors": [r for r in cursors if r.get("ultima_sesion") == ident],
            "resumen": {"perfiles_afectados": len(set(affected)),
                "respaldos": sum(any(r["id"] == ident for r in p.get("sesiones_con_acceso", [])) for p in registro["perfiles"].values()),
                "preferidas": preferidas, "sin_acceso": sin_acceso,
                "programaciones_pausadas": pausadas, "programaciones_actualizadas": len(updates),
                "perfiles_navegador": perfiles, "perfiles_compartidos_conservados": omitidos,
                "sesiones_habilitadas_restantes": len(enabled)}}

    def resumen(self, ident):
        plan = self.plan(ident)
        return {k: plan[k] for k in ("presente", "id", "token", "target", "resumen") if k in plan}

    def tx(self, txid):
        if not TX_RE.fullmatch(str(txid)):
            raise RuntimeError("El ID de operación es inválido.")
        return ruta_segura(self.base, self.root / txid)

    def pendientes(self):
        if not self.root.exists():
            return []
        return sorted(p.name for p in self.root.iterdir() if TX_RE.fullmatch(p.name) and p.is_dir())

    def estado(self, txid):
        folder = self.tx(txid)
        return leer_json(folder / "estado.json") if folder.exists() else {"fase": "ausente"}

    def preparar(self, ident, txid, expected, extra=None):
        folder = self.tx(txid)
        if folder.exists():
            state = self.estado(txid)
            if state.get("id") != ident:
                raise RuntimeError("La operación pendiente pertenece a otra sesión.")
            return state
        if self.token(ident) != expected:
            raise RuntimeError("La configuración cambió desde la confirmación. Volvé a ejecutar el BAT.")
        plan = self.plan(ident)
        if not plan["presente"]:
            return {"fase": "ausente"}
        self.crear_root()
        try:
            self.lock.mkdir(mode=0o700)
        except FileExistsError:
            raise RuntimeError("Hay otra eliminación pendiente; primero debe recuperarse.") from None
        guardar_json(self.lock / "owner.json", {"tx": txid})
        folder.mkdir(mode=0o700)
        state = {"id": ident, "fase": "preparando", "plan": plan, "copias": [], "extra": extra or {}}
        guardar_json(folder / "estado.json", state)
        try:
            snapshots = list(plan["json"])
            if plan["db_updates"] or plan["db_cursors"]:
                snapshots.append(self.relativa(self.db))
            for relative in snapshots:
                source = ruta_segura(self.base, relative)
                dest = ruta_segura(self.base, folder / "copias" / relative)
                dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                if source.exists():
                    if source == self.db:
                        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
                            with closing(sqlite3.connect(dest)) as dst:
                                src.backup(dst)
                        os.chmod(dest, 0o600)
                    else:
                        shutil.copy2(source, dest)
                        os.chmod(dest, 0o600)
                state["copias"].append({"ruta": relative, "existia": source.exists()})
                guardar_json(folder / "estado.json", state)
            state["fase"] = "preparada"
            guardar_json(folder / "estado.json", state)
            return state
        except BaseException:
            self.restaurar(txid)
            raise

    def aplicar(self, txid):
        folder = self.tx(txid)
        state = self.estado(txid)
        if state["fase"] == "aplicada":
            self.verificar(txid)
            return state
        if state["fase"] != "preparada":
            raise RuntimeError("La operación no está preparada.")
        plan = state["plan"]
        if self.token(state["id"]) != plan["token"]:
            raise RuntimeError("El proyecto cambió durante la eliminación; debe recuperarse.")
        state["fase"] = "aplicando"
        guardar_json(folder / "estado.json", state)
        try:
            for relative in plan["retirar"]:
                source = comprobar_arbol(self.base, self.base / relative)
                dest = ruta_segura(self.base, folder / "retirados" / relative)
                dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                os.replace(source, dest)
            for relative, data in plan["json"].items():
                guardar_json(ruta_segura(self.base, relative), data)
            if plan["db_updates"] or plan["db_cursors"]:
                with closing(sqlite3.connect(self.db, timeout=15)) as conn, conn:
                    conn.execute("BEGIN IMMEDIATE")
                    for row in plan["db_updates"]:
                        keys = list(row["cambios"])
                        allowed = {"activa", "sesiones_rotacion_json", "ultima_sesion_rotacion"}
                        if not set(keys) <= allowed:
                            raise RuntimeError("La operación contiene columnas no permitidas.")
                        cursor = conn.execute("UPDATE programaciones SET " + ", ".join(k + " = ?" for k in keys) +
                            " WHERE chat_id = ? AND username = ?",
                            [row["cambios"][k] for k in keys] + [row["chat_id"], row["username"]])
                        if cursor.rowcount != 1:
                            raise RuntimeError("Una programación cambió durante la eliminación.")
                    conn.execute("DELETE FROM rotaciones_programacion_grupo WHERE ultima_sesion = ?", (state["id"],)) if plan["db_cursors"] else None
            state["fase"] = "aplicada"
            guardar_json(folder / "estado.json", state)
            self.verificar(txid)
            return state
        except BaseException:
            self.restaurar(txid)
            raise

    def verificar(self, txid):
        state = self.estado(txid)
        if state["fase"] != "aplicada":
            raise RuntimeError("La eliminación no está aplicada.")
        ident = state["id"]
        if any(r["id"] == ident for r in self.configuracion()["sesiones"]):
            raise RuntimeError("La sesión continúa en la configuración.")
        registry = self.registro_actual()
        if ident in registry["sesiones"] or ident in registry.get("actualizaciones_sesiones", {}) or ident in registry.get("pausas_sesiones", {}):
            raise RuntimeError("Quedaron referencias al estado de la sesión.")
        sync = registry.get("sincronizacion_asociaciones", {})
        if ident in sync.get("comprobaciones", {}) or ident in sync.get("sesiones_plan", {}):
            raise RuntimeError("Quedaron comprobaciones pendientes de la sesión.")
        for row in registry["perfiles"].values():
            if (row.get("sesion_id") == ident or row.get("ultima_sesion_exitosa") == ident or
                    any(r["id"] == ident for r in row.get("sesiones_con_acceso", []))):
                raise RuntimeError("Quedaron referencias en los respaldos de un perfil.")
        for relative in state["plan"]["retirar"]:
            if (self.base / relative).exists():
                raise RuntimeError("Un archivo retirado volvió a aparecer.")
        schedules, cursors = self.filas_db()
        if any(r.get("ultima_sesion_rotacion") == ident or ident in json.loads(r.get("sesiones_rotacion_json", "[]")) for r in schedules):
            raise RuntimeError("Quedaron referencias en las programaciones.")
        if any(r.get("ultima_sesion") == ident for r in cursors):
            raise RuntimeError("Quedaron referencias de rotaciones anteriores.")
        return state["plan"]["resumen"]

    def restaurar(self, txid):
        folder = self.tx(txid)
        if not folder.exists():
            return
        state = self.estado(txid)
        if self.lock.exists() and leer_json(self.lock / "owner.json").get("finalizando"):
            raise RuntimeError("La operación ya está confirmada; sólo resta limpiar sus copias temporales.")
        if state["fase"] in ("aplicando", "aplicada"):
            for row in state["copias"]:
                source = ruta_segura(self.base, folder / "copias" / row["ruta"])
                dest = ruta_segura(self.base, row["ruta"])
                if not row["existia"]:
                    dest.unlink(missing_ok=True)
                elif dest == self.db:
                    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
                        with closing(sqlite3.connect(dest)) as dst:
                            src.backup(dst)
                            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                else:
                    descriptor, name = tempfile.mkstemp(prefix=".restaurar-", dir=dest.parent)
                    os.close(descriptor)
                    try:
                        shutil.copy2(source, name)
                        os.replace(name, dest)
                    finally:
                        Path(name).unlink(missing_ok=True)
            for relative in reversed(state["plan"]["retirar"]):
                source = ruta_segura(self.base, folder / "retirados" / relative)
                dest = ruta_segura(self.base, relative)
                if source.exists():
                    if dest.exists():
                        raise RuntimeError("No se pudo recuperar: reapareció un archivo o perfil. Se conserva el respaldo.")
                    comprobar_arbol(self.base, source)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, dest)
        state["fase"] = "restaurada"
        guardar_json(folder / "estado.json", state)

    def finalizar(self, txid, restaurada=False):
        folder = self.tx(txid)
        owner = leer_json(self.lock / "owner.json") if self.lock.exists() else {}
        if owner and owner.get("tx") != txid:
            raise RuntimeError("La operación no es propietaria del bloqueo.")
        reanudando = owner.get("finalizando", False)
        if reanudando and owner.get("restaurada") != restaurada:
            raise RuntimeError("La decisión de la operación ya fue confirmada.")
        if folder.exists():
            if not reanudando:
                state = self.estado(txid)
                if restaurada:
                    if state["fase"] != "restaurada":
                        raise RuntimeError("La recuperación todavía no terminó.")
                else:
                    self.verificar(txid)
                guardar_json(self.lock / "owner.json", {"tx": txid,
                    "finalizando": True, "restaurada": restaurada})
            comprobar_arbol(self.base, folder)
            shutil.rmtree(folder)
        if self.lock.exists():
            comprobar_arbol(self.base, self.lock)
            shutil.rmtree(self.lock)

# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Small, dependency-free backup core. Production requires Linux/Python 3.13."""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import uuid
from pathlib import Path
from typing import Callable, Protocol

UTC = dt.timezone.utc
REQUEST_LIMIT = 16 * 1024
ID_RE = re.compile(r"^[0-9a-f]{32}$")


class BackupError(Exception):
    """Only safe, fixed Spanish messages are surfaced to the bot/log."""


def validate_download_url(value: object) -> str:
    """Only a complete, read-only MEGA file link is a download destination."""
    if not isinstance(value, str) or re.fullmatch(
            r'https://mega\.nz/file/[A-Za-z0-9_-]{8}#[A-Za-z0-9_-]{43}', value) is None:
        raise BackupError('No se pudo preparar el enlace de descarga; se conserva la copia anterior.')
    return value


def utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def parse_date(value: object) -> dt.datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise BackupError("La fecha programada no es válida.")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise BackupError("La fecha programada no es válida.") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BackupError("La fecha debe incluir su zona horaria.")
    return parsed.astimezone(UTC)


def strict_json(data: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, TypeError):
        raise BackupError("El archivo de solicitud no es válido.") from None
    if not isinstance(value, dict):
        raise BackupError("El archivo de solicitud no es válido.")
    return value


def validate_request(value: dict, filename: str, now: dt.datetime) -> dict:
    rid = value.get("id")
    if not isinstance(rid, str) or not ID_RE.fullmatch(rid) or rid == "0" * 32:
        raise BackupError("El identificador de solicitud no es válido.")
    if filename != rid + ".json":
        raise BackupError("El nombre de solicitud no coincide.")
    action = value.get("action")
    if action in ("run", "clear_schedule"):
        if set(value) != {"id", "action"}:
            raise BackupError("La solicitud contiene campos no permitidos.")
        return value
    if action != "schedule" or set(value) != {"id", "action", "interval_hours", "dates", "enabled"}:
        raise BackupError("La solicitud no es válida.")
    interval = value["interval_hours"]
    dates = value["dates"]
    if interval is not None and (type(interval) is not int or not 1 <= interval <= 8760):
        raise BackupError("El intervalo debe estar entre 1 y 8760 horas.")
    if type(value["enabled"]) is not bool or not isinstance(dates, list) or len(dates) > 50:
        raise BackupError("La programación no es válida.")
    normalized = sorted({iso(parse_date(item)) for item in dates})
    if any(parse_date(item) <= now for item in normalized):
        raise BackupError("Las fechas programadas deben ser futuras.")
    if value["enabled"] and interval is None and not normalized:
        raise BackupError("La programación necesita un intervalo o una fecha.")
    return {**value, "dates": normalized}


@dataclasses.dataclass(frozen=True)
class Paths:
    source: Path
    state: Path = Path("/var/lib/storypulse-backups")
    spool: Path = Path("/var/spool/storypulse-backups")
    credentials: Path = Path("/etc/storypulse-backups/credentials.json")

    @property
    def work(self) -> Path:
        return self.state / "work"

    @property
    def status(self) -> Path:
        return self.state / "status.json"

    @property
    def database(self) -> Path:
        return self.state / "state.json"


def atomic_json(path: Path, value: dict, mode: int = 0o600, gid: int | None = None) -> None:
    """Destination's parent must be protected and owned by root in production."""
    data = (json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode) if hasattr(os, "fchmod") else None
            if gid is not None:
                os.fchown(output.fileno(), 0, gid)
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def secure_read(path: Path, limit: int, owner: int | None = None, mode: int | None = None) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size >= limit:
            raise BackupError("El archivo local no cumple las restricciones de seguridad.")
        if owner is not None and info.st_uid != owner:
            raise BackupError("El archivo local no pertenece al usuario esperado.")
        if mode is not None and stat.S_IMODE(info.st_mode) != mode:
            raise BackupError("Los permisos del archivo local no son válidos.")
        with os.fdopen(fd, "rb", closefd=False) as source:
            result = source.read(limit)
        if len(result) >= limit:
            raise BackupError("El archivo local es demasiado grande.")
        if signature(info) != signature(os.fstat(fd)):
            raise BackupError("El archivo local cambió durante su lectura.")
        return result
    finally:
        os.close(fd)


def credentials(path: Path, strict: bool = True) -> dict:
    value = strict_json(secure_read(path, REQUEST_LIMIT, 0 if strict else None, 0o600 if strict else None))
    keys = {"mega_email", "mega_password", "veracrypt_password"}
    if set(value) != keys or any(not isinstance(value[k], str) or not value[k] or len(value[k]) > 4096 for k in keys):
        raise BackupError("Revise el archivo privado de credenciales.")
    if any("\n" in value[k] or "\r" in value[k] or "\x00" in value[k] for k in keys):
        raise BackupError("Las credenciales no pueden contener saltos de línea.")
    return value


def signature(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def open_directory(path: Path) -> int:
    """Open every absolute component with NOFOLLOW, avoiding ancestor-link races."""
    if os.name != "posix" or not path.is_absolute():
        raise BackupError("La copia segura requiere una ruta absoluta en Linux.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            if part in ("", ".", ".."):
                raise BackupError("La ruta de origen no es válida.")
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


@dataclasses.dataclass
class Snapshot:
    root_fd: int
    entries: dict[tuple[str, ...], tuple]
    total_bytes: int

    def close(self) -> None:
        if self.root_fd >= 0:
            os.close(self.root_fd)
            self.root_fd = -1


def _scan(fd: int, prefix: tuple[str, ...], result: dict) -> int:
    total = 0
    before = os.fstat(fd)
    result[prefix] = signature(before)
    with os.scandir(fd) as iterator:
        names = sorted(entry.name for entry in iterator)
    for name in names:
        if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
            raise BackupError("El origen contiene un nombre no compatible.")
        relative = prefix + (name,)
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                if signature(info) != signature(os.fstat(child)):
                    raise BackupError("El origen cambió durante la copia.")
                total += _scan(child, relative, result)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            result[relative] = signature(info)
            total += info.st_size
        else:
            raise BackupError("El origen contiene enlaces o archivos especiales; revise historys.")
    if signature(before) != signature(os.fstat(fd)):
        raise BackupError("El origen cambió durante la copia.")
    return total


def snapshot(source: Path) -> Snapshot:
    if source.name != "historys":
        raise BackupError("El origen configurado debe ser la carpeta historys.")
    fd = open_directory(source)
    try:
        entries = {}
        size = _scan(fd, (), entries)
        return Snapshot(fd, entries, size)
    except BaseException:
        os.close(fd)
        raise


def container_size(snap: Snapshot) -> int:
    """Conservative exFAT budget, including 128 KiB cluster rounding per entry."""
    cluster = 128 * 1024
    allocated = 0
    for expected in snap.entries.values():
        if stat.S_ISDIR(expected[2]):
            allocated += cluster
        else:
            allocated += max(cluster, ((expected[4] + cluster - 1) // cluster) * cluster)
    bounded = allocated + max(64 * 1024**2, allocated // 100)
    mib = 1024**2
    return ((bounded + mib - 1) // mib) * mib


def estimate_space(snap: Snapshot) -> int:
    bounded = container_size(snap)
    return bounded + max(256 * 1024**2, bounded // 20)


def mounted_under(directory: Path) -> bool:
    """Also detect bind mounts (which os.path.ismount can miss). Fail closed on Linux."""
    if os.name != "posix":
        return os.path.ismount(directory / "mounted")
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
        for line in lines:
            encoded = line.split()[4]
            decoded = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), encoded)
            mount = Path(decoded)
            if mount == directory or directory in mount.parents:
                return True
        return False
    except (OSError, IndexError, UnicodeError):
        return True


def cleanup_stale_work(paths: Paths) -> None:
    """Recover local artifacts after a killed service, never traverse an active mount."""
    for directory in paths.work.iterdir():
        info = directory.lstat()
        if not ID_RE.fullmatch(directory.name) or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
            raise BackupError("Hay archivos inesperados en el área privada de trabajo; requiere revisión.")
        if mounted_under(directory):
            raise BackupError("Hay un contenedor todavía montado; desmóntelo antes de reanudar las copias.")
        shutil.rmtree(directory)


def _assert_snapshot_unchanged(snap: Snapshot) -> None:
    current = {}
    _scan(snap.root_fd, (), current)
    if current != snap.entries:
        raise BackupError("El origen cambió durante la copia.")


def _relative_directory(root_fd: int, parts: tuple[str, ...], expected: dict | None = None) -> int:
    fd = os.dup(root_fd)
    try:
        for depth, component in enumerate(parts, 1):
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            if expected is not None and signature(os.fstat(fd)) != expected[parts[:depth]]:
                raise BackupError("Un archivo cambió durante la copia o su verificación.")
        return fd
    except BaseException:
        os.close(fd)
        raise


def copy_snapshot(snap: Snapshot, mount: Path, progress: Callable[[int], None]) -> dict[tuple[str, ...], str]:
    """Copy historys contents directly to an empty mounted volume, without intermediates.

    Only regular source files are opened, exclusively through anchored descriptors.
    Destination names are created exclusively; nothing existing can be overwritten.
    """
    _assert_snapshot_unchanged(snap)
    destination = open_directory(mount)
    copied = 0
    hashes = {}
    try:
        if os.listdir(destination):
            raise BackupError("El contenedor de destino debe estar vacío.")
        for relative, expected in sorted(snap.entries.items()):
            if not relative:
                continue
            parent = _relative_directory(destination, relative[:-1])
            try:
                if stat.S_ISDIR(expected[2]):
                    source_dir = _relative_directory(snap.root_fd, relative, snap.entries)
                    os.close(source_dir)
                    os.mkdir(relative[-1], mode=0o700, dir_fd=parent)
                    os.fsync(parent)
                    continue
                source_parent = _relative_directory(snap.root_fd, relative[:-1], snap.entries)
                try:
                    source_fd = os.open(relative[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_parent)
                finally:
                    os.close(source_parent)
                try:
                    before = os.fstat(source_fd)
                    if signature(before) != expected or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                        raise BackupError("El origen cambió durante la copia.")
                    target_fd = os.open(relative[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                        0o600, dir_fd=parent)
                    digest = hashlib.sha256()
                    with os.fdopen(target_fd, "wb") as target:
                        with os.fdopen(source_fd, "rb", closefd=False) as source:
                            remaining = expected[4]
                            while remaining:
                                chunk = source.read(min(1024**2, remaining))
                                if not chunk:
                                    raise BackupError("El origen cambió durante la copia.")
                                target.write(chunk)
                                digest.update(chunk)
                                remaining -= len(chunk)
                                copied += len(chunk)
                                progress(min(100, copied * 100 // max(1, snap.total_bytes)))
                            if source.read(1):
                                raise BackupError("El origen cambió durante la copia.")
                        target.flush()
                        os.fsync(target.fileno())
                    if signature(os.fstat(source_fd)) != expected:
                        raise BackupError("El origen cambió durante la copia.")
                    os.fsync(parent)
                    hashes[relative] = digest.hexdigest()
                finally:
                    os.close(source_fd)
            finally:
                os.close(parent)
        _assert_snapshot_unchanged(snap)
        if copied != snap.total_bytes:
            raise BackupError("El origen cambió durante la copia.")
        os.fsync(destination)
        progress(100)
        return hashes
    finally:
        os.close(destination)


def verify_snapshot_copy(snap: Snapshot, mount: Path, hashes: dict[tuple[str, ...], str],
                         progress: Callable[[int], None]) -> None:
    """Read-only comparison of the reopened volume: exact names, sizes, and SHA-256."""
    file_names = {name for name, info in snap.entries.items() if stat.S_ISREG(info[2])}
    if not isinstance(hashes, dict) or set(hashes) != file_names:
        raise BackupError("La lista de verificación de la copia no es válida.")
    destination = open_directory(mount)
    verified_bytes = 0
    try:
        entries = {}
        _scan(destination, (), entries)
        if set(entries) != set(snap.entries):
            raise BackupError("Los nombres dentro del contenedor no coinciden con historys.")
        for relative, source_info in sorted(snap.entries.items()):
            copied_info = entries[relative]
            if stat.S_ISDIR(source_info[2]):
                if not stat.S_ISDIR(copied_info[2]):
                    raise BackupError("Las carpetas dentro del contenedor no coinciden.")
                continue
            if not stat.S_ISREG(copied_info[2]) or copied_info[4] != source_info[4] or copied_info[3] != 1:
                raise BackupError("El tamaño de un archivo cifrado no coincide.")
            parent = _relative_directory(destination, relative[:-1], entries)
            try:
                fd = os.open(relative[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            finally:
                os.close(parent)
            try:
                if signature(os.fstat(fd)) != copied_info:
                    raise BackupError("Un archivo cifrado cambió durante la verificación.")
                digest = hashlib.sha256()
                with os.fdopen(fd, "rb", closefd=False) as source:
                    remaining = source_info[4]
                    while remaining:
                        chunk = source.read(min(1024**2, remaining))
                        if not chunk:
                            raise BackupError("Un archivo cifrado está incompleto.")
                        digest.update(chunk)
                        remaining -= len(chunk)
                        verified_bytes += len(chunk)
                        progress(min(100, verified_bytes * 100 // max(1, snap.total_bytes)))
                    if source.read(1):
                        raise BackupError("El tamaño de un archivo cifrado no coincide.")
                if signature(os.fstat(fd)) != copied_info or digest.hexdigest() != hashes[relative]:
                    raise BackupError("Falló la verificación SHA-256 de un archivo cifrado.")
            finally:
                os.close(fd)
        after = {}
        _scan(destination, (), after)
        if after != entries or verified_bytes != snap.total_bytes:
            raise BackupError("La copia cambió durante su verificación.")
        _assert_snapshot_unchanged(snap)
        progress(100)
    finally:
        os.close(destination)


class CryptAdapter(Protocol):
    def create_from_snapshot(self, snap: Snapshot, container_path: Path, mount_dir: Path,
                             password: str, progress: Callable[[int], None]) -> None: ...


class CloudAdapter(Protocol):
    def ensure_session(self, email: str, password: str) -> None: ...
    def free_bytes(self) -> int: ...
    def upload(self, local: Path, name: str, run_id: str, progress: Callable[[int], None]) -> dict: ...
    def verify(self, remote: dict, local: Path) -> bool:
        """Confirm provider completion, expected own-node identity, size and metadata.

        This is provider confirmation, not an independent full remote byte comparison.
        """
        ...
    def delete(self, remote_id: str) -> None: ...
    def export_link(self, remote: dict) -> str: ...


def empty_schedule() -> dict:
    return {"interval_hours": None, "dates": [], "enabled": False, "next_run_at": None}


class Store:
    def __init__(self, paths: Paths, *, status_gid: int | None = None):
        self.paths = paths
        self.status_gid = status_gid
        self.lock = threading.RLock()
        self.data = {"version": 1, "schedule": empty_schedule(), "interval_next": None,
                     "manifest": [], "pending_upload": None, "processed": [], "last_claim": None}
        self.status = {"revision": 0, "state": "idle", "message": "Listo para crear una copia.",
                       "progress": 0, "run_id": None, "request_id": None, "filename": None,
                       "last_request_id": None, "last_request_action": None, "last_request_result": None,
                       "stage": "idle", "started_at": None, "finished_at": None,
                       "updated_at": iso(utcnow()), "schedule": empty_schedule(), "last_success": None}

    def load(self, strict: bool = True) -> None:
        if self.paths.database.exists():
            value = strict_json(secure_read(self.paths.database, 1024**2, 0 if strict else None, 0o600 if strict else None))
            if value.get("version") != 1 or set(value) != set(self.data):
                raise BackupError("El estado privado no es válido; requiere revisión.")
            if not isinstance(value["processed"], list) or len(value["processed"]) > 1024 or any(not isinstance(x, str) or not ID_RE.fullmatch(x) for x in value["processed"]):
                raise BackupError("El estado privado no es válido; requiere revisión.")
            if not isinstance(value["manifest"], list) or any(not isinstance(x, dict) or not isinstance(x.get("id"), str) or not x["id"] or x.get("verified") is not True for x in value["manifest"]):
                raise BackupError("El manifiesto privado no es válido; requiere revisión.")
            pending = value["pending_upload"]
            if pending is not None and (not isinstance(pending, dict) or not isinstance(pending.get("id"), str) or not pending["id"] or not isinstance(pending.get("run_id"), str) or not ID_RE.fullmatch(pending["run_id"])):
                raise BackupError("La subida pendiente no es válida; requiere revisión.")
            s = value["schedule"]
            if not isinstance(s, dict) or set(s) != set(empty_schedule()) or type(s["enabled"]) is not bool or not isinstance(s["dates"], list) or len(s["dates"]) > 50:
                raise BackupError("La programación guardada no es válida.")
            interval = s["interval_hours"]
            if interval is not None and (type(interval) is not int or not 1 <= interval <= 8760):
                raise BackupError("La programación guardada no es válida.")
            for date in s["dates"]:
                parse_date(date)
            if value["interval_next"] is not None:
                parse_date(value["interval_next"])
            self.data = value
        if self.paths.status.exists():
            old = strict_json(secure_read(self.paths.status, 128 * 1024, 0 if strict else None, 0o640 if strict else None))
            if type(old.get("revision")) is int and old["revision"] >= 0:
                self.status["revision"] = old["revision"]
            self.status["last_success"] = old.get("last_success")
        linked = None
        for record in reversed(self.data['manifest']):
            try:
                url = validate_download_url(record.get('download_url'))
                parse_date(record.get('at'))
            except BackupError:
                continue
            if (not isinstance(record.get('filename'), str)
                    or re.fullmatch(r'backup-\d{2}-\d{2}-\d{4}', record['filename']) is None
                    or type(record.get('bytes')) is not int or not 0 < record['bytes'] < 2**63):
                continue
            linked = {key: record[key] for key in ('at', 'filename', 'bytes')}
            linked['download_url'] = url
            break
        if linked is not None:
            self.status['last_success'] = linked
        elif isinstance(self.status['last_success'], dict):
            self.status['last_success'].pop('download_url', None)
        self.recompute_next()
        self.publish()

    def save(self) -> None:
        atomic_json(self.paths.database, self.data)

    def recompute_next(self) -> None:
        schedule = self.data["schedule"]
        candidates = list(schedule["dates"])
        if self.data["interval_next"]:
            candidates.append(self.data["interval_next"])
        schedule["next_run_at"] = min(candidates, key=parse_date) if schedule["enabled"] and candidates else None

    def publish(self, **changes) -> None:
        with self.lock:
            self.status.update(changes)
            self.status["revision"] += 1
            self.status["updated_at"] = iso(utcnow())
            self.status["schedule"] = dict(self.data["schedule"])
            atomic_json(self.paths.status, self.status, 0o640, self.status_gid)

    def mark_processed(self, rid: str) -> bool:
        if rid in self.data["processed"]:
            return False
        self.data["processed"] = (self.data["processed"] + [rid])[-1024:]
        return True

    def set_schedule(self, request: dict, now: dt.datetime) -> None:
        with self.lock:
            interval = request["interval_hours"]
            self.data["schedule"] = {"interval_hours": interval, "dates": request["dates"],
                                     "enabled": request["enabled"], "next_run_at": None}
            self.data["interval_next"] = iso(now + dt.timedelta(hours=interval)) if interval else None
            self.recompute_next()
            self.save()

    def claim_due(self, now: dt.datetime, run_id: str) -> bool:
        """Persist the claim before starting work: restarts cannot execute it twice."""
        with self.lock:
            next_at = self.data["schedule"]["next_run_at"]
            if next_at is None or parse_date(next_at) > now:
                return False
            schedule = self.data["schedule"]
            schedule["dates"] = [x for x in schedule["dates"] if parse_date(x) > now]
            interval = schedule["interval_hours"]
            interval_next = self.data["interval_next"]
            if interval and interval_next and parse_date(interval_next) <= now:
                previous = parse_date(interval_next)
                elapsed = (now - previous).total_seconds()
                periods = int(elapsed // (interval * 3600)) + 1
                self.data["interval_next"] = iso(previous + dt.timedelta(hours=periods * interval))
            self.data["last_claim"] = {"at": iso(now), "run_id": run_id, "due_at": next_at}
            self.recompute_next()
            self.save()
            return True


class Pipeline:
    def __init__(self, store: Store, crypt: CryptAdapter, cloud: CloudAdapter, *,
                 strict_credentials: bool = True, snapshot_factory=snapshot):
        self.store, self.paths, self.crypt, self.cloud = store, store.paths, crypt, cloud
        self.strict_credentials, self.snapshot_factory = strict_credentials, snapshot_factory

    def _progress(self, stage: str, message: str, base: int, span: int):
        last = [-1]
        def update(percent: int):
            mapped = base + max(0, min(100, int(percent))) * span // 100
            if mapped != last[0]:
                self.store.publish(stage=stage, message=message, progress=mapped)
                last[0] = mapped
        return update

    def _retain(self, current_id: str) -> None:
        with self.store.lock:
            old_ids = [item["id"] for item in self.store.data["manifest"] if item["id"] != current_id]
        for rid in old_ids:
            self.cloud.delete(rid)
            with self.store.lock:
                self.store.data["manifest"] = [x for x in self.store.data["manifest"] if x["id"] != rid]
                self.store.save()

    def _cleanup_pending(self) -> None:
        with self.store.lock:
            pending = self.store.data["pending_upload"]
            if pending is None:
                return
            if any(item["id"] == pending["id"] for item in self.store.data["manifest"]):
                raise BackupError("La subida pendiente requiere revisión del manifiesto privado.")
        self.cloud.delete(pending["id"])
        with self.store.lock:
            self.store.data["pending_upload"] = None
            self.store.save()

    def run(self, run_id: str, request_id: str | None = None, now: dt.datetime | None = None) -> None:
        if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id):
            raise BackupError("El identificador interno de la copia no es válido.")
        now = now or utcnow()
        filename = "backup-" + now.astimezone(dt.timezone(dt.timedelta(hours=-3))).strftime("%d-%m-%Y")
        directory = self.paths.work / run_id
        snap = None
        verified = False
        try:
            directory.mkdir(mode=0o700)
            self.store.publish(state="running", message="Preparando la copia.", progress=0,
                               run_id=run_id, request_id=request_id, filename=filename, stage="prepare",
                               started_at=iso(now), finished_at=None)
            secret = credentials(self.paths.credentials, self.strict_credentials)
            self.cloud.ensure_session(secret["mega_email"], secret["mega_password"])
            self._cleanup_pending()
            snap = self.snapshot_factory(self.paths.source)
            required = estimate_space(snap)
            if shutil.disk_usage(self.paths.work).free < required:
                raise BackupError("No hay espacio local suficiente para crear la copia.")
            cloud_required = container_size(snap)
            if self.cloud.free_bytes() < cloud_required:
                raise BackupError("No hay espacio suficiente en MEGA; la copia anterior se conserva.")
            container = directory / filename
            mount_dir = directory / "mounted"
            mount_dir.mkdir(mode=0o700)
            self.crypt.create_from_snapshot(snap, container, mount_dir, secret["veracrypt_password"],
                                           self._progress("encrypt", "Copiando y verificando archivos dentro de VeraCrypt.", 5, 55))
            secret.clear()
            info = container.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0:
                raise BackupError("El contenedor cifrado no es válido.")
            if self.cloud.free_bytes() < info.st_size:
                raise BackupError("No hay espacio suficiente en MEGA; la copia anterior se conserva.")
            remote = self.cloud.upload(container, filename, run_id,
                                       self._progress("upload", "Subiendo copia cifrada a MEGA.", 60, 30))
            if not isinstance(remote, dict) or not isinstance(remote.get("id"), str) or not remote["id"] or len(remote["id"]) > 4096 or any(c in remote["id"] for c in "\r\n\x00"):
                raise BackupError("MEGA no confirmó el archivo completo; se conserva la copia anterior.")
            with self.store.lock:
                if any(item["id"] == remote["id"] for item in self.store.data["manifest"]):
                    raise BackupError("MEGA devolvió un identificador repetido; se conserva la copia anterior.")
                self.store.data["pending_upload"] = {"id": remote["id"], "run_id": run_id}
                self.store.save()
            if type(remote.get("size")) is not int or remote["size"] != info.st_size:
                raise BackupError("MEGA no confirmó el tamaño completo; se conserva la copia anterior.")
            self.store.publish(stage="verify", message="Comprobando confirmación y tamaño de la copia remota.", progress=91)
            if self.cloud.verify(remote, container) is not True:
                raise BackupError("No se pudo verificar la copia remota; se conserva la copia anterior.")
            self.store.publish(stage='link', progress=94, message='Preparando el enlace de descarga de la copia verificada.')
            try:
                download_url = validate_download_url(self.cloud.export_link(remote))
            except Exception:
                raise BackupError('No se pudo preparar el enlace de descarga; se conserva la copia anterior.') from None
            finished = iso(utcnow())
            record = {"id": remote["id"], "run_id": run_id, "filename": filename,
                      "bytes": info.st_size, "at": finished, "verified": True,
                      "download_url": download_url}
            with self.store.lock:
                if any(item["id"] == record["id"] for item in self.store.data["manifest"]):
                    raise BackupError("MEGA devolvió un identificador repetido; se conserva la copia anterior.")
                self.store.data["manifest"].append(record)
                self.store.data["pending_upload"] = None
                self.store.save()
            verified = True
            commit = getattr(self.cloud, "commit", None)
            if commit is not None:
                commit(remote)
            self.store.publish(stage="retention", progress=97, message="Conservando la copia nueva verificada.",
                               last_success={"at": finished, "filename": filename, "bytes": info.st_size,
                                             "download_url": download_url})
            self._retain(record["id"])
            self.store.publish(state="success", stage="complete", progress=100,
                               message="Copia cifrada verificada en MEGA. Se conserva una copia.", finished_at=iso(utcnow()))
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if verified:
                message = "La copia nueva está verificada; falta completar la limpieza de copias anteriores."
            elif isinstance(exc, BackupError):
                message = str(exc)
            else:
                message = "No se pudo completar la copia. La fuente y la copia anterior se conservan."
            if not verified:
                with contextlib.suppress(Exception):
                    self._cleanup_pending()
            self.store.publish(state="error", stage="error", message=message, finished_at=iso(utcnow()))
        finally:
            try:
                if snap is not None:
                    snap.close()
                if directory.exists():
                    if mounted_under(directory):
                        raise BackupError("El contenedor sigue montado y requiere revisión local.")
                    shutil.rmtree(directory)
            except Exception:
                message = ("La copia remota está verificada; falta limpiar temporales locales."
                           if verified else "No se pudo limpiar el área temporal local. La fuente se conserva.")
                with contextlib.suppress(Exception):
                    self.store.publish(state="error", stage="cleanup", message=message,
                                       finished_at=iso(utcnow()))


def read_request(spool_fd: int, filename: str, bot_uid: int | None, now: dt.datetime) -> tuple[dict, tuple]:
    if not re.fullmatch(r"[0-9a-f]{32}\.json", filename):
        raise BackupError("El nombre de solicitud no es válido.")
    fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=spool_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size >= REQUEST_LIMIT or stat.S_IMODE(info.st_mode) != 0o600:
            raise BackupError("La solicitud no cumple las restricciones de seguridad.")
        if bot_uid is not None and info.st_uid != bot_uid:
            raise BackupError("La solicitud no pertenece al usuario del bot.")
        data = os.read(fd, REQUEST_LIMIT)
        if len(data) >= REQUEST_LIMIT or signature(info) != signature(os.fstat(fd)):
            raise BackupError("La solicitud cambió durante su lectura.")
        return validate_request(strict_json(data), filename, now), signature(info)
    finally:
        os.close(fd)

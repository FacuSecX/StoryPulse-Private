# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Root service entry point; no bot process or Instagram calls are made here."""
from __future__ import annotations

import argparse
import contextlib
import os
import signal
import stat
import sys
import threading
import uuid
from pathlib import Path

from .core import (ID_RE, BackupError, Paths, Pipeline, Store, cleanup_stale_work, empty_schedule, iso,
                   open_directory, read_request, secure_read, signature,
                   strict_json, utcnow)


def check_directory(path: Path, uid: int, gid: int, mode: int) -> None:
    if not path.exists():
        path.mkdir(mode=mode)
        os.chown(path, uid, gid)
        os.chmod(path, mode)
    fd = open_directory(path)
    try:
        info = os.fstat(fd)
        if info.st_uid != uid or info.st_gid != gid or stat.S_IMODE(info.st_mode) != mode:
            raise BackupError("Revise propietario y permisos de las carpetas de backups.")
    finally:
        os.close(fd)


def prepare(paths: Paths, uid: int, gid: int) -> None:
    if os.name != "posix" or os.geteuid() != 0:
        raise BackupError("El worker debe ejecutarse como root en Linux.")
    check_directory(paths.state, 0, gid, 0o750)
    check_directory(paths.work, 0, 0, 0o700)
    check_directory(paths.spool, uid, gid, 0o700)
    for target in (paths.state, paths.spool):
        if paths.source == target or target in paths.source.parents or paths.source in target.parents:
            raise BackupError("La carpeta historys debe estar separada del estado del worker.")


class Worker:
    def __init__(self, store: Store, pipeline: Pipeline, bot_uid: int):
        self.store, self.pipeline, self.bot_uid = store, pipeline, bot_uid
        self.active = threading.Event()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self, run_id: str, request_id: str | None) -> None:
        if self.active.is_set():
            raise BackupError("Ya hay una copia en curso.")
        self.active.set()
        self.store.publish(state="running", stage="prepare", progress=0, run_id=run_id,
                           request_id=request_id, filename=None, started_at=iso(utcnow()),
                           finished_at=None, message="Preparando la copia.")
        def work():
            try:
                self.pipeline.run(run_id, request_id)
            finally:
                self.active.clear()
        self.thread = threading.Thread(target=work, name="backup", daemon=False)
        self.thread.start()

    def handle(self, request: dict) -> None:
        with self.store.lock:
            if not self.store.mark_processed(request["id"]):
                return
            action = request["action"]
            if action == "run":
                self.store.save()
                if self.active.is_set():
                    self.store.publish(last_request_id=request["id"], last_request_action="run", last_request_result="busy",
                                       message="Ya hay una copia en curso; no se inició otra.")
                else:
                    self.store.publish(last_request_id=request["id"], last_request_action="run", last_request_result="accepted")
                    self.start(uuid.uuid4().hex, request["id"])
            elif action == "schedule":
                self.store.set_schedule(request, utcnow())
                self.store.publish(last_request_id=request["id"], last_request_action=action, last_request_result="applied",
                                   message="Programación guardada.")
            elif action == "clear_schedule":
                self.store.data["schedule"] = empty_schedule()
                self.store.data["interval_next"] = None
                self.store.save()
                self.store.publish(last_request_id=request["id"], last_request_action=action, last_request_result="applied",
                                   message="Programación eliminada.")

    def tick(self) -> None:
        spool_fd = open_directory(self.store.paths.spool)
        try:
            names = sorted(name for name in os.listdir(spool_fd) if name.endswith(".json") and ID_RE.fullmatch(name[:-5]))[:100]
            for name in names:
                if len(name) != 37 or not name.endswith(".json"):
                    continue
                before = None
                try:
                    before = os.stat(name, dir_fd=spool_fd, follow_symlinks=False)
                    request, _ = read_request(spool_fd, name, self.bot_uid, utcnow())
                    self.handle(request)
                except (BackupError, OSError):
                    self.store.publish(message="Se rechazó una solicitud de backup inválida.")
                finally:
                    try:
                        current = os.stat(name, dir_fd=spool_fd, follow_symlinks=False)
                        if before is not None and signature(current) == signature(before) and not stat.S_ISDIR(current.st_mode):
                            os.unlink(name, dir_fd=spool_fd)
                    except FileNotFoundError:
                        pass
        finally:
            os.close(spool_fd)
        if not self.active.is_set():
            run_id = uuid.uuid4().hex
            if self.store.claim_due(utcnow(), run_id):
                self.start(run_id, None)

    def loop(self, once: bool = False) -> None:
        while not self.stop_event.is_set():
            self.tick()
            if once:
                break
            self.stop_event.wait(1)
        if self.thread is not None:
            self.thread.join()


def acquire_lock(path: Path) -> int:
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        os.close(fd)
        raise BackupError("El bloqueo privado del worker no es válido.")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise BackupError("Ya hay otro worker de backups activo.") from None
    return fd


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Worker independiente de backups cifrados.")
    parser.add_argument("--config", default="/etc/storypulse-backups/config.json")
    parser.add_argument("--check", action="store_true", help="Validar configuración local sin subir ni iniciar sesión.")
    parser.add_argument("--once", action="store_true", help="Procesar la cola y vencimientos una vez, esperar cualquier copia y salir.")
    args = parser.parse_args(argv)
    lock_fd = None
    try:
        if os.name != "posix" or os.geteuid() != 0:
            raise BackupError("El worker debe ejecutarse como root en Linux.")
        import pwd
        config = strict_json(secure_read(Path(args.config), 64 * 1024, 0, 0o600))
        bot_user = config.get("bot_user", "debian")
        if not isinstance(bot_user, str) or not __import__("re").fullmatch(r"[a-z_][a-z0-9_-]{0,31}", bot_user):
            raise BackupError("El usuario del bot no es válido.")
        account = pwd.getpwnam(bot_user)
        if account.pw_uid == 0:
            raise BackupError("El bot necesita un usuario sin privilegios de root.")
        source = config.get("source")
        if not isinstance(source, str) or not Path(source).is_absolute() or Path(source).name != "historys":
            raise BackupError("Configure la ruta absoluta de la carpeta historys.")
        paths = Paths(source=Path(source))
        prepare(paths, account.pw_uid, account.pw_gid)
        lock_fd = acquire_lock(paths.state / "worker.lock")
        from .adapters import make_adapters
        crypt, cloud = make_adapters(config)
        from .core import credentials
        secret = credentials(paths.credentials)
        secret.clear()
        source_fd = open_directory(paths.source)
        os.close(source_fd)
        if args.check:
            print("Configuración local válida. No se inició sesión ni se subieron archivos.")
            return 0
        recover = getattr(crypt, "recover", None)
        if recover is not None:
            recover(paths.work)
        cleanup_stale_work(paths)
        store = Store(paths, status_gid=account.pw_gid)
        store.load()
        pipeline = Pipeline(store, crypt, cloud)
        worker = Worker(store, pipeline, account.pw_uid)
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_: worker.stop_event.set())
        worker.loop(args.once)
        return 0
    except (BackupError, KeyError):
        print("No se pudo iniciar el worker; revise configuración, permisos y dependencias locales.", file=sys.stderr)
        return 1
    except Exception:
        print("El worker se detuvo por un error local. Revise la instalación.", file=sys.stderr)
        return 1
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())

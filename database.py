# StoryPulse v2.0
# Created by FacuSecX https://github.com/FacuSecX/StoryPulse-Private

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "bot_historias.db"

_LOCK = threading.RLock()


# ============================================================
# CONEXIÓN / UTILIDADES
# ============================================================

@contextmanager
def conectar() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
    finally:
        conn.close()


def ahora_iso() -> str:
    return datetime.now(
        timezone.utc
    ).isoformat()


def _columnas(
    conn: sqlite3.Connection,
    tabla: str,
) -> set[str]:
    rows = conn.execute(
        f"PRAGMA table_info({tabla})"
    ).fetchall()

    return {
        str(row["name"])
        for row in rows
    }


# ============================================================
# INICIALIZACIÓN / MIGRACIÓN
# ============================================================

def inicializar() -> None:
    """
    Crea o actualiza la base sin borrar datos existentes.

    Compatible con las versiones anteriores de StoryPulse Web Private.
    """
    with _LOCK, conectar() as conn:

        # ----------------------------------------------------
        # Tablas base
        # ----------------------------------------------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS historias_enviadas (
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                story_pk TEXT NOT NULL,
                enviada_en TEXT NOT NULL,
                PRIMARY KEY (
                    chat_id,
                    username,
                    story_pk
                )
            )
            """
        )

        # Historias guardadas dentro de carruseles destacados. Se conserva el
        # ID del carrusel y el ID de la Story para que una nueva sesión o un
        # cambio de nombre del carrusel no vuelva a descargar el mismo item.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS historias_destacadas_descargadas (
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                highlight_id TEXT NOT NULL,
                story_pk TEXT NOT NULL,
                grupo TEXT NOT NULL DEFAULT '',
                ruta TEXT NOT NULL DEFAULT '',
                descargada_en TEXT NOT NULL,
                PRIMARY KEY (
                    chat_id,
                    username,
                    highlight_id,
                    story_pk
                )
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS programaciones (
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                horarios_json TEXT NOT NULL,
                activa INTEGER NOT NULL DEFAULT 1,
                creada_en TEXT NOT NULL,
                notificacion_activada INTEGER NOT NULL DEFAULT 1,
                tipo_programacion TEXT NOT NULL DEFAULT 'horarios',
                intervalo_horas INTEGER,
                intervalo_inicio TEXT,
                modalidad_sesiones TEXT NOT NULL DEFAULT 'normal',
                sesiones_rotacion_json TEXT NOT NULL DEFAULT '[]',
                ultima_sesion_rotacion TEXT,
                PRIMARY KEY (
                    chat_id,
                    username
                )
            )
            """
        )

        # Migración desde la primera versión del proyecto.
        columnas_programaciones = _columnas(
            conn,
            "programaciones",
        )

        if (
            "notificacion_activada"
            not in columnas_programaciones
        ):
            conn.execute(
                """
                ALTER TABLE programaciones
                ADD COLUMN notificacion_activada
                INTEGER NOT NULL DEFAULT 1
                """
            )

        if (
            "tipo_programacion"
            not in columnas_programaciones
        ):
            conn.execute(
                """
                ALTER TABLE programaciones
                ADD COLUMN tipo_programacion
                TEXT NOT NULL DEFAULT 'horarios'
                """
            )

        if (
            "intervalo_horas"
            not in columnas_programaciones
        ):
            conn.execute(
                """
                ALTER TABLE programaciones
                ADD COLUMN intervalo_horas INTEGER
                """
            )

        if (
            "intervalo_inicio"
            not in columnas_programaciones
        ):
            conn.execute(
                """
                ALTER TABLE programaciones
                ADD COLUMN intervalo_inicio TEXT
                """
            )

        # La selección de sesiones es independiente de la cadencia. Las
        # programaciones existentes conservan su comportamiento habitual.
        for columna, definicion in (
            ("modalidad_sesiones", "TEXT NOT NULL DEFAULT 'normal'"),
            ("sesiones_rotacion_json", "TEXT NOT NULL DEFAULT '[]'"),
            ("ultima_sesion_rotacion", "TEXT"),
        ):
            if columna not in columnas_programaciones:
                conn.execute(
                    f"ALTER TABLE programaciones ADD COLUMN {columna} {definicion}"
                )

        # ----------------------------------------------------
        # Publicaciones descargadas / antirepetición
        # ----------------------------------------------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publicaciones_descargadas (
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                post_id TEXT NOT NULL,
                fecha_publicacion TEXT NOT NULL,
                cantidad_archivos INTEGER NOT NULL DEFAULT 0,
                descargada_en TEXT NOT NULL,
                PRIMARY KEY (
                    chat_id,
                    username,
                    post_id
                )
            )
            """
        )

        # Estado de backfill de publicaciones por perfil.
        # Las instalaciones existentes empiezan sin fila, por lo que la primera
        # ejecución con esta versión hará un recorrido histórico completo.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS publicaciones_sync_perfil (
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                historial_completo INTEGER NOT NULL DEFAULT 0,
                actualizado_en TEXT NOT NULL,
                PRIMARY KEY (
                    chat_id,
                    username
                )
            )
            """
        )

        # ----------------------------------------------------
        # Multimedia enviada por el bot
        # ----------------------------------------------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multimedia_telegram (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                enviada_en TEXT NOT NULL,
                PRIMARY KEY (
                    chat_id,
                    message_id
                )
            )
            """
        )

        # ----------------------------------------------------
        # Textos registrados por LIMPIAR CHAT V2.3
        # Se mantiene por compatibilidad.
        # ----------------------------------------------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mensajes_chat_limpiables (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                creada_en TEXT NOT NULL,
                PRIMARY KEY (
                    chat_id,
                    message_id
                )
            )
            """
        )

        # ----------------------------------------------------
        # IDs de multimedia que LIMPIAR CHAT NO debe tocar
        # ----------------------------------------------------
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS multimedia_protegida_chat (
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                creada_en TEXT NOT NULL,
                PRIMARY KEY (
                    chat_id,
                    message_id
                )
            )
            """
        )

        # Toda multimedia que ya estaba registrada por versiones
        # anteriores también queda protegida automáticamente.
        conn.execute(
            """
            INSERT OR IGNORE INTO multimedia_protegida_chat
            (
                chat_id,
                message_id,
                creada_en
            )
            SELECT
                chat_id,
                message_id,
                enviada_en
            FROM multimedia_telegram
            """
        )

        # ----------------------------------------------------
        # Índices
        # ----------------------------------------------------
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_historias_usuario
            ON historias_enviadas (
                chat_id,
                username
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_historias_destacadas_usuario
            ON historias_destacadas_descargadas (
                chat_id,
                username
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_programaciones_chat
            ON programaciones (
                chat_id
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_publicaciones_usuario
            ON publicaciones_descargadas (
                chat_id,
                username
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_multimedia_chat_fecha
            ON multimedia_telegram (
                chat_id,
                enviada_en
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_limpiable_fecha
            ON mensajes_chat_limpiables (
                chat_id,
                creada_en
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_multimedia_protegida_fecha
            ON multimedia_protegida_chat (
                chat_id,
                creada_en
            )
            """
        )

        conn.commit()


# ============================================================
# ANTIDUPLICACIÓN DE STORIES
# ============================================================

def historia_ya_enviada(
    chat_id: int,
    username: str,
    story_pk: str,
) -> bool:

    with _LOCK, conectar() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM historias_enviadas
            WHERE chat_id = ?
              AND username = ?
              AND story_pk = ?
            LIMIT 1
            """,
            (
                int(chat_id),
                str(username).lower(),
                str(story_pk),
            ),
        ).fetchone()

    return row is not None


def registrar_historia(
    chat_id: int,
    username: str,
    story_pk: str,
) -> None:

    with _LOCK, conectar() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO historias_enviadas
            (
                chat_id,
                username,
                story_pk,
                enviada_en
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                int(chat_id),
                str(username).lower(),
                str(story_pk),
                ahora_iso(),
            ),
        )
        conn.commit()


def destacada_ya_descargada(
    chat_id: int,
    username: str,
    highlight_id: str,
    story_pk: str,
) -> bool:
    """Indica si una Story de un carrusel ya fue archivada."""
    with _LOCK, conectar() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM historias_destacadas_descargadas
            WHERE chat_id = ?
              AND username = ?
              AND highlight_id = ?
              AND story_pk = ?
            LIMIT 1
            """,
            (
                int(chat_id),
                str(username).lower(),
                str(highlight_id),
                str(story_pk),
            ),
        ).fetchone()
    return row is not None


def obtener_destacada(
    chat_id: int,
    username: str,
    highlight_id: str,
    story_pk: str,
) -> dict[str, str] | None:
    """Devuelve el registro completo para poder migrar rutas antiguas."""
    with _LOCK, conectar() as conn:
        row = conn.execute(
            """
            SELECT grupo, ruta
            FROM historias_destacadas_descargadas
            WHERE chat_id = ?
              AND username = ?
              AND highlight_id = ?
              AND story_pk = ?
            LIMIT 1
            """,
            (
                int(chat_id),
                str(username).lower(),
                str(highlight_id),
                str(story_pk),
            ),
        ).fetchone()
    if row is None:
        return None
    return {"grupo": str(row["grupo"] or ""), "ruta": str(row["ruta"] or "")}


def actualizar_ruta_destacada(
    chat_id: int,
    username: str,
    highlight_id: str,
    story_pk: str,
    *,
    grupo: str,
    ruta: str,
) -> None:
    """Actualiza la carpeta/nombre conservando el ID antirepetición."""
    with _LOCK, conectar() as conn:
        conn.execute(
            """
            UPDATE historias_destacadas_descargadas
            SET grupo = ?, ruta = ?
            WHERE chat_id = ?
              AND username = ?
              AND highlight_id = ?
              AND story_pk = ?
            """,
            (
                str(grupo),
                str(ruta),
                int(chat_id),
                str(username).lower(),
                str(highlight_id),
                str(story_pk),
            ),
        )
        conn.commit()


def registrar_destacada(
    chat_id: int,
    username: str,
    highlight_id: str,
    story_pk: str,
    *,
    grupo: str = "",
    ruta: str = "",
) -> None:
    """Registra una Story de Highlights de forma idempotente."""
    with _LOCK, conectar() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO historias_destacadas_descargadas
            (
                chat_id,
                username,
                highlight_id,
                story_pk,
                grupo,
                ruta,
                descargada_en
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(chat_id),
                str(username).lower(),
                str(highlight_id),
                str(story_pk),
                str(grupo),
                str(ruta),
                ahora_iso(),
            ),
        )
        conn.commit()


def ids_destacadas_descargadas(
    chat_id: int,
    username: str,
) -> set[tuple[str, str]]:
    """Devuelve (ID del carrusel, ID de Story) ya archivados."""
    with _LOCK, conectar() as conn:
        rows = conn.execute(
            """
            SELECT highlight_id, story_pk
            FROM historias_destacadas_descargadas
            WHERE chat_id = ?
              AND username = ?
            """,
            (int(chat_id), str(username).lower()),
        ).fetchall()
    return {(str(row["highlight_id"]), str(row["story_pk"])) for row in rows}


# ============================================================
# ANTIDUPLICACIÓN DE PUBLICACIONES
# ============================================================

def publicacion_ya_descargada(
    chat_id: int,
    username: str,
    post_id: str,
) -> bool:

    with _LOCK, conectar() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM publicaciones_descargadas
            WHERE chat_id = ?
              AND username = ?
              AND post_id = ?
            LIMIT 1
            """,
            (
                int(chat_id),
                str(username).lower(),
                str(post_id),
            ),
        ).fetchone()

    return row is not None


def ids_publicaciones_descargadas(
    chat_id: int,
    username: str,
) -> set[str]:
    """
    Devuelve en una sola consulta todos los Post ID ya procesados
    para evitar consultar SQLite una vez por publicación.
    """
    with _LOCK, conectar() as conn:
        rows = conn.execute(
            """
            SELECT post_id
            FROM publicaciones_descargadas
            WHERE chat_id = ?
              AND username = ?
            """,
            (
                int(chat_id),
                str(username).lower(),
            ),
        ).fetchall()

    return {
        str(row["post_id"])
        for row in rows
    }


def registrar_publicacion(
    chat_id: int,
    username: str,
    post_id: str,
    *,
    fecha_publicacion: str,
    cantidad_archivos: int,
) -> None:

    with _LOCK, conectar() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO publicaciones_descargadas
            (
                chat_id,
                username,
                post_id,
                fecha_publicacion,
                cantidad_archivos,
                descargada_en
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(chat_id),
                str(username).lower(),
                str(post_id),
                str(fecha_publicacion),
                max(0, int(cantidad_archivos)),
                ahora_iso(),
            ),
        )
        conn.commit()



def publicaciones_historial_completo(
    chat_id: int,
    username: str,
) -> bool:
    """Indica si ya se recorrió hasta el final el feed del perfil."""
    with _LOCK, conectar() as conn:
        row = conn.execute(
            """
            SELECT historial_completo
            FROM publicaciones_sync_perfil
            WHERE chat_id = ?
              AND username = ?
            LIMIT 1
            """,
            (
                int(chat_id),
                str(username).lower(),
            ),
        ).fetchone()

    return bool(row and int(row["historial_completo"]))


def marcar_publicaciones_historial_completo(
    chat_id: int,
    username: str,
) -> None:
    with _LOCK, conectar() as conn:
        conn.execute(
            """
            INSERT INTO publicaciones_sync_perfil
            (
                chat_id,
                username,
                historial_completo,
                actualizado_en
            )
            VALUES (?, ?, 1, ?)
            ON CONFLICT(chat_id, username)
            DO UPDATE SET
                historial_completo = 1,
                actualizado_en = excluded.actualizado_en
            """,
            (
                int(chat_id),
                str(username).lower(),
                ahora_iso(),
            ),
        )
        conn.commit()


def limpiar_antirepeticion_perfil(
    chat_id: int,
    username: str,
    tipo: str = "todo",
) -> dict[str, int]:
    """
    Elimina los IDs del tipo elegido para un perfil y chat concretos.

    No elimina la cuenta, programaciones, archivos, sesión ni registros de
    Telegram. Al resetear publicaciones (o todo), también borra su estado de
    backfill para recorrer de nuevo el historial de publicaciones.
    """
    tablas = {
        "historias": "historias_enviadas",
        "publicaciones": "publicaciones_descargadas",
        "destacadas": "historias_destacadas_descargadas",
    }
    if tipo not in {"todo", *tablas}:
        raise ValueError("Tipo de antirepetición inválido.")

    chat_id = int(chat_id)
    username = str(username).lower()
    eliminados = dict.fromkeys(tablas, 0)

    with _LOCK, conectar() as conn:
        for nombre, tabla in tablas.items():
            if tipo not in {"todo", nombre}:
                continue
            # Los nombres de tabla provienen exclusivamente del mapa fijo.
            eliminados[nombre] = int(conn.execute(
                f"SELECT COUNT(*) AS total FROM {tabla} "
                "WHERE chat_id = ? AND username = ?",
                (chat_id, username),
            ).fetchone()["total"])
            conn.execute(
                f"DELETE FROM {tabla} WHERE chat_id = ? AND username = ?",
                (chat_id, username),
            )

        if tipo in {"todo", "publicaciones"}:
            conn.execute(
                "DELETE FROM publicaciones_sync_perfil "
                "WHERE chat_id = ? AND username = ?",
                (chat_id, username),
            )

        conn.commit()

    return eliminados


# ============================================================
# PROGRAMACIONES
# ============================================================

def guardar_programacion(
    chat_id: int,
    username: str,
    horarios: list[str],
    *,
    modalidad_sesiones: str = "normal",
    sesiones_rotacion: list[str] | None = None,
) -> None:
    """
    Guarda la modalidad clásica de horarios fijos por día.

    Si la cuenta tenía una programación por intervalo, vuelve a
    modalidad "horarios". Se conserva la preferencia actual de
    notificaciones multimedia.
    """

    horarios_limpios = sorted(
        {
            str(hora)
            for hora in horarios
            if str(hora).strip()
        }
    )
    modalidad_sesiones, sesiones = _validar_modalidad_sesiones(
        modalidad_sesiones, sesiones_rotacion
    )

    with _LOCK, conectar() as conn:
        conn.execute(
            """
            INSERT INTO programaciones
            (
                chat_id,
                username,
                horarios_json,
                activa,
                creada_en,
                notificacion_activada,
                tipo_programacion,
                intervalo_horas,
                intervalo_inicio,
                modalidad_sesiones,
                sesiones_rotacion_json,
                ultima_sesion_rotacion
            )
            VALUES (?, ?, ?, 1, ?, 1, 'horarios', NULL, NULL, ?, ?, NULL)

            ON CONFLICT(
                chat_id,
                username
            )
            DO UPDATE SET
                horarios_json = excluded.horarios_json,
                activa = 1,
                tipo_programacion = 'horarios',
                intervalo_horas = NULL,
                intervalo_inicio = NULL,
                modalidad_sesiones = excluded.modalidad_sesiones,
                sesiones_rotacion_json = excluded.sesiones_rotacion_json,
                ultima_sesion_rotacion = NULL
            """,
            (
                int(chat_id),
                str(username).lower(),
                json.dumps(
                    horarios_limpios,
                    ensure_ascii=False,
                ),
                ahora_iso(),
                modalidad_sesiones,
                json.dumps(sesiones, ensure_ascii=False),
            ),
        )
        conn.commit()


def guardar_programacion_intervalo(
    chat_id: int,
    username: str,
    intervalo_horas: int,
    *,
    inicio_iso: str | None = None,
    modalidad_sesiones: str = "normal",
    sesiones_rotacion: list[str] | None = None,
) -> None:
    """
    Guarda una programación cada N horas.

    El instante de inicio es el momento exacto en que se crea la
    programación. La primera revisión ocurre N horas después.
    """

    intervalo_horas = int(intervalo_horas)

    if not 1 <= intervalo_horas <= 12:
        raise ValueError(
            "intervalo_horas debe estar entre 1 y 12"
        )

    if inicio_iso is None:
        inicio_iso = ahora_iso()

    inicio = datetime.fromisoformat(
        str(inicio_iso)
    )

    if inicio.tzinfo is None:
        inicio = inicio.replace(
            tzinfo=timezone.utc
        )

    inicio_normalizado = inicio.astimezone(
        timezone.utc
    ).isoformat()
    modalidad_sesiones, sesiones = _validar_modalidad_sesiones(
        modalidad_sesiones, sesiones_rotacion
    )

    with _LOCK, conectar() as conn:
        conn.execute(
            """
            INSERT INTO programaciones
            (
                chat_id,
                username,
                horarios_json,
                activa,
                creada_en,
                notificacion_activada,
                tipo_programacion,
                intervalo_horas,
                intervalo_inicio,
                modalidad_sesiones,
                sesiones_rotacion_json,
                ultima_sesion_rotacion
            )
            VALUES (?, ?, '[]', 1, ?, 1, 'intervalo', ?, ?, ?, ?, NULL)

            ON CONFLICT(
                chat_id,
                username
            )
            DO UPDATE SET
                horarios_json = '[]',
                activa = 1,
                tipo_programacion = 'intervalo',
                intervalo_horas = excluded.intervalo_horas,
                intervalo_inicio = excluded.intervalo_inicio,
                modalidad_sesiones = excluded.modalidad_sesiones,
                sesiones_rotacion_json = excluded.sesiones_rotacion_json,
                ultima_sesion_rotacion = NULL
            """,
            (
                int(chat_id),
                str(username).lower(),
                ahora_iso(),
                intervalo_horas,
                inicio_normalizado,
                modalidad_sesiones,
                json.dumps(sesiones, ensure_ascii=False),
            ),
        )
        conn.commit()


def _normalizar_sesiones_rotacion(sesiones) -> list[str]:
    if not isinstance(sesiones, (list, tuple)):
        raise ValueError("sesiones_rotacion debe ser una lista de sesiones")
    resultado = []
    for sesion_id in sesiones:
        if not isinstance(sesion_id, str):
            raise ValueError("La rotación contiene un identificador de sesión inválido")
        sesion_id = sesion_id.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", sesion_id):
            raise ValueError("La rotación contiene un identificador de sesión inválido")
        if sesion_id not in resultado:
            resultado.append(sesion_id)
    return resultado


def _validar_modalidad_sesiones(modalidad, sesiones) -> tuple[str, list[str]]:
    if not isinstance(modalidad, str) or modalidad not in {"normal", "variable"}:
        raise ValueError("modalidad_sesiones debe ser 'normal' o 'variable'")
    if modalidad == "normal":
        return modalidad, []
    sesiones = _normalizar_sesiones_rotacion(sesiones)
    if not sesiones:
        raise ValueError("Una programación variable necesita al menos una sesión con acceso")
    return modalidad, sesiones


def modalidad_sesiones_de(row) -> str:
    """Una fila de una versión anterior sigue usando su sesión preferida."""
    try:
        valor = row["modalidad_sesiones"]
    except (KeyError, IndexError, TypeError):
        return "normal"
    return "variable" if valor == "variable" else "normal"


def sesiones_rotacion_de(row) -> list[str]:
    """Lee sólo el conjunto validado de esta programación, sin otras sesiones."""
    if modalidad_sesiones_de(row) != "variable":
        return []
    try:
        return _normalizar_sesiones_rotacion(json.loads(row["sesiones_rotacion_json"]))
    except (KeyError, IndexError, TypeError, ValueError):
        return []


def preparar_rotacion(chat_id: int, username: str) -> list[str]:
    """Ordena el próximo intento después de la última sesión realmente usada.

    Leer el orden no lo avanza. El ejecutor confirma la sesión al terminar
    cada revisión; así un fallback también determina el siguiente turno.
    """
    with _LOCK, conectar() as conn:
        row = conn.execute(
            "SELECT * FROM programaciones WHERE chat_id = ? AND username = ?",
            (int(chat_id), str(username).lower()),
        ).fetchone()
        sesiones = sesiones_rotacion_de(row)
        if not sesiones:
            return []
        ultima = row["ultima_sesion_rotacion"]
        if ultima not in sesiones:
            return sesiones
        siguiente = sesiones.index(ultima) + 1
        return sesiones[siguiente:] + sesiones[:siguiente]


def registrar_sesion_rotacion(chat_id: int, username: str, sesion_id: str) -> bool:
    """Avanza sólo esta programación y sólo dentro de su conjunto autorizado."""
    if not isinstance(sesion_id, str):
        return False
    sesion_id = sesion_id.strip().lower()
    with _LOCK, conectar() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM programaciones WHERE chat_id = ? AND username = ?",
            (int(chat_id), str(username).lower()),
        ).fetchone()
        if sesion_id not in sesiones_rotacion_de(row):
            return False
        conn.execute(
            """UPDATE programaciones SET ultima_sesion_rotacion = ?
               WHERE chat_id = ? AND username = ?""",
            (sesion_id, int(chat_id), str(username).lower()),
        )
        conn.commit()
        return True


def tipo_programacion(row) -> str:
    """
    Compatibilidad con bases anteriores:
    toda programación vieja se considera de horarios fijos.
    """
    try:
        valor = str(
            row["tipo_programacion"]
        ).strip().lower()
    except Exception:
        return "horarios"

    if valor == "intervalo":
        return "intervalo"

    return "horarios"


def intervalo_horas_de(row) -> int | None:
    if tipo_programacion(row) != "intervalo":
        return None

    try:
        valor = int(
            row["intervalo_horas"]
        )
    except Exception:
        return None

    if valor <= 0:
        return None

    return valor


def inicio_intervalo_de(
    row,
) -> datetime | None:
    if tipo_programacion(row) != "intervalo":
        return None

    try:
        texto = str(
            row["intervalo_inicio"]
        ).strip()

        if not texto:
            return None

        fecha = datetime.fromisoformat(
            texto
        )

        if fecha.tzinfo is None:
            fecha = fecha.replace(
                tzinfo=timezone.utc
            )

        return fecha.astimezone(
            timezone.utc
        )
    except Exception:
        return None


def proxima_ejecucion_intervalo(
    row,
    *,
    ahora: datetime | None = None,
) -> datetime | None:
    """
    Calcula el siguiente punto de la cadencia sin modificar la DB.

    Ejemplo:
      inicio 22:45 + intervalo 2 h
      -> 00:45, 02:45, 04:45...
    """
    horas = intervalo_horas_de(
        row
    )
    inicio = inicio_intervalo_de(
        row
    )

    if horas is None or inicio is None:
        return None

    if ahora is None:
        ahora = datetime.now(
            timezone.utc
        )
    elif ahora.tzinfo is None:
        ahora = ahora.replace(
            tzinfo=timezone.utc
        )
    else:
        ahora = ahora.astimezone(
            timezone.utc
        )

    segundos_intervalo = (
        horas * 60 * 60
    )

    transcurrido = max(
        0.0,
        (
            ahora - inicio
        ).total_seconds(),
    )

    pasos = (
        int(
            transcurrido
            // segundos_intervalo
        )
        + 1
    )

    return inicio + timedelta(
        seconds=(
            pasos
            * segundos_intervalo
        )
    )


def obtener_programacion(
    chat_id: int,
    username: str,
):
    with _LOCK, conectar() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM programaciones
            WHERE chat_id = ?
              AND username = ?
            LIMIT 1
            """,
            (
                int(chat_id),
                str(username).lower(),
            ),
        ).fetchone()

    return row


def listar_programaciones(
    chat_id: int,
):
    with _LOCK, conectar() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM programaciones
            WHERE chat_id = ?
            ORDER BY username COLLATE NOCASE
            """,
            (
                int(chat_id),
            ),
        ).fetchall()

    return rows


def listar_programaciones_activas():
    with _LOCK, conectar() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM programaciones
            WHERE activa = 1
            ORDER BY
                chat_id,
                username COLLATE NOCASE
            """
        ).fetchall()

    return rows


def horarios_de(
    row,
) -> list[str]:

    if row is None:
        return []

    try:
        data = json.loads(
            row["horarios_json"]
        )
    except Exception:
        return []

    if not isinstance(
        data,
        list,
    ):
        return []

    return [
        str(item)
        for item in data
        if isinstance(
            item,
            str,
        )
    ]


def notificacion_activada(
    row,
) -> bool:

    if row is None:
        return True

    try:
        return bool(
            int(
                row["notificacion_activada"]
            )
        )
    except Exception:
        # Compatibilidad defensiva con registros/versiones viejas.
        return True


def cambiar_estado_programacion(
    chat_id: int,
    username: str,
    activa: bool,
) -> None:

    with _LOCK, conectar() as conn:
        conn.execute(
            """
            UPDATE programaciones
            SET activa = ?
            WHERE chat_id = ?
              AND username = ?
            """,
            (
                1 if activa else 0,
                int(chat_id),
                str(username).lower(),
            ),
        )
        conn.commit()


def cambiar_notificacion_programacion(
    chat_id: int,
    username: str,
    activada: bool,
) -> None:

    with _LOCK, conectar() as conn:
        conn.execute(
            """
            UPDATE programaciones
            SET notificacion_activada = ?
            WHERE chat_id = ?
              AND username = ?
            """,
            (
                1 if activada else 0,
                int(chat_id),
                str(username).lower(),
            ),
        )
        conn.commit()


def eliminar_programacion(
    chat_id: int,
    username: str,
) -> None:

    with _LOCK, conectar() as conn:
        conn.execute(
            """
            DELETE FROM programaciones
            WHERE chat_id = ?
              AND username = ?
            """,
            (
                int(chat_id),
                str(username).lower(),
            ),
        )
        conn.commit()


# ============================================================
# MULTIMEDIA DE TELEGRAM
# ============================================================

def registrar_multimedia_telegram(
    chat_id: int,
    message_id: int,
) -> None:
    """
    Registra una foto/video enviado por StoryPulse.

    También lo marca automáticamente como PROTEGIDO para que
    LIMPIAR CHAT nunca intente borrarlo.
    """
    fecha = ahora_iso()

    with _LOCK, conectar() as conn:

        conn.execute(
            """
            INSERT OR REPLACE INTO multimedia_telegram
            (
                chat_id,
                message_id,
                enviada_en
            )
            VALUES (?, ?, ?)
            """,
            (
                int(chat_id),
                int(message_id),
                fecha,
            ),
        )

        conn.execute(
            """
            INSERT OR REPLACE INTO multimedia_protegida_chat
            (
                chat_id,
                message_id,
                creada_en
            )
            VALUES (?, ?, ?)
            """,
            (
                int(chat_id),
                int(message_id),
                fecha,
            ),
        )

        conn.commit()


def multimedia_reciente_telegram(
    chat_id: int,
    *,
    limite: int = 100,
    horas: int = 48,
) -> list[int]:

    limite = max(
        1,
        min(
            int(limite),
            100,
        ),
    )

    corte = (
        datetime.now(
            timezone.utc
        )
        - timedelta(
            hours=int(horas)
        )
    ).isoformat()

    with _LOCK, conectar() as conn:
        rows = conn.execute(
            """
            SELECT message_id
            FROM multimedia_telegram
            WHERE chat_id = ?
              AND enviada_en >= ?
            ORDER BY enviada_en DESC
            LIMIT ?
            """,
            (
                int(chat_id),
                corte,
                limite,
            ),
        ).fetchall()

    return [
        int(row["message_id"])
        for row in rows
    ]


def eliminar_registros_multimedia_telegram(
    chat_id: int,
    message_ids: list[int],
) -> None:

    ids = [
        int(message_id)
        for message_id in message_ids
    ]

    if not ids:
        return

    placeholders = ",".join(
        "?"
        for _ in ids
    )

    parametros = [
        int(chat_id),
        *ids,
    ]

    with _LOCK, conectar() as conn:

        conn.execute(
            f"""
            DELETE FROM multimedia_telegram
            WHERE chat_id = ?
              AND message_id IN ({placeholders})
            """,
            parametros,
        )

        # Si BORRAR MULTIMEDIA ya quitó esos mensajes de Telegram,
        # tampoco hace falta conservarlos como protegidos.
        conn.execute(
            f"""
            DELETE FROM multimedia_protegida_chat
            WHERE chat_id = ?
              AND message_id IN ({placeholders})
            """,
            parametros,
        )

        conn.commit()


# ============================================================
# MENSAJES DE TEXTO REGISTRADOS
# ============================================================

def registrar_mensaje_chat_limpiable(
    chat_id: int,
    message_id: int,
) -> None:

    with _LOCK, conectar() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO mensajes_chat_limpiables
            (
                chat_id,
                message_id,
                creada_en
            )
            VALUES (?, ?, ?)
            """,
            (
                int(chat_id),
                int(message_id),
                ahora_iso(),
            ),
        )
        conn.commit()


def mensajes_chat_limpiables_recientes(
    chat_id: int,
    *,
    horas: int = 48,
) -> list[int]:

    corte = (
        datetime.now(
            timezone.utc
        )
        - timedelta(
            hours=int(horas)
        )
    ).isoformat()

    with _LOCK, conectar() as conn:
        rows = conn.execute(
            """
            SELECT message_id
            FROM mensajes_chat_limpiables
            WHERE chat_id = ?
              AND creada_en >= ?
            ORDER BY message_id DESC
            """,
            (
                int(chat_id),
                corte,
            ),
        ).fetchall()

    return [
        int(row["message_id"])
        for row in rows
    ]


def eliminar_registros_chat_limpiables(
    chat_id: int,
    message_ids: list[int],
) -> None:

    ids = [
        int(message_id)
        for message_id in message_ids
    ]

    if not ids:
        return

    placeholders = ",".join(
        "?"
        for _ in ids
    )

    with _LOCK, conectar() as conn:
        conn.execute(
            f"""
            DELETE FROM mensajes_chat_limpiables
            WHERE chat_id = ?
              AND message_id IN ({placeholders})
            """,
            [
                int(chat_id),
                *ids,
            ],
        )
        conn.commit()


def purgar_registros_chat_limpiables_vencidos(
    chat_id: int,
    *,
    horas: int = 48,
) -> None:

    corte = (
        datetime.now(
            timezone.utc
        )
        - timedelta(
            hours=int(horas)
        )
    ).isoformat()

    with _LOCK, conectar() as conn:
        conn.execute(
            """
            DELETE FROM mensajes_chat_limpiables
            WHERE chat_id = ?
              AND creada_en < ?
            """,
            (
                int(chat_id),
                corte,
            ),
        )
        conn.commit()


# ============================================================
# MULTIMEDIA PROTEGIDA PARA "LIMPIAR CHAT"
# ============================================================

def registrar_multimedia_protegida_chat(
    chat_id: int,
    message_id: int,
) -> None:

    with _LOCK, conectar() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO multimedia_protegida_chat
            (
                chat_id,
                message_id,
                creada_en
            )
            VALUES (?, ?, ?)
            """,
            (
                int(chat_id),
                int(message_id),
                ahora_iso(),
            ),
        )
        conn.commit()


def ids_multimedia_protegida_chat(
    chat_id: int,
) -> set[int]:
    """
    Devuelve todos los message_id que LIMPIAR CHAT debe preservar.
    """

    with _LOCK, conectar() as conn:
        rows = conn.execute(
            """
            SELECT message_id
            FROM multimedia_telegram
            WHERE chat_id = ?

            UNION

            SELECT message_id
            FROM multimedia_protegida_chat
            WHERE chat_id = ?
            """,
            (
                int(chat_id),
                int(chat_id),
            ),
        ).fetchall()

    return {
        int(row["message_id"])
        for row in rows
    }

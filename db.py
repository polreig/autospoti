"""Conexión a la base de datos: SQLite (local y tests) o Postgres (nube, ``DATABASE_URL``).

El resto del código escribe SQL portable con ``?`` como marcador; aquí se traduce a
``%s`` para Postgres. Diferencias que se evitan a propósito en las consultas:
``INSERT OR IGNORE`` (se usa ``ON CONFLICT DO NOTHING``), ``PRAGMA`` y ``executescript``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from config import get_database_url, get_db_path

# Versión del esquema SQLite (PRAGMA user_version). 0/1 = esquema antiguo sin user_id.
SCHEMA_VERSION = 2

PLAYS_TABLE = """
CREATE TABLE IF NOT EXISTS plays (
    user_id     TEXT    NOT NULL,
    ts          BIGINT  NOT NULL,
    track_uri   TEXT    NOT NULL,
    track_name  TEXT,
    artist      TEXT,
    album       TEXT,
    ms_played   INTEGER,
    source      TEXT    NOT NULL,
    skipped     INTEGER,            -- 1/0; NULL si se desconoce (p. ej. registro por API)
    reason_end  TEXT,               -- trackdone, fwdbtn, endplay... (solo export)
    shuffle     INTEGER,            -- 1/0; NULL si se desconoce
    platform    TEXT,
    PRIMARY KEY (user_id, ts, track_uri)
)
"""

SCHEMA_STATEMENTS = (
    PLAYS_TABLE,
    "CREATE INDEX IF NOT EXISTS idx_plays_user_uri_ts ON plays (user_id, track_uri, ts)",
    "CREATE INDEX IF NOT EXISTS idx_plays_user_ts ON plays (user_id, ts)",
    # Playlists que gestiona la app: se recuerda su ID para no depender solo del nombre.
    """
    CREATE TABLE IF NOT EXISTS managed_playlists (
        user_id     TEXT NOT NULL,
        name        TEXT NOT NULL,
        playlist_id TEXT NOT NULL,
        PRIMARY KEY (user_id, name)
    )
    """,
    # Tokens de Spotify por usuario (cifrados, ver tokens.py). Solo en modo nube.
    """
    CREATE TABLE IF NOT EXISTS spotify_tokens (
        user_id     TEXT   PRIMARY KEY,
        token       TEXT   NOT NULL,
        updated_at  BIGINT NOT NULL
    )
    """,
)


def is_postgres_url(target: str) -> bool:
    return target.startswith(("postgres://", "postgresql://"))


class Database:
    """Envoltorio mínimo sobre sqlite3 / psycopg con la misma interfaz para ambos."""

    def __init__(self, raw: Any, dialect: str, target: str, check_same_thread: bool = True):
        self.raw = raw
        self.dialect = dialect          # "sqlite" | "postgres"
        self._target = target
        self._check_same_thread = check_same_thread

    @property
    def is_postgres(self) -> bool:
        return self.dialect == "postgres"

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_postgres else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        try:
            return self.raw.execute(self._sql(sql), tuple(params))
        except Exception:
            self._rollback_quietly()
            raise

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        try:
            if self.is_postgres:
                with self.raw.cursor() as cur:
                    cur.executemany(self._sql(sql), list(seq))
            else:
                self.raw.executemany(sql, seq)
        except Exception:
            self._rollback_quietly()
            raise

    def copy_rows(self, table: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
        """Carga masiva: COPY en Postgres (rápido por red), executemany en SQLite."""
        if not self.is_postgres:
            marks = ", ".join("?" for _ in columns)
            self.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})", rows)
            return
        try:
            with self.raw.cursor() as cur:
                with cur.copy(f"COPY {table} ({', '.join(columns)}) FROM STDIN") as copy:
                    for row in rows:
                        copy.write_row(row)
        except Exception:
            self._rollback_quietly()
            raise

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def _rollback_quietly(self) -> None:
        # En Postgres, tras un error la transacción queda abortada hasta hacer rollback.
        try:
            self.raw.rollback()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self.raw.close()

    def ensure_alive(self) -> "Database":
        """Postgres gestionado (Neon...) corta conexiones inactivas: reconecta si hace falta."""
        if not self.is_postgres:
            return self
        try:
            self.raw.execute("SELECT 1")
        except Exception:  # noqa: BLE001
            try:
                self.raw.close()
            except Exception:  # noqa: BLE001
                pass
            self.raw = _connect_postgres(self._target)
        return self


def _connect_postgres(url: str) -> Any:
    import psycopg  # import perezoso: en local con SQLite no hace falta

    # autocommit: cada sentencia se confirma sola; una conexión de la app que se queda
    # abierta no deja transacciones "idle in transaction" en el servidor.
    return psycopg.connect(url, connect_timeout=15, autocommit=True)


def _needs_v2_migration(raw: sqlite3.Connection) -> bool:
    cols = {row[1] for row in raw.execute("PRAGMA table_info(plays)")}
    return bool(cols) and "user_id" not in cols


def _backup_sqlite(raw: sqlite3.Connection, target: str) -> None:
    """Copia de seguridad antes de migrar (solo bases de datos en disco)."""
    if target == ":memory:" or not Path(target).exists():
        return
    backup = Path(target).with_suffix(".v1-backup.db")
    if not backup.exists():
        raw.commit()
        dest = sqlite3.connect(str(backup))
        try:
            raw.backup(dest)
        finally:
            dest.close()


def _migrate_sqlite_to_v2(raw: sqlite3.Connection) -> None:
    """Esquema antiguo (sin user_id) -> v2. Las filas quedan huérfanas (user_id = '')
    hasta que ``history.claim_orphan_plays`` las asigna al dueño."""
    raw.executescript(
        "BEGIN;\n"
        "ALTER TABLE plays RENAME TO plays_v1;\n"
        + PLAYS_TABLE + ";\n"
        "INSERT INTO plays (user_id, ts, track_uri, track_name, artist, album, ms_played, source)\n"
        "    SELECT '', ts, track_uri, track_name, artist, album, ms_played, source FROM plays_v1;\n"
        "DROP TABLE plays_v1;\n"
        "COMMIT;"
    )


def _connect_sqlite(target: str, check_same_thread: bool) -> sqlite3.Connection:
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(target, check_same_thread=check_same_thread)
    if target != ":memory:":
        # WAL: lecturas concurrentes (app web + cron) sin bloquearse mutuamente.
        raw.execute("PRAGMA journal_mode=WAL")
    if _needs_v2_migration(raw):
        _backup_sqlite(raw, target)
        _migrate_sqlite_to_v2(raw)
    return raw


def create_schema(db: Database) -> None:
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)
    if not db.is_postgres:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.commit()


def connect(target: "str | Path | None" = None, check_same_thread: bool = True) -> Database:
    """Abre la BD: ``target`` explícito, si no ``DATABASE_URL``, si no ``data/history.db``."""
    if target is None:
        target = get_database_url() or get_db_path()
    target = str(target)
    if is_postgres_url(target):
        db = Database(_connect_postgres(target), "postgres", target)
    else:
        db = Database(_connect_sqlite(target, check_same_thread), "sqlite", target, check_same_thread)
    create_schema(db)
    return db

"""Database access: connection pool, schema bootstrap, and queries."""

from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    url         TEXT        NOT NULL UNIQUE,
    title       TEXT        NOT NULL DEFAULT '',
    description TEXT        NOT NULL DEFAULT '',
    tags        TEXT[]      NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bookmarks_tags_gin ON bookmarks USING GIN (tags);
CREATE INDEX IF NOT EXISTS bookmarks_created_at ON bookmarks (created_at DESC);
"""

COLUMNS = "id, url, title, description, tags, created_at"


def create_pool(settings: Settings) -> ConnectionPool:
    conninfo = make_conninfo(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user=settings.db_user,
        password=settings.db_password,
        connect_timeout=5,
        application_name="bookmarks",
    )
    return ConnectionPool(
        conninfo,
        min_size=1,
        max_size=settings.db_pool_size,
        kwargs={"row_factory": dict_row},
        open=False,
        # Fail requests with a 503 after 10s instead of hanging while the DB is down.
        timeout=10,
        # Drop connections that died (e.g. after a database restart) before use.
        check=ConnectionPool.check_connection,
    )


def migrate(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        # Serialise schema setup in case several app replicas start together.
        conn.execute("SELECT pg_advisory_xact_lock(724100)")
        conn.execute(SCHEMA)


def ping(pool: ConnectionPool) -> None:
    with pool.connection(timeout=3) as conn:
        conn.execute("SELECT 1")


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def list_bookmarks(pool: ConnectionPool, q: str | None, tag: str | None, limit: int, offset: int) -> list[dict]:
    where, params = [], []
    if q:
        where.append("(url ILIKE %s OR title ILIKE %s OR description ILIKE %s)")
        params += [_like(q)] * 3
    if tag:
        where.append("tags @> ARRAY[%s]::text[]")
        params.append(tag)
    sql = f"SELECT {COLUMNS} FROM bookmarks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s"
    params += [limit, offset]
    with pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


def get_bookmark(pool: ConnectionPool, bookmark_id: int) -> dict | None:
    with pool.connection() as conn:
        return conn.execute(f"SELECT {COLUMNS} FROM bookmarks WHERE id = %s", [bookmark_id]).fetchone()


def insert_bookmark(pool: ConnectionPool, url: str, title: str, description: str, tags: list[str]) -> dict:
    with pool.connection() as conn:
        return conn.execute(
            f"INSERT INTO bookmarks (url, title, description, tags) VALUES (%s, %s, %s, %s) RETURNING {COLUMNS}",
            [url, title, description, tags],
        ).fetchone()


def update_bookmark(pool: ConnectionPool, bookmark_id: int, fields: dict) -> dict | None:
    if not fields:
        return get_bookmark(pool, bookmark_id)
    # Column names come from a fixed whitelist in the API model, never from user input.
    assignments = ", ".join(f"{column} = %s" for column in fields)
    with pool.connection() as conn:
        return conn.execute(
            f"UPDATE bookmarks SET {assignments} WHERE id = %s RETURNING {COLUMNS}",
            [*fields.values(), bookmark_id],
        ).fetchone()


def delete_bookmark(pool: ConnectionPool, bookmark_id: int) -> bool:
    with pool.connection() as conn:
        return conn.execute("DELETE FROM bookmarks WHERE id = %s", [bookmark_id]).rowcount == 1


def list_tags(pool: ConnectionPool) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(
            "SELECT tag, count(*) AS count FROM bookmarks, unnest(tags) AS tag GROUP BY tag ORDER BY count DESC, tag"
        ).fetchall()

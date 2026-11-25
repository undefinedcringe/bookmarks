"""Bookmark manager: JSON API under /api, a static web UI at /, and /health."""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import psycopg
from fastapi import FastAPI, HTTPException, Path as PathParam, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from app import db
from app.config import load_settings

log = logging.getLogger("bookmarks")
STATIC_DIR = Path(__file__).parent / "static"

MAX_TAGS = 20
MAX_TAG_LEN = 50
MAX_OFFSET = 10_000_000
MAX_ID = 2**63 - 1  # Postgres BIGINT
# PostgreSQL text cannot hold NUL bytes; reject them as bad input instead of failing with a 500.
NO_NUL = r"^[^\x00]*$"
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
# The UI page only talks to its own origin; inline script/style live in index.html.
INDEX_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    pool = db.create_pool(settings)
    # Fail fast: if the database is unreachable at startup, exit and let the
    # restart policy retry, rather than serving a half-working app.
    pool.open(wait=True, timeout=30)
    db.migrate(pool)
    app.state.allowed_hosts = None if "*" in settings.allowed_hosts else LOOPBACK_HOSTS | set(settings.allowed_hosts)
    log.info("connected to database %s@%s:%s/%s", settings.db_user, settings.db_host, settings.db_port, settings.db_name)
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Bookmarks", version="1.0.0", lifespan=lifespan)


def _host_name(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # IPv6 literal, e.g. [::1]:8080
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0]


@app.middleware("http")
async def host_check_and_headers(request: Request, call_next):
    # The app has no login, so only answer to known host names. This stops DNS
    # rebinding, where a web page re-points its own domain at 127.0.0.1 to read
    # or delete bookmarks from the visitor's browser.
    allowed = getattr(request.app.state, "allowed_hosts", None)
    if allowed is not None and _host_name(request.headers.get("host", "")) not in allowed:
        return JSONResponse(status_code=400, content={"detail": "host not allowed; add it to ALLOWED_HOSTS"})
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if request.url.path == "/":
        response.headers["Content-Security-Policy"] = INDEX_CSP
    return response


# ---------- models ----------

def _clean_url(value: str) -> str:
    value = value.strip()
    if "\x00" in value:
        raise ValueError("must not contain NUL characters")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("must be an absolute http:// or https:// URL")
    return value


def _clean_tags(values: list[str]) -> list[str]:
    seen: list[str] = []
    for raw in values:
        tag = raw.strip().lower()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LEN or "," in tag or "\x00" in tag:
            raise ValueError(f"tags must be at most {MAX_TAG_LEN} characters and contain no commas")
        if tag not in seen:
            seen.append(tag)
    if len(seen) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags allowed")
    return seen


class BookmarkIn(BaseModel):
    url: str = Field(max_length=2048)
    title: str = Field(default="", max_length=500, pattern=NO_NUL)
    description: str = Field(default="", max_length=5000, pattern=NO_NUL)
    tags: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("url")
    @classmethod
    def check_url(cls, v: str) -> str:
        return _clean_url(v)

    @field_validator("tags")
    @classmethod
    def check_tags(cls, v: list[str]) -> list[str]:
        return _clean_tags(v)


class BookmarkPatch(BaseModel):
    title: str | None = Field(default=None, max_length=500, pattern=NO_NUL)
    description: str | None = Field(default=None, max_length=5000, pattern=NO_NUL)
    tags: list[str] | None = Field(default=None, max_length=100)

    @field_validator("tags")
    @classmethod
    def check_tags(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_tags(v)


BookmarkId = Annotated[int, PathParam(ge=1, le=MAX_ID)]


class Bookmark(BaseModel):
    id: int
    url: str
    title: str
    description: str
    tags: list[str]
    created_at: datetime


class Tag(BaseModel):
    tag: str
    count: int


# ---------- error handling ----------

@app.exception_handler(psycopg.OperationalError)
async def db_unavailable(request: Request, exc: psycopg.OperationalError):
    log.error("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# ---------- routes ----------

@app.get("/health", include_in_schema=False)
def health(request: Request, response: Response):
    try:
        db.ping(request.app.state.pool)
    except Exception as exc:  # any failure to reach the DB means unhealthy
        log.warning("health check failed: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "database": "unreachable"}
    return {"status": "ok", "database": "ok"}


@app.get("/api/bookmarks", response_model=list[Bookmark])
def list_bookmarks(
    request: Request,
    q: str | None = Query(default=None, max_length=200, pattern=NO_NUL, description="Text search in URL, title and description"),
    tag: str | None = Query(default=None, max_length=MAX_TAG_LEN, pattern=NO_NUL, description="Only bookmarks with this tag"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=MAX_OFFSET),
):
    tag = tag.strip().lower() if tag else None
    return db.list_bookmarks(request.app.state.pool, q.strip() if q else None, tag, limit, offset)


@app.post("/api/bookmarks", response_model=Bookmark, status_code=status.HTTP_201_CREATED)
def create_bookmark(request: Request, body: BookmarkIn):
    try:
        return db.insert_bookmark(request.app.state.pool, body.url, body.title.strip(), body.description.strip(), body.tags)
    except psycopg.errors.UniqueViolation:
        raise HTTPException(status.HTTP_409_CONFLICT, "a bookmark with this URL already exists")


@app.get("/api/bookmarks/{bookmark_id}", response_model=Bookmark)
def get_bookmark(request: Request, bookmark_id: BookmarkId):
    row = db.get_bookmark(request.app.state.pool, bookmark_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bookmark not found")
    return row


@app.patch("/api/bookmarks/{bookmark_id}", response_model=Bookmark)
def patch_bookmark(request: Request, bookmark_id: BookmarkId, body: BookmarkPatch):
    fields = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    row = db.update_bookmark(request.app.state.pool, bookmark_id, fields)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bookmark not found")
    return row


@app.delete("/api/bookmarks/{bookmark_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bookmark(request: Request, bookmark_id: BookmarkId):
    if not db.delete_bookmark(request.app.state.pool, bookmark_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bookmark not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/tags", response_model=list[Tag])
def list_tags(request: Request):
    return db.list_tags(request.app.state.pool)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")

"""Bookmark manager: JSON API under /api, a static web UI at /, and /health."""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from app import db
from app.config import load_settings

log = logging.getLogger("bookmarks")
STATIC_DIR = Path(__file__).parent / "static"

MAX_TAGS = 20
MAX_TAG_LEN = 50


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    pool = db.create_pool(settings)
    # Fail fast: if the database is unreachable at startup, exit and let the
    # restart policy retry, rather than serving a half-working app.
    pool.open(wait=True, timeout=30)
    db.migrate(pool)
    log.info("connected to database %s@%s:%s/%s", settings.db_user, settings.db_host, settings.db_port, settings.db_name)
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Bookmarks", version="1.0.0", lifespan=lifespan)


# ---------- models ----------

def _clean_url(value: str) -> str:
    value = value.strip()
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
        if len(tag) > MAX_TAG_LEN or "," in tag:
            raise ValueError(f"tags must be at most {MAX_TAG_LEN} characters and contain no commas")
        if tag not in seen:
            seen.append(tag)
    if len(seen) > MAX_TAGS:
        raise ValueError(f"at most {MAX_TAGS} tags allowed")
    return seen


class BookmarkIn(BaseModel):
    url: str = Field(max_length=2048)
    title: str = Field(default="", max_length=500)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list)

    @field_validator("url")
    @classmethod
    def check_url(cls, v: str) -> str:
        return _clean_url(v)

    @field_validator("tags")
    @classmethod
    def check_tags(cls, v: list[str]) -> list[str]:
        return _clean_tags(v)


class BookmarkPatch(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=5000)
    tags: list[str] | None = None

    @field_validator("tags")
    @classmethod
    def check_tags(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_tags(v)


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
    q: str | None = Query(default=None, max_length=200, description="Text search in URL, title and description"),
    tag: str | None = Query(default=None, max_length=MAX_TAG_LEN, description="Only bookmarks with this tag"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
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
def get_bookmark(request: Request, bookmark_id: int):
    row = db.get_bookmark(request.app.state.pool, bookmark_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bookmark not found")
    return row


@app.patch("/api/bookmarks/{bookmark_id}", response_model=Bookmark)
def patch_bookmark(request: Request, bookmark_id: int, body: BookmarkPatch):
    fields = {k: (v.strip() if isinstance(v, str) else v) for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    row = db.update_bookmark(request.app.state.pool, bookmark_id, fields)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bookmark not found")
    return row


@app.delete("/api/bookmarks/{bookmark_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_bookmark(request: Request, bookmark_id: int):
    if not db.delete_bookmark(request.app.state.pool, bookmark_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "bookmark not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/api/tags", response_model=list[Tag])
def list_tags(request: Request):
    return db.list_tags(request.app.state.pool)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")

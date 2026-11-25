# Deployment and operations

Configuration, API reference, backups, upgrades and troubleshooting for running
Bookmarks with Docker Compose. For an overview, see the [README](../README.md).

## Environment variables

All configuration comes from `.env`, which Compose reads automatically. Every
variable has a safe default in `.env.example`.

| Name | Required | Default (`.env.example`) | Description |
|---|---|---|---|
| `POSTGRES_USER` | **yes** | `bookmarks` | Database user. Creates the user on first start; the app connects with it. |
| `POSTGRES_PASSWORD` | **yes** | `change-me-local-only` | Database password. **Change it** before exposing the app beyond localhost (`openssl rand -base64 24`). |
| `POSTGRES_DB` | **yes** | `bookmarks` | Database name. |
| `APP_BIND_ADDRESS` | no | `127.0.0.1` | Host interface the app is published on. `127.0.0.1` = this machine only; `0.0.0.0` = all interfaces (the app has no login, see [Reverse proxy](#putting-it-behind-a-reverse-proxy--tls--auth)). |
| `APP_PORT` | no | `8080` | Host port for the UI/API. |
| `LOG_LEVEL` | no | `info` | `critical`, `error`, `warning`, `info` or `debug`. |
| `DB_POOL_SIZE` | no | `5` | Maximum DB connections the app keeps open. |
| `ALLOWED_HOSTS` | no | *(empty)* | Extra host names the app answers to, comma separated, e.g. `bookmarks.example.com,192.168.1.20`. `localhost` and `127.0.0.1` always work. Any other `Host` header gets a 400, which blocks DNS rebinding. `*` turns the check off. |

If a required variable is missing, it fails fast at two levels:

- `docker compose up` refuses to start and prints, e.g., `POSTGRES_PASSWORD is required - copy .env.example to .env`.
- The app itself validates its config before starting and exits with a list of
  every missing or invalid variable (`FATAL: Invalid configuration: - DB_PASSWORD is required but not set ...`).

Internally, Compose maps the `POSTGRES_*` values to the app's `DB_HOST`, `DB_PORT`,
`DB_NAME`, `DB_USER` and `DB_PASSWORD`. You don't set those yourself unless you run
the app outside Compose.

## JSON API

| Method | Path | Body / query | Result |
|---|---|---|---|
| `GET` | `/api/bookmarks` | `?q=text&tag=name&limit=100&offset=0` | List, newest first. `q` searches URL, title and note; `tag` filters by tag. |
| `POST` | `/api/bookmarks` | `{"url", "title"?, "description"?, "tags"?: []}` | `201` with the bookmark; `409` if the URL already exists; `422` if invalid. |
| `GET` | `/api/bookmarks/{id}` | | Bookmark or `404`. |
| `PATCH` | `/api/bookmarks/{id}` | any of `title`, `description`, `tags` | Updated bookmark (use this to re-tag). |
| `DELETE` | `/api/bookmarks/{id}` | | `204` or `404`. |
| `GET` | `/api/tags` | | `[{"tag", "count"}]`. |
| `GET` | `/health` | | `200 {"status":"ok"}` only if the DB answers `SELECT 1`, else `503`. |

```bash
curl -X POST localhost:8080/api/bookmarks -H 'Content-Type: application/json' \
  -d '{"url":"https://www.postgresql.org/docs/16/","title":"Postgres docs","tags":["db","docs"]}'
curl 'localhost:8080/api/bookmarks?q=postgres'
curl 'localhost:8080/api/bookmarks?tag=docs'
curl -X DELETE localhost:8080/api/bookmarks/1
```

Rules: URLs must be absolute `http://` or `https://`. Tags are lower-cased and
de-duplicated, at most 20 per bookmark, 50 characters each, and no commas.

## Data, backup, restore, upgrade

### Where data lives

All state lives in PostgreSQL, in the named volume **`bookmarks_pgdata`**
(mounted at `/var/lib/postgresql/data`). The app container is stateless and its
filesystem is read-only. The app has no file uploads, so no other volume is needed.

- `docker compose down` and then `docker compose up -d` keeps your data.
- `docker compose down -v` **deletes the volume and all bookmarks.** Don't use `-v` unless you mean it.

```bash
docker volume inspect bookmarks_pgdata   # shows where Docker keeps it on disk
```

### Backup

```bash
scripts/backup.sh                         # -> backups/bookmarks-20260101T120000Z.dump
```

This runs `pg_dump --format=custom` inside the `db` container while everything
stays online. Dumps go to `./backups/`, which git ignores. Copy them off the host
regularly. A nightly cron job could look like this:

```cron
15 3 * * * cd /opt/bookmarks && scripts/backup.sh >/dev/null && find backups -name '*.dump' -mtime +30 -delete
```

The same thing without the script:

```bash
docker compose exec -T db sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > backup.dump
```

### Restore

```bash
scripts/restore.sh backups/bookmarks-20260101T120000Z.dump        # asks for confirmation
scripts/restore.sh backups/bookmarks-20260101T120000Z.dump --yes  # non-interactive
```

The script stops the app, restores the dump in a single transaction (replacing
current data), then restarts the app and waits until it's healthy. The manual
equivalent:

```bash
docker compose stop app
docker compose exec -T db sh -c 'pg_restore --clean --if-exists --no-owner --single-transaction -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < backup.dump
docker compose up -d app
```

### Upgrade

**App or Postgres minor version** (e.g. `16.11` → `16.12`; the data format is compatible):

```bash
scripts/backup.sh                # always back up first
git pull                         # or edit the image tag in compose.yaml
docker compose up -d --build     # rebuilds the app and recreates changed containers
docker compose ps                # wait for "(healthy)"
```

**Postgres major version** (e.g. 16 → 17) needs a dump and restore, because the
on-disk format changes:

```bash
scripts/backup.sh
docker compose down
docker volume rm bookmarks_pgdata                  # data is now only in the backup
# edit compose.yaml: image: postgres:17.x-alpine
docker compose up -d --wait db
scripts/restore.sh backups/<latest>.dump --yes
```

## Health checks and logs

```bash
docker compose ps                         # STATUS shows (healthy) / (unhealthy) / (health: starting)
curl -i localhost:8080/health             # 200 when the app can reach the DB, 503 otherwise
docker inspect --format '{{json .State.Health}}' bookmarks-app-1   # last health check results

docker compose logs -f                    # all services
docker compose logs -f app                # just the app
docker compose logs --since 10m db        # recent DB logs
```

- **`db`** is checked with `pg_isready` over TCP. TCP is used so the check stays
  unhealthy during first-time initialisation, when Postgres only listens on a socket.
- **`app`** is checked by calling `GET /health` from inside the container. That
  endpoint runs `SELECT 1` against the database.
- **`app` waits for the database:** it uses `depends_on: db: condition: service_healthy`.
  If the DB still can't be reached within 30 s of startup, the app exits and
  `restart: unless-stopped` tries again.
- **If the DB goes down while the app is running:** `/health` returns `503` and API
  calls return `503 {"detail":"database unavailable"}`. Both recover by themselves
  a few seconds after the DB is back.

## Putting it behind a reverse proxy / TLS / auth

The app has **no authentication**, which is why it binds to `127.0.0.1` by default.
To reach it from other machines, put a reverse proxy in front that terminates TLS
and adds authentication (basic auth, or forward-auth such as Authelia,
oauth2-proxy or Authentik). Then leave `APP_BIND_ADDRESS=127.0.0.1` so the
proxy is the only way in. Add the proxy's domain to `.env`, for example
`ALLOWED_HOSTS=bookmarks.example.com`, so the app accepts the forwarded `Host` header.

Caddy example (gets TLS certificates automatically):

```caddyfile
bookmarks.example.com {
    basic_auth {
        # generate with: caddy hash-password
        alice $2a$14$...
    }
    reverse_proxy 127.0.0.1:8080
}
```

nginx example:

```nginx
server {
    listen 443 ssl;
    server_name bookmarks.example.com;
    ssl_certificate     /etc/letsencrypt/live/bookmarks.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bookmarks.example.com/privkey.pem;
    auth_basic           "Bookmarks";
    auth_basic_user_file /etc/nginx/bookmarks.htpasswd;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

If the proxy runs in Docker on the same host, you can instead attach it to this
project's `frontend` network, proxy to `app:8000`, and remove the `ports:`
mapping from `compose.yaml`. The app serves everything from `/`, so mount it on
its own (sub)domain rather than a sub-path.

## Common problems

| Symptom | Cause | Fix |
|---|---|---|
| `required variable POSTGRES_PASSWORD is missing a value` | No `.env` file | `cp .env.example .env` |
| `Bind for 127.0.0.1:8080 failed: port is already allocated` | Something else uses port 8080 | Set `APP_PORT=8081` (or any free port) in `.env`, then `docker compose up -d` |
| App keeps restarting, logs say `password authentication failed for user` | `POSTGRES_PASSWORD`/`POSTGRES_USER` were changed after the first start. Postgres only reads them when it creates the volume. | Put the old values back, **or** change the password inside the DB: `docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER USER \"$POSTGRES_USER\" PASSWORD '\''new-password'\''"'` and then update `.env`. On a fresh install with no data, `docker compose down -v` also works. |
| `app` stuck in `health: starting` or `unhealthy` | DB not reachable or still initialising | `docker compose logs db app`; `docker compose ps` shows whether `db` is healthy |
| `dependency failed to start: container bookmarks-db-1 is unhealthy` | Postgres failed to start, often from a corrupt or incompatible volume (e.g. switching major versions) | `docker compose logs db`. For a major-version change, follow the [upgrade steps](#upgrade). |
| `{"detail":"host not allowed; add it to ALLOWED_HOSTS"}` (HTTP 400) | You opened the app by a name or IP other than `localhost`/`127.0.0.1` | Add that name or IP to `ALLOWED_HOSTS` in `.env`, then `docker compose up -d` |
| UI loads but can't be reached from another machine | Bound to `127.0.0.1` by default | Use a reverse proxy (recommended), or set `APP_BIND_ADDRESS=0.0.0.0` on a trusted network |
| Data disappeared after restart | `docker compose down -v` was used, or the project folder (and so the project/volume name) was renamed | `docker volume ls`: the old volume is `<old-folder-name>_pgdata` unless removed. The project name is pinned to `bookmarks` in `compose.yaml` to avoid this. |
| `restore.sh`: `pg_restore: error: input file does not appear to be a valid archive` | The file isn't a `pg_dump -Fc` dump (e.g. an empty or partial file) | Use a `.dump` file produced by `backup.sh`; check its size is not 0 |

## Development

Run the tests against any PostgreSQL. The API tests empty the `bookmarks` table,
so use a throwaway database:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest==8.3.4 httpx==0.28.1
pytest -q                                            # config tests only
DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=bookmarks_test DB_USER=postgres DB_PASSWORD=... pytest -q   # + API tests
```

Run the app outside Docker with `DB_HOST=... DB_NAME=... DB_USER=... DB_PASSWORD=... python -m app`.
It listens on port 8000.

## Project layout

```
app/
  __main__.py     entrypoint: validate env, start uvicorn
  config.py       env-var loading + fail-fast validation
  db.py           connection pool, schema, SQL queries
  main.py         FastAPI routes, request validation, /health
  static/index.html   the web UI (plain HTML + JS)
scripts/backup.sh, scripts/restore.sh
tests/test_app.py
docs/images/         screenshots used in the README
docs/DEPLOYMENT.md    this file
Dockerfile, compose.yaml, .env.example
UNLICENSE            public-domain dedication
```

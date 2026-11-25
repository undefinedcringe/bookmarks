"""Configuration loaded only from environment variables.

Missing or invalid required settings stop the process at startup with a
message naming every problem, instead of failing later on first request.
"""

import os
from dataclasses import dataclass

REQUIRED = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
LOG_LEVELS = ("critical", "error", "warning", "info", "debug")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    db_pool_size: int
    log_level: str
    # Extra Host header values to accept besides localhost/127.0.0.1; ("*",) disables the check.
    allowed_hosts: tuple[str, ...] = ()


def _int(env: dict, name: str, default: int, minimum: int, errors: list) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be an integer, got {raw!r}")
        return default
    if value < minimum:
        errors.append(f"{name} must be >= {minimum}, got {value}")
    return value


def load_settings(env: dict | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    errors = [f"{name} is required but not set" for name in REQUIRED if not env.get(name, "").strip()]

    log_level = env.get("LOG_LEVEL", "info").strip().lower() or "info"
    if log_level not in LOG_LEVELS:
        errors.append(f"LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}, got {log_level!r}")

    db_port = _int(env, "DB_PORT", 5432, 1, errors)
    pool_size = _int(env, "DB_POOL_SIZE", 5, 1, errors)

    allowed_hosts = tuple(h.strip().lower() for h in env.get("ALLOWED_HOSTS", "").split(",") if h.strip())
    bad_hosts = [h for h in allowed_hosts if "/" in h or " " in h]
    if bad_hosts:
        errors.append(f"ALLOWED_HOSTS must be comma-separated host names without scheme or path, got {', '.join(bad_hosts)}")

    if errors:
        raise ConfigError(
            "Invalid configuration:\n  - " + "\n  - ".join(errors)
            + "\nSee .env.example and README.md for the full list of variables."
        )

    return Settings(
        db_host=env["DB_HOST"].strip(),
        db_port=db_port,
        db_name=env["DB_NAME"].strip(),
        db_user=env["DB_USER"].strip(),
        db_password=env["DB_PASSWORD"],
        db_pool_size=pool_size,
        log_level=log_level,
        allowed_hosts=allowed_hosts,
    )

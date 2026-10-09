"""Validated, redacted configuration editing with optimistic, atomic saves."""

import copy
import hashlib
import os
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal, get_args
from urllib.parse import urlsplit

import yaml
from curl_cffi.requests.impersonate import BrowserTypeLiteral
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import PROVIDER_OPTIONS, Settings, _ConfigLoader
from app.errors import ServiceError
from app.limits import MAX_MAILBOX_TTL_SECONDS, MAX_WORKER_CONCURRENCY, MIN_MAILBOX_TTL_SECONDS
from app.providers import TempMailLolProvider, TempMailOrgProvider

PROVIDER_CLASSES = {"temp-mail-org": TempMailOrgProvider, "tempmail-lol": TempMailLolProvider}
INDEX_URLS = {"temp-mail-org": "https://temp-mail.org/", "tempmail-lol": "https://tempmail.lol/"}
PositiveNumber = Annotated[float, Field(gt=0, allow_inf_nan=False)]
PositiveInteger = Annotated[int, Field(gt=0)]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AppUpdate(ConfigModel):
    db_path: Annotated[str, Field(min_length=1, max_length=4096)] | None = None
    api_token: Annotated[str, Field(max_length=4096)] | None = None


class WorkerUpdate(ConfigModel):
    sync_interval_seconds: PositiveInteger | None = None
    operation_timeout_seconds: PositiveInteger | None = None
    poll_seconds: PositiveNumber | None = None
    create_concurrency: Annotated[int, Field(ge=1, le=MAX_WORKER_CONCURRENCY)] | None = None
    receive_concurrency: Annotated[int, Field(ge=1, le=MAX_WORKER_CONCURRENCY)] | None = None


class ProviderUpdate(ConfigModel):
    id: Literal["temp-mail-org", "tempmail-lol"]
    enabled: bool
    index_url: Annotated[str, Field(max_length=4096)] | None
    base_url: Annotated[str, Field(min_length=1, max_length=4096)]
    timeout_seconds: PositiveNumber
    impersonate: Annotated[str, Field(min_length=1, max_length=100)]
    max_ttl_seconds: Annotated[int, Field(ge=MIN_MAILBOX_TTL_SECONDS, le=MAX_MAILBOX_TTL_SECONDS)] | None = None
    proxy: Annotated[str, Field(max_length=8192)] | None = None


class ConfigUpdate(ConfigModel):
    revision: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    app: AppUpdate | None = None
    worker: WorkerUpdate | None = None
    providers: list[ProviderUpdate] | None = None


def _revision(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _http_url(value: str, *, api: bool = False):
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and (parsed.port is None or parsed.port > 0)
            and not any(char.isspace() for char in value)
            and not (api and (parsed.query or parsed.fragment))
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid provider URL")


def _effective(settings: Settings) -> dict:
    providers = []
    for name in [*settings.providers, *(name for name in PROVIDER_OPTIONS if name not in settings.providers)]:
        config = settings.providers.get(name)
        provider = PROVIDER_CLASSES[name](**(config.options if config else {}))
        index_url = config.index_url if config and config.index_url else INDEX_URLS[name]
        _http_url(index_url)
        _http_url(provider.base_url, api=True)
        if provider.impersonate not in get_args(BrowserTypeLiteral):
            raise ValueError("Unsupported browser impersonation")
        providers.append(
            {
                "id": name,
                "enabled": config.enabled if config else False,
                "index_url": index_url,
                "base_url": provider.base_url,
                "timeout_seconds": provider.timeout_seconds,
                "impersonate": provider.impersonate,
                "proxy": provider.proxy,
                "max_ttl_seconds": provider.capabilities.max_ttl_seconds,
                "capabilities": asdict(provider.capabilities),
            }
        )
    return {
        "app": {"db_path": settings.db_path, "api_token": settings.api_token, "encryption_key": settings.encryption_key},
        "worker": {
            "sync_interval_seconds": settings.sync_interval_seconds,
            "operation_timeout_seconds": settings.operation_timeout_seconds,
            "poll_seconds": settings.worker_poll_seconds,
            "create_concurrency": settings.create_concurrency,
            "receive_concurrency": settings.receive_concurrency,
        },
        "providers": providers,
    }


@contextmanager
def _file_lock(path: Path):
    # Keep a separate inode: replacing config.yaml must not invalidate the writer lock.
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)

            def acquire():
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)

            def release():
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(descriptor, fcntl.LOCK_UN)

        deadline = time.monotonic() + 5
        while True:
            try:
                acquire()
                acquired = True
                break
            except (BlockingIOError, PermissionError):
                if time.monotonic() >= deadline:
                    raise OSError("Configuration writer is busy") from None
                time.sleep(0.05)
        yield descriptor
    finally:
        if acquired:
            release()
        os.close(descriptor)


def _preserve_owner(descriptor: int, original):
    if hasattr(os, "fchown"):
        current = os.fstat(descriptor)
        if (current.st_uid, current.st_gid) != (original.st_uid, original.st_gid):
            os.fchown(descriptor, original.st_uid, original.st_gid)


class ConfigStore:
    def __init__(self, settings: Settings, prepare: Callable[[Settings, str], Callable[[], None]] | None = None):
        self.path = settings.config_path
        self._lock = threading.Lock()
        self._startup = settings
        self._prepare = prepare

    def _require_path(self) -> Path:
        if self.path is None:
            raise ServiceError("CONFIG_UNAVAILABLE", "No configuration file is associated with this service", 503)
        return self.path

    def _read(self, content: bytes | None = None) -> tuple[bytes, dict, Settings]:
        path = self._require_path()
        try:
            if content is None:
                content = path.read_bytes()
            document = yaml.load(content.decode("utf-8"), Loader=_ConfigLoader)
            settings = Settings.from_mapping(document, path)
            _effective(settings)
            return content, document, settings
        except (OSError, ValueError, TypeError, UnicodeError, yaml.YAMLError):
            raise ServiceError("CONFIG_READ_FAILED", "The configuration file cannot be read or validated", 503) from None

    def _view(self, content: bytes, document: dict, settings: Settings) -> dict:
        effective = _effective(settings)
        restart_required_fields = [
            f"app.{field}" for field in ("db_path", "encryption_key") if getattr(settings, field) != getattr(self._startup, field)
        ]
        effective["app"] = {
            "db_path": document["app"].get("db_path", "./data/temp-mail.db"),
            "api_token_configured": bool(settings.api_token),
            "encryption_key_configured": bool(settings.encryption_key),
        }
        for provider in effective["providers"]:
            provider["proxy_configured"] = bool(provider.pop("proxy"))
        return {
            "revision": _revision(content),
            "restart_required": bool(restart_required_fields),
            "restart_required_fields": restart_required_fields,
            **effective,
        }

    def get(self) -> dict:
        return self._view(*self._read())

    def save(self, body: dict) -> dict:
        path = self._require_path()
        try:
            patch = ConfigUpdate.model_validate(body)
            # Optional fields are omittable, but null is only meaningful for a provider proxy/index URL.
            if any(value is None for value in patch.model_dump(exclude_unset=True).values()):
                raise ValueError
            for section in (patch.app, patch.worker):
                if section and any(value is None for value in section.model_dump(exclude_unset=True).values()):
                    raise ValueError
            if patch.providers is not None and len({item.id for item in patch.providers}) != len(patch.providers):
                raise ValueError
        except (ValidationError, ValueError):
            raise ServiceError("CONFIG_INVALID", "Invalid configuration changes", 422) from None
        try:
            with self._lock, _file_lock(path.with_name(f".{path.name}.lock")) as lock_descriptor:
                content, document, _ = self._read()
                _preserve_owner(lock_descriptor, path.stat())
                if _revision(content) != patch.revision:
                    raise ServiceError("CONFIG_CONFLICT", "The configuration changed; reload it before saving", 409)
                updated = copy.deepcopy(document)
                if patch.app is not None:
                    app = patch.app.model_dump(exclude_unset=True)
                    if not app.get("api_token", "").strip():
                        app.pop("api_token", None)
                    updated["app"].update(app)
                if patch.worker is not None:
                    updated.setdefault("worker", {}).update(patch.worker.model_dump(exclude_unset=True))
                if patch.providers is not None:
                    providers = {}
                    for item in patch.providers:
                        values = item.model_dump(exclude_unset=True, exclude={"id", "proxy"})
                        previous = document["providers"].get(item.id, {})
                        if "max_ttl_seconds" not in item.model_fields_set and "max_ttl_seconds" in previous:
                            values["max_ttl_seconds"] = previous["max_ttl_seconds"]
                        if "proxy" in item.model_fields_set and item.proxy is None:
                            values["proxy"] = None
                        elif item.proxy and item.proxy.strip():
                            values["proxy"] = item.proxy.strip()
                        elif "proxy" in previous:
                            values["proxy"] = previous["proxy"]
                        providers[item.id] = values
                    updated["providers"] = providers
                try:
                    settings = Settings.from_mapping(updated, path)
                    _effective(settings)
                    output = yaml.safe_dump(updated, allow_unicode=True, sort_keys=False).encode("utf-8")
                    commit = self._prepare(settings, _revision(output)) if self._prepare is not None else None
                except (ValueError, TypeError, yaml.YAMLError):
                    raise ServiceError("CONFIG_INVALID", "Invalid configuration changes", 422) from None
                self._replace(path, output, patch.revision)
                if commit is not None:
                    commit()
                return self._view(output, updated, settings)
        except OSError:
            raise ServiceError("CONFIG_WRITE_FAILED", "The configuration file could not be saved", 503) from None

    @staticmethod
    def _replace(path: Path, content: bytes, expected_revision: str):
        temporary = None
        try:
            original = path.stat()
            if original.st_mode & 0o222 == 0:
                raise PermissionError("Configuration is read-only")
            descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                _preserve_owner(stream.fileno(), original)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            # Also detect edits by external tools between reading and writing.
            if _revision(path.read_bytes()) != expected_revision:
                raise ServiceError("CONFIG_CONFLICT", "The configuration changed; reload it before saving", 409)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

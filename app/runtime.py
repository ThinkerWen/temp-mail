"""Swap service snapshots at request/tick boundaries while retaining durable storage."""

import threading
from collections.abc import Callable
from dataclasses import replace

from loguru import logger

from app.config import Settings
from app.config_store import ConfigStore, _revision
from app.errors import ServiceError
from app.providers import Registry
from app.service import Service


class Runtime:
    def __init__(self, settings: Settings, registry: Registry | None = None):
        self._startup = settings
        self._registry_override = registry
        self._service = Service(settings, registry)
        self._db = self._service.db
        self._cipher = self._service.cipher
        self._revision: str | None = None
        self._lock = threading.RLock()
        self._read_failed = False
        self._failed_revision: str | None = None
        self.config_store = ConfigStore(settings, self.prepare)

    @property
    def service(self) -> Service:
        return self._service

    def prepare(self, settings: Settings, revision: str) -> Callable[[], None]:
        # A new snapshot cannot switch storage or encrypt against a different key mid-process.
        effective = replace(settings, db_path=self._startup.db_path, encryption_key=self._startup.encryption_key)
        candidate = Service(effective, self._registry_override, db=self._db, cipher=self._cipher)
        prepared_revision = self._revision

        def commit():
            with self._lock:
                # A concurrent refresh may already have adopted a newer external edit.
                if self._revision not in {prepared_revision, revision} and self.config_store.path is not None:
                    try:
                        if _revision(self.config_store.path.read_bytes()) != revision:
                            return
                    except OSError:
                        return
                if self._revision != revision:
                    self._service = candidate
                    self._revision = revision
                self._read_failed = False
                self._failed_revision = None

        return commit

    def _apply(self, content: bytes, settings: Settings):
        revision = _revision(content)
        if revision != self._revision:
            self.prepare(settings, revision)()
        self._read_failed = False

    def refresh(self) -> Service:
        if self.config_store.path is None:
            return self._service
        with self._lock:
            revision = None
            try:
                content = self.config_store.path.read_bytes()
                revision = _revision(content)
                if revision == self._revision:
                    self._read_failed = False
                    self._failed_revision = None
                    return self._service
                if revision == self._failed_revision:
                    return self._service
                _, _, settings = self.config_store._read(content)
                self._apply(content, settings)
            except (ServiceError, ValueError, TypeError, OSError):
                self._failed_revision = revision
                if not self._read_failed:
                    logger.warning("Configuration reload failed; retaining the last valid runtime settings")
                    self._read_failed = True
            return self._service

    def configuration(self) -> dict:
        # Read and apply the same revision so a GET cannot imply an unapplied hot configuration.
        with self._lock:
            content, document, settings = self.config_store._read()
            self._apply(content, settings)
            return self.config_store._view(content, document, settings)

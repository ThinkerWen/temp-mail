from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

import yaml
from cryptography.fernet import Fernet

PROVIDER_OPTIONS = {
    "temp-mail-org": {"base_url", "timeout_seconds", "impersonate", "proxy"},
    "tempmail-lol": {"base_url", "timeout_seconds", "impersonate", "proxy"},
}


class _ConfigLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("Configuration keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _mapping(value, section: str, allowed: set[str] | None = None) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{section} must be a mapping with string keys")
    if allowed is not None and value.keys() - allowed:
        raise ValueError(f"Unknown configuration field in {section}")
    return value


def _positive_number(value, name: str, *, integer: bool = False):
    valid_type = type(value) is int if integer else type(value) in {int, float}
    if not valid_type or not isfinite(value) or value <= 0:
        kind = "integer" if integer else "number"
        raise ValueError(f"{name} must be a finite positive {kind}")


@dataclass(frozen=True)
class ProviderSettings:
    enabled: bool = True
    options: dict = field(default_factory=dict, repr=False)
    index_url: str | None = None

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Provider enabled must be a boolean")
        if self.index_url is not None and not isinstance(self.index_url, str):
            raise ValueError("Provider index_url must be a string or null")
        options = dict(_mapping(self.options, "provider options"))
        if "proxy" in options:
            if options["proxy"] is not None and not isinstance(options["proxy"], str):
                raise ValueError("Provider proxy must be a URL string or null")
            options["proxy"] = options["proxy"].strip() or None if options["proxy"] is not None else None
        object.__setattr__(self, "options", options)


@dataclass(frozen=True)
class Settings:
    db_path: str
    api_token: str = field(repr=False)
    encryption_key: str = field(repr=False)
    sync_interval_seconds: int = 15
    operation_timeout_seconds: int = 300
    worker_poll_seconds: float = 1
    providers: dict[str, ProviderSettings] = field(default_factory=lambda: {name: ProviderSettings() for name in PROVIDER_OPTIONS})

    def __post_init__(self):
        if not isinstance(self.api_token, str) or len(self.api_token.strip()) < 24:
            raise ValueError("app.api_token must contain at least 24 characters")
        try:
            if not isinstance(self.encryption_key, str):
                raise ValueError
            Fernet(self.encryption_key.encode())
        except (ValueError, TypeError):
            raise ValueError("app.encryption_key must be a valid Fernet key") from None
        if not isinstance(self.db_path, str) or not self.db_path.strip() or self.db_path == ":memory:":
            raise ValueError("app.db_path must name a file-backed SQLite database")
        _positive_number(self.sync_interval_seconds, "worker.sync_interval_seconds", integer=True)
        _positive_number(self.operation_timeout_seconds, "worker.operation_timeout_seconds", integer=True)
        _positive_number(self.worker_poll_seconds, "worker.poll_seconds")
        _mapping(self.providers, "providers", set(PROVIDER_OPTIONS))
        for name, provider in self.providers.items():
            if not isinstance(provider, ProviderSettings):
                raise ValueError("Provider configuration must use ProviderSettings")
            options = _mapping(provider.options, f"providers.{name}", PROVIDER_OPTIONS[name])
            for key in ("base_url", "impersonate"):
                if key in options and (not isinstance(options[key], str) or not options[key].strip()):
                    raise ValueError(f"providers.{name}.{key} must be a nonempty string")
            if "timeout_seconds" in options:
                _positive_number(options["timeout_seconds"], f"providers.{name}.timeout_seconds")

    @classmethod
    def from_yaml(cls, path: str | Path = "config.yaml"):
        path = Path(path).resolve()
        try:
            document = yaml.load(path.read_text(encoding="utf-8"), Loader=_ConfigLoader)
        except (yaml.YAMLError, ValueError, UnicodeError):
            # Parser exceptions can contain the offending line, including proxy credentials.
            raise ValueError("Invalid config.yaml: use valid YAML with unique string keys") from None
        document = _mapping(document, "config.yaml", {"app", "worker", "providers"})
        if "app" not in document or "providers" not in document:
            raise ValueError("config.yaml requires app and providers sections")
        app = _mapping(document["app"], "app", {"db_path", "api_token", "encryption_key"})
        worker = _mapping(document.get("worker", {}), "worker", {"sync_interval_seconds", "operation_timeout_seconds", "poll_seconds"})
        providers = {}
        for name, config in _mapping(document["providers"], "providers", set(PROVIDER_OPTIONS)).items():
            config = _mapping(config, f"providers.{name}", {"enabled", "index_url", *PROVIDER_OPTIONS[name]})
            providers[name] = ProviderSettings(
                enabled=config.get("enabled", True),
                options={key: value for key, value in config.items() if key not in {"enabled", "index_url"}},
                index_url=config.get("index_url"),
            )
        db_path = app.get("db_path", "./data/temp-mail.db")
        if not isinstance(db_path, str) or not db_path.strip() or db_path == ":memory:":
            raise ValueError("app.db_path must name a file-backed SQLite database")
        return cls(
            db_path=str((path.parent / Path(db_path).expanduser()).resolve()),
            api_token=app.get("api_token", ""),
            encryption_key=app.get("encryption_key", ""),
            sync_interval_seconds=worker.get("sync_interval_seconds", 15),
            operation_timeout_seconds=worker.get("operation_timeout_seconds", 300),
            worker_poll_seconds=worker.get("poll_seconds", 1),
            providers=providers,
        )

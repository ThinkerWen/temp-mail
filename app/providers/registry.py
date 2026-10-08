"""Choose a provider only when creating a mailbox; existing bindings use get()."""

from app.providers.base import Provider, ProviderError

CAPABILITY_NAMES = {"receive", "send", "delete", "attachments", "webhook", "custom_local_part"}


class Registry:
    def __init__(self, providers: list[Provider]):
        self._providers = {provider.id: provider for provider in providers}
        if len(self._providers) != len(providers):
            raise ValueError("Provider IDs must be unique")
        if "auto" in self._providers:
            raise ValueError("Provider ID 'auto' is reserved")

    def get(self, provider_id: str) -> Provider:
        try:
            return self._providers[provider_id]
        except KeyError:
            raise ProviderError("PROVIDER_UNAVAILABLE", "The bound provider is not configured") from None

    def all(self) -> list[Provider]:
        return list(self._providers.values())

    def select(self, required_capabilities: list[str], ttl_seconds: int, provider_id: str = "auto") -> Provider:
        if set(required_capabilities) - CAPABILITY_NAMES:
            raise ProviderError("CAPABILITY_UNSUPPORTED", "Unknown required capability")
        candidates = self.all() if provider_id == "auto" else [self.get(provider_id)]
        if not candidates:
            raise ProviderError("PROVIDER_UNAVAILABLE", "No providers are configured")
        candidates = [provider for provider in candidates if all(getattr(provider.capabilities, name) for name in required_capabilities)]
        if not candidates:
            raise ProviderError("CAPABILITY_UNSUPPORTED", "No provider supports all required capabilities")
        for provider in candidates:
            if 0 < ttl_seconds <= provider.capabilities.max_ttl_seconds:
                return provider
        raise ProviderError("TTL_UNSUPPORTED", "No matching provider supports the requested mailbox lifetime")

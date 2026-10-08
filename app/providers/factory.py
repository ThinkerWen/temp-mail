from app.config import Settings
from app.providers.registry import Registry
from app.providers.temp_mail_org import TempMailOrgProvider
from app.providers.tempmail_lol import TempMailLolProvider


def build_registry(settings: Settings) -> Registry:
    providers = []
    for provider_id, config in settings.providers.items():
        if not config.enabled:
            continue
        if provider_id == "temp-mail-org":
            providers.append(TempMailOrgProvider(**config.options))
        elif provider_id == "tempmail-lol":
            providers.append(TempMailLolProvider(**config.options))
    return Registry(providers)

MIN_MAILBOX_TTL_SECONDS = 60
MAX_MAILBOX_TTL_SECONDS = 31_536_000
MAX_WORKER_CONCURRENCY = 32


def validate_max_ttl_seconds(value: int):
    if type(value) is not int or not MIN_MAILBOX_TTL_SECONDS <= value <= MAX_MAILBOX_TTL_SECONDS:
        raise ValueError("Provider max_ttl_seconds must be an integer between 60 and 31536000")

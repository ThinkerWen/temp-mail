"""One Loguru sink for application, standard-library, and Uvicorn logs."""

import logging
import sys

from loguru import logger

LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | PID {process.id} | {name}:{function}:{line} - {message}"
UVICORN_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"loguru": {"class": "app.logging.InterceptHandler"}},
    "root": {"handlers": ["loguru"], "level": "INFO"},
    "loggers": {name: {"handlers": [], "level": "NOTSET", "propagate": True} for name in ("uvicorn", "uvicorn.error", "uvicorn.access")},
}


def _write_stderr(message):
    # Resolve stderr at write time so capture and redirection keep working.
    sys.stderr.write(message)
    sys.stderr.flush()


class InterceptHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        logger.remove()
        logger.add(_write_stderr, level="INFO", format=LOG_FORMAT, backtrace=False, diagnose=False, colorize=False)
        logging.captureWarnings(True)

    def emit(self, record):
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 0
        while frame and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1
        source = logger.patch(lambda entry: entry.update(name=record.name))
        source.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def configure_logging():
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.INFO, force=True)
    for name in logging.root.manager.loggerDict.copy():
        target = logging.getLogger(name)
        target.handlers.clear()
        target.setLevel(logging.NOTSET)
        target.propagate = True
        target.disabled = False

import argparse
import logging
import sqlite3
import time

from app.config import Settings
from app.service import Service

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Process mailbox operations, synchronize messages, and expire local mailboxes")
    parser.add_argument("--once", action="store_true", help="Run one worker tick and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = Service(Settings.from_yaml())
    try:
        while True:
            try:
                result = service.run_once()
                if any(result.values()):
                    logger.info("Worker tick: %s", result)
            except sqlite3.OperationalError:
                if args.once:
                    raise
                logger.error("Worker storage is temporarily unavailable; retrying on the next tick")
            if args.once:
                return
            time.sleep(service.settings.worker_poll_seconds)
    except KeyboardInterrupt:
        logger.info("Worker stopped")


if __name__ == "__main__":
    main()

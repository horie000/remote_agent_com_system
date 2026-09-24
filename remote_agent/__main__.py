from __future__ import annotations

import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from .bridge import BridgeBot
from .config import Settings


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
    except (ValueError, OSError) as exc:
        logging.error("configuration error: %s", exc)
        return 2
    bot = BridgeBot(settings)
    try:
        bot.run(settings.discord_token, log_handler=None)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logging.error("bridge stopped (%s)", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

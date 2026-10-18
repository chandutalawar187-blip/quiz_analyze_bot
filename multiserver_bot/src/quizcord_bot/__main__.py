"""Application entry point."""

import logging

from quizcord_bot.bot import AnalyticsBot
from quizcord_bot.config import Settings


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings.from_environment()
    except ValueError as error:
        raise SystemExit(str(error)) from error

    bot = AnalyticsBot(settings)
    bot.run(settings.token, log_handler=None)


if __name__ == "__main__":
    main()

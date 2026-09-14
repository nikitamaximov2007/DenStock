"""Run the customer-request Telegram bot (long polling, exactly one instance)."""
import logging
import signal
import threading

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.core.observability import RedactingFormatter
from apps.customer_requests.telegram_api import TelegramBotApi
from apps.customer_requests.telegram_bot import SingleInstanceError, TelegramBotWorker


def _configure_logging() -> None:
    logger = logging.getLogger("apps.customer_requests.telegram_bot")
    if any(getattr(handler, "_denstock_bot", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler._denstock_bot = True
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class Command(BaseCommand):
    help = "Запустить Telegram-бота заявок клиентов (long polling, один экземпляр)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once", action="store_true", help="Один цикл опроса и отправки, затем выход."
        )

    def handle(self, *args, **options):
        _configure_logging()
        logger = logging.getLogger("apps.customer_requests.telegram_bot")
        if not settings.TELEGRAM_BOT_TOKEN:
            raise CommandError("TELEGRAM_BOT_TOKEN не задан: Telegram-бот не настроен.")
        executor = MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            raise CommandError("Есть непримененные миграции: сначала обновите web.")
        api = TelegramBotApi(
            settings.TELEGRAM_BOT_TOKEN,
            base_url=settings.TELEGRAM_API_BASE_URL,
        )
        stop = threading.Event()

        def request_stop(signum, frame):
            logger.info("stop signal %s received, finishing the current cycle", signum)
            stop.set()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)
        worker = TelegramBotWorker(api, stop=stop)
        logger.info("telegram bot starting, worker %s", worker.worker_id[:8])
        try:
            worker.run(once=options["once"])
        except SingleInstanceError as exc:
            raise CommandError(str(exc)) from None
        logger.info("telegram bot stopped")

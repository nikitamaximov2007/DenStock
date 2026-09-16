"""Run the customer-request MAX bot worker (sends the outbox, exactly one instance).

Updates arrive at the webhook in the web process; this worker only sends what
was stored and fans operator events out. It needs MAX_BOT_TOKEN and nothing
else secret.
"""
import logging
import signal
import threading

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from apps.core.observability import RedactingFormatter
from apps.customer_requests.max_bot import MaxBotWorker, SingleInstanceError, build_api

REFUSAL_PAUSE_SECONDS = 60
LOGGER = "apps.customer_requests.max_bot"


def _configure_logging() -> None:
    logger = logging.getLogger(LOGGER)
    if any(getattr(handler, "_denstock_bot", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler._denstock_bot = True
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class Command(BaseCommand):
    help = "Запустить MAX-бота заявок клиентов (отправка очереди, один экземпляр)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Один цикл отправки, затем выход.")

    def handle(self, *args, **options):
        _configure_logging()
        logger = logging.getLogger(LOGGER)
        if not settings.MAX_BOT_TOKEN:
            raise CommandError("MAX_BOT_TOKEN не задан: MAX-бот не настроен.")
        executor = MigrationExecutor(connection)
        if executor.migration_plan(executor.loader.graph.leaf_nodes()):
            raise CommandError("Есть непримененные миграции: сначала обновите web.")
        stop = threading.Event()

        def request_stop(signum, frame):
            logger.info("stop signal %s received, finishing the current cycle", signum)
            stop.set()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)

        def refuse(message: str):
            logger.error("max bot refused to run: %s", message)
            if not options["once"]:
                stop.wait(REFUSAL_PAUSE_SECONDS)
            return CommandError(message)

        try:
            api = build_api()
        except ValueError as exc:
            raise refuse(str(exc)) from None
        worker = MaxBotWorker(api, stop=stop)
        logger.info("max bot starting, worker %s", worker.worker_id[:8])
        try:
            worker.run(once=options["once"])
        except SingleInstanceError as exc:
            raise refuse(str(exc)) from None
        logger.info("max bot stopped")

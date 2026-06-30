from django.core.management.base import BaseCommand, CommandError

from apps.operations import checks


class Command(BaseCommand):
    help = "Проверка готовности к эксплуатации: БД, media/backup writability, pg_dump и пр."

    def handle(self, *args, **options):
        results = checks.run_checks()
        styles = {
            checks.OK: self.style.SUCCESS,
            checks.WARN: self.style.WARNING,
            checks.FAIL: self.style.ERROR,
        }
        for r in results:
            self.stdout.write(styles[r.level](f"[{r.level.upper():4}] {r.name}: {r.message}"))
        if checks.has_failures(results):
            raise CommandError("Проверка готовности НЕ пройдена (см. строки [FAIL] выше).")
        self.stdout.write(self.style.SUCCESS("Готовность к эксплуатации: ОК."))

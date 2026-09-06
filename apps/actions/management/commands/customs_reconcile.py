"""Read-only сверка таможенной выгрузки с отчётом «Продажи и ремонты».

    python manage.py customs_reconcile
    python manage.py customs_reconcile --lines        # построчная расшифровка
    python manage.py customs_reconcile --json         # машиночитаемый вывод

Команда НИЧЕГО не пишет: ни документов, ни движений, ни таможенных карточек.
Её можно безопасно запускать на production, чтобы получить приёмочные числа
перед выкладкой.

Проверяется ровно то, что должно совпасть:

* действующее количество канонических строк == количество «Продаж и ремонтов»
  за всё время;
* клиентская сумма этих строк == сумма того же отчёта;
* количество ДО свёртки в строки Excel == количество ПОСЛЕ свёртки;
* ни одна каноническая строка не потерялась молча.

Ненулевая дельта - причина не выкладывать, а искать различие построчно.
"""
import json

from django.core.management.base import BaseCommand

from apps.actions.services import customs_export_reconciliation


class Command(BaseCommand):
    help = "Read-only сверка таможенной выгрузки с «Продажами и ремонтами» за всё время."

    def add_arguments(self, parser):
        parser.add_argument(
            "--lines", action="store_true",
            help="Показать каждую каноническую строку расхода.",
        )
        parser.add_argument(
            "--json", action="store_true", dest="as_json",
            help="Вывести итоги как JSON.",
        )

    def handle(self, *args, **options):
        result = customs_export_reconciliation()
        totals, report, delta = result["totals"], result["report"], result["delta"]
        payload = {
            "canonical_line_count": totals["line_count"],
            "effective_line_count": totals["effective_line_count"],
            "export_row_count": totals["row_count"],
            "export_quantity": str(totals["quantity"]),
            "export_row_quantity": str(totals["row_quantity"]),
            "export_amount": str(totals["amount"]),
            "report_quantity": str(report["quantity"]),
            "report_amount": str(report["amount"]),
            "report_customers": report["customers"],
            "report_customers_with_unknown_price": report["customers_with_unknown_price"],
            "delta_quantity": str(delta["quantity"]),
            "delta_amount": str(delta["amount"]),
            "incomplete_customs_rows": len(result["incomplete_rows"]),
            "incomplete_lines": len(result["incomplete"]),
            "article_unproven_lines": len(result["article_unproven"]),
            "price_unknown_lines": len(result["price_unknown"]),
            "fully_returned_lines": len(result["fully_returned"]),
            "silent": len(result["silent"]),
            "duplicates": len(result["duplicates"]),
        }
        reconciled = (
            delta["quantity"] == 0 and delta["amount"] == 0
            and not result["silent"] and not result["duplicates"]
            and totals["quantity"] == totals["row_quantity"]
        )
        payload["reconciled"] = reconciled

        if options["as_json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for key, value in payload.items():
                self.stdout.write(f"{key}: {value}")

        if options["lines"]:
            self.stdout.write("")
            self.stdout.write("kind\tdocument\tline\tarticle\tissued\treturned\teffective\tamount")
            for line in result["lines"]:
                self.stdout.write(
                    "\t".join(str(value) for value in (
                        line["kind"], line["document_number"], line["line_id"],
                        line["number"] or "-", line["issued_quantity"],
                        line["returned_quantity"], line["quantity"],
                        "-" if line["amount"] is None else line["amount"],
                    ))
                )

        style = self.style.SUCCESS if reconciled else self.style.ERROR
        self.stdout.write(style("RECONCILED" if reconciled else "NOT RECONCILED"))

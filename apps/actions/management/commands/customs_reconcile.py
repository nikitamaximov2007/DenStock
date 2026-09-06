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
* ни одна каноническая строка не потерялась молча;
* множество строк выгрузки СОВПАДАЕТ со множеством строк отчёта поимённо.

Последнюю проверку команда делает независимым запросом к тем же документам,
которыми считается отчёт, а не доверяет тому, что источник «тот же по
построению». Совпадение итогов при разном составе строк - тоже расхождение,
просто взаимно погасившееся.

Ненулевая дельта - причина не выкладывать, а искать различие построчно.
"""
import json
from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.actions.services import customs_export_reconciliation
from apps.procurement.models import money
from apps.repairs.models import RepairIssueLine, RepairOrder
from apps.sales.models import Sale, SaleLine


def _report_line_keys() -> set[tuple[str, int]]:
    """Строки, из которых «Продажи и ремонты» складывают итог за всё время.

    Запрос повторяет фильтр отчёта дословно и намеренно не переиспользует
    таможенный код: иначе сверка сравнивала бы одну реализацию сама с собой.
    """
    keys = {
        ("sale", pk)
        for pk in SaleLine.objects.filter(
            sale__status=Sale.Status.COMPLETED
        ).values_list("pk", flat=True)
    }
    keys |= {
        ("repair", pk)
        for pk in RepairIssueLine.objects.filter(
            repair_order__status=RepairOrder.Status.COMPLETED
        ).values_list("pk", flat=True)
    }
    return keys


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
        lines = result["lines"]
        sales = [line for line in lines if line["kind"] == "sale"]
        repairs = [line for line in lines if line["kind"] == "repair"]
        customs_keys = {(line["kind"], line["line_id"]) for line in lines}
        report_keys = _report_line_keys()
        report_only = sorted(report_keys - customs_keys)
        customs_only = sorted(customs_keys - report_keys)
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
            "report_only_lines": len(report_only),
            "customs_only_lines": len(customs_only),
            # Разрез по видам операций: те же строки, только названные отдельно.
            "sales_documents": len({line["document_id"] for line in sales}),
            "sales_lines": len(sales),
            "sales_quantity": str(sum((line["quantity"] for line in sales), Decimal("0"))),
            "sales_amount": str(money(sum(
                (line["amount"] for line in sales if line["amount_known"]), Decimal("0")
            ))),
            "repair_documents": len({line["document_id"] for line in repairs}),
            "repair_lines": len(repairs),
            "repair_quantity": str(sum((line["quantity"] for line in repairs), Decimal("0"))),
            "repair_amount": str(money(sum(
                (line["amount"] for line in repairs if line["amount_known"]), Decimal("0")
            ))),
            "blank_article_rows": sum(1 for row in result["rows"] if not row["number"]),
        }
        reconciled = (
            delta["quantity"] == 0 and delta["amount"] == 0
            and not result["silent"] and not result["duplicates"]
            and totals["quantity"] == totals["row_quantity"]
            and not report_only and not customs_only
        )
        payload["reconciled"] = reconciled

        if options["as_json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for key, value in payload.items():
                self.stdout.write(f"{key}: {value}")

        for label, missing in (("report only", report_only), ("customs only", customs_only)):
            if missing:
                self.stdout.write("")
                self.stdout.write(f"{label} ({len(missing)}):")
                for kind, line_id in missing[:200]:
                    self.stdout.write(f"  {kind} line {line_id}")
                if len(missing) > 200:
                    self.stdout.write(f"  ... и ещё {len(missing) - 200}")

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

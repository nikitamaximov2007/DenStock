"""Доказуемое происхождение таможенного расхода.

Таможенная выгрузка обязана называть деталь тем номером, под которым она
фактически ушла со склада, и гасить возврат ровно тем выбытием, которое он
отменяет. Обе величины должны быть доказаны, а не восстановлены правдоподобной
догадкой: экспорт уходит в таможню, и «скорее всего это тот же номер» там не
аргумент.

Что здесь запрещено и почему:

* брать текущий артикул детали. Деталь могла быть переименована, заменена по
  BRP-supersession или получить псевдоним уже после продажи. Сегодняшний номер
  не является историей и выдавать его за неё нельзя;
* гасить возврат по LIFO. Одна и та же деталь из одного лота могла уйти и в
  продажу, и в ремонт, с разными таможенными профилями. Догадка «вернулось из
  последнего» молча припишет расход не тому документу.

Если доказать нечего, строка получает явную пометку и экспорт блокируется.
Пятой, молчаливой категории не существует.
"""
from apps.actions.models import WarehouseAction
from apps.inventory.models import StockMovement
from apps.returns.models import StockReturn, StockReturnLine

# Состояния артикула выбытия.
ARTICLE_PROVEN = "proven"
ARTICLE_MISSING = "legacy_no_article_provenance"
ARTICLE_AMBIGUOUS = "legacy_article_ambiguous"

# Состояния привязки возврата.
RETURN_EXACT = "exact"
RETURN_AMBIGUOUS = "legacy_return_attribution_ambiguous"

_DOCUMENT_FIELDS = {"sale": "sale_id", "repair_order": "repair_order_id"}

OUTBOUND_TYPES = (
    StockMovement.MovementType.SALE_ITEM, StockMovement.MovementType.SALE_LOT,
    StockMovement.MovementType.ISSUE_ITEM, StockMovement.MovementType.ISSUE_LOT,
    StockMovement.MovementType.WRITE_OFF_ITEM, StockMovement.MovementType.WRITE_OFF_LOT,
)
RETURN_TYPES = (
    StockMovement.MovementType.RETURN_ITEM, StockMovement.MovementType.RETURN_LOT,
)


def _document_key(movement):
    """Документ-источник движения: тип и номер, как их хранит журнал."""
    return (movement.document_type or "", movement.document_id)


def article_snapshots(movements) -> dict[int, dict]:
    """Артикул на момент выбытия для каждого движения.

    Источник один: снимок журнала действий по ключу «документ + деталь».
    Действие фиксирует номер, который оператор отсканировал в момент операции,
    и жёстко привязано к своему документу, поэтому соответствие точное, а не
    хронологическая догадка по количествам.

    Своего снимка у движения нет намеренно. Единственное, что склад знает о
    детали в этой точке, - её карточка, а сегодняшний канонический номер
    историей не является: у детали бывает несколько точных номеров, и продажа
    по WH-200 не имеет права стать строкой по WH-100 только потому, что второй
    номер в карточке первый.

    Если снимка нет - артикул НЕ ДОКАЗАН, и выгрузка блокируется.
    """
    movements = list(movements)
    outbound = [
        movement
        for movement in movements
        if movement.movement_type in OUTBOUND_TYPES
    ]
    if not outbound:
        return {}

    wanted = {
        _document_key(movement)
        for movement in outbound
        if movement.document_type in _DOCUMENT_FIELDS and movement.document_id
    }
    by_key: dict[tuple, set] = {}
    for document_type, field in _DOCUMENT_FIELDS.items():
        ids = [key[1] for key in wanted if key[0] == document_type]
        if not ids:
            continue
        rows = WarehouseAction.objects.filter(**{f"{field}__in": ids}).exclude(
            part_number=""
        ).values(field, "part_type_id", "part_number")
        for row in rows:
            key = (document_type, row[field], row["part_type_id"])
            by_key.setdefault(key, set()).add(row["part_number"])

    resolved = {}
    for movement in outbound:
        document_type, document_id = _document_key(movement)
        numbers = by_key.get((document_type, document_id, movement.part_type_id), set())
        if len(numbers) == 1:
            resolved[movement.pk] = {
                "status": ARTICLE_PROVEN, "number": next(iter(numbers)), "source": "action"
            }
        elif len(numbers) > 1:
            resolved[movement.pk] = {
                "status": ARTICLE_AMBIGUOUS, "number": "", "source": "action"
            }
        else:
            resolved[movement.pk] = {"status": ARTICLE_MISSING, "number": "", "source": ""}
    return resolved


def return_attributions(movements) -> dict[int, dict]:
    """Какое именно выбытие гасит каждый возврат.

    Доказательством считается сохранённая строка возврата: она ссылается на
    конкретную строку продажи или выдачи в ремонт, а та принадлежит своему
    документу. Этого достаточно для таможни: все движения одной строки
    документа делят и артикул, и действующий таможенный профиль.

    Отмена документа целиком тоже проходит здесь, если её движение помечено
    своим документом. Старые движения, помеченные чужим типом документа,
    доказать нечем - они помечаются неоднозначными и блокируют выгрузку.
    """
    movements = list(movements)
    inbound = [
        movement for movement in movements if movement.movement_type in RETURN_TYPES
    ]
    if not inbound:
        return {}

    return_ids = [
        movement.document_id
        for movement in inbound
        if movement.document_type == "stock_return" and movement.document_id
    ]
    completed = set(
        StockReturn.objects.filter(
            pk__in=return_ids, status=StockReturn.Status.COMPLETED
        ).values_list("pk", flat=True)
    )
    lines_by_return: dict[int, list] = {}
    for line in StockReturnLine.objects.filter(stock_return_id__in=completed).values(
        "stock_return_id", "part_type_id", "quantity",
        "source_sale_line__sale_id", "source_repair_line__repair_order_id",
    ):
        lines_by_return.setdefault(line["stock_return_id"], []).append(line)

    resolved = {}
    for movement in inbound:
        document_type, document_id = _document_key(movement)
        # Отмена документа целиком: движение помечено самим документом.
        if document_type in _DOCUMENT_FIELDS and document_id:
            resolved[movement.pk] = {
                "status": RETURN_EXACT,
                "source": (document_type, document_id),
                "proof": "document",
            }
            continue
        if document_type != "stock_return" or document_id not in completed:
            resolved[movement.pk] = {
                "status": RETURN_AMBIGUOUS, "source": None, "proof": "",
            }
            continue
        lines = [
            line
            for line in lines_by_return.get(document_id, [])
            if line["part_type_id"] == movement.part_type_id
        ]
        sources = {
            ("sale", line["source_sale_line__sale_id"])
            if line["source_sale_line__sale_id"]
            else ("repair_order", line["source_repair_line__repair_order_id"])
            for line in lines
            if line["source_sale_line__sale_id"] or line["source_repair_line__repair_order_id"]
        }
        if len(sources) == 1 and len(lines) == len(sources):
            resolved[movement.pk] = {
                "status": RETURN_EXACT, "source": next(iter(sources)), "proof": "return_line",
            }
        else:
            resolved[movement.pk] = {
                "status": RETURN_AMBIGUOUS, "source": None, "proof": "",
            }
    return resolved


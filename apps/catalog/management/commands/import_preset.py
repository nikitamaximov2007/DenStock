"""Опциональный импорт СТАРТОВЫХ СПРАВОЧНИКОВ из preset (v1.1.5).

Наполняет только справочники существующих моделей (Manufacturer, VehicleType/Make/Model,
Category, PartType, StorageLocation, Supplier) из data/presets/<preset>/*.json. Данные —
подтверждённые термины из docs/research/01. Команда ОПЦИОНАЛЬНА и НЕ создаёт остатки:
никаких Batch/StockLot/PartItem/StockMovement/StockBalance. Идемпотентна (re-run без дублей).
`--dry-run` ничего не пишет в БД.

Совместимость (v1.1.6, compatibility.json): импортируются ТОЛЬКО записи со `status=active`
и только если PartType и VehicleModel уже существуют. `deferred` не импортируются (только
счётчик); отсутствующие сущности → `missing` (не создаём их на лету).
"""
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.catalog.models import (
    Category,
    Manufacturer,
    PartCompatibility,
    PartType,
    Unit,
    VehicleMake,
    VehicleModel,
    VehicleType,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PRESET_ROOT = Path(settings.BASE_DIR) / "data" / "presets"


class Command(BaseCommand):
    help = "Импорт стартовых СПРАВОЧНИКОВ из опционального preset. Остатки не создаёт."

    def add_arguments(self, parser):
        parser.add_argument(
            "--preset", default="mototech", help="Имя preset (папка в data/presets/)."
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Показать план без записи в БД."
        )

    def handle(self, *args, **options):
        self._dry = options["dry_run"]
        preset = options["preset"]
        base = PRESET_ROOT / preset
        if not base.is_dir():
            raise CommandError(f"Preset '{preset}' не найден: {base}")
        self._stats: dict[str, dict[str, int]] = {}

        # Порядок важен: сначала родительские справочники, потом зависимые.
        with transaction.atomic():
            self._section(base, "manufacturers.json", self._import_manufacturers)
            self._section(base, "vehicle_types.json", self._import_vehicle_types)
            self._section(base, "vehicle_makes.json", self._import_vehicle_makes)
            self._section(base, "vehicle_models.json", self._import_vehicle_models)
            self._section(base, "categories.json", self._import_categories)
            self._section(base, "part_types.json", self._import_part_types)
            self._section(base, "storage_locations.json", self._import_storage_locations)
            self._section(base, "suppliers.json", self._import_suppliers)
            # Совместимость — после part_types и vehicle_models (нужны для резолва).
            self._section(base, "compatibility.json", self._import_compatibility)
            self._reference_synonyms(base)
            if self._dry:
                transaction.set_rollback(True)  # dry-run: откатываем всё

        self._print_summary(preset)

    # --- инфраструктура ------------------------------------------------------

    def _section(self, base: Path, filename: str, importer) -> None:
        path = base / filename
        if not path.exists():
            self.stdout.write(
                self.style.WARNING(f"{filename}: файл отсутствует — секция пропущена.")
            )
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"{filename}: некорректный JSON — {exc}") from exc
        importer(data)

    def _mark(self, section: str, key: str) -> None:
        stats = self._stats.setdefault(
            section, {"created": 0, "existing": 0, "deferred": 0, "missing": 0}
        )
        stats[key] += 1

    def _upsert(self, section: str, model, natural: dict, defaults: dict | None = None) -> None:
        _, created = model.objects.get_or_create(**natural, defaults=defaults or {})
        self._mark(section, "created" if created else "existing")

    def _defer(self, section: str, message: str) -> None:
        self._mark(section, "deferred")
        self.stdout.write(self.style.WARNING(f"  отложено [{section}]: {message}"))

    # --- импортеры справочников ---------------------------------------------

    def _import_manufacturers(self, data) -> None:
        for row in data:
            self._upsert("manufacturers", Manufacturer, {"name": row["name"]},
                         {"country": row.get("country", "")})

    def _import_vehicle_types(self, data) -> None:
        for row in data:
            self._upsert("vehicle_types", VehicleType, {"name": row["name"]},
                         {"sort_order": row.get("sort_order", 0)})

    def _import_vehicle_makes(self, data) -> None:
        for row in data:
            vtype = VehicleType.objects.filter(name=row["vehicle_type"]).first()
            if vtype is None:
                self._defer(
                    "vehicle_makes", f"{row['name']}: нет вида техники '{row['vehicle_type']}'"
                )
                continue
            self._upsert("vehicle_makes", VehicleMake, {"vehicle_type": vtype, "name": row["name"]})

    def _import_vehicle_models(self, data) -> None:
        for row in data:
            make = VehicleMake.objects.filter(name=row["vehicle_make"]).first()
            if make is None:
                self._defer("vehicle_models", f"{row['name']}: нет марки '{row['vehicle_make']}'")
                continue
            self._upsert(
                "vehicle_models", VehicleModel,
                {"vehicle_make": make, "name": row["name"],
                 "year_from": row.get("year_from"), "year_to": row.get("year_to")},
            )

    def _import_categories(self, data) -> None:
        for row in data:
            parent = None
            if row.get("parent"):
                parent = Category.objects.filter(name=row["parent"], parent__isnull=True).first()
            self._upsert("categories", Category, {"name": row["name"], "parent": parent},
                         {"sort_order": row.get("sort_order", 0)})

    def _import_part_types(self, data) -> None:
        for row in data:
            category = Category.objects.filter(name=row["category"], parent__isnull=True).first()
            unit = Unit.objects.filter(name=row["unit"]).first()
            if category is None or unit is None:
                self._defer("part_types", f"{row['name']}: нет категории/единицы")
                continue
            manufacturer = None
            if row.get("manufacturer"):
                manufacturer = Manufacturer.objects.filter(name=row["manufacturer"]).first()
            self._upsert(
                "part_types", PartType,
                {"name": row["name"], "category": category},
                {"unit": unit, "manufacturer": manufacturer,
                 "tracking_mode": row.get("tracking_mode", "serial"),
                 "description": row.get("description", "")},
            )

    def _import_storage_locations(self, data) -> None:
        for row in data:
            self._upsert(
                "storage_locations", StorageLocation, {"code": row["code"]},
                {"name": row["name"], "level": row.get("level", "cell"),
                 "purpose": row.get("purpose", "normal"),
                 "storage_allowed": row.get("storage_allowed", True)},
            )

    def _import_suppliers(self, data) -> None:
        for row in data:
            self._upsert("suppliers", Supplier, {"name": row["name"]},
                         {"comment": row.get("comment", ""), "country": row.get("country", "")})

    # --- совместимость (только active, без создания сущностей на лету) --------

    def _resolve_part_type(self, name):
        if not name:
            return None
        return PartType.objects.filter(name=name).first()

    def _resolve_vehicle_model(self, make_name, model_name):
        if not model_name:
            return None
        qs = VehicleModel.objects.filter(name=model_name)
        if make_name:
            qs = qs.filter(vehicle_make__name=make_name)
        return qs.first()

    def _import_compatibility(self, data) -> None:
        for row in data:
            if row.get("status") != "active":
                self._mark("compatibility", "deferred")  # deferred НЕ импортируем
                continue
            part = self._resolve_part_type(row.get("part_type"))
            model = self._resolve_vehicle_model(row.get("vehicle_make"), row.get("vehicle_model"))
            if part is None or model is None:
                self._mark("compatibility", "missing")
                self.stdout.write(self.style.WARNING(
                    f"  отсутствует [compatibility]: "
                    f"{row.get('part_type')} ↔ {row.get('vehicle_model')} — "
                    f"нет PartType/VehicleModel, связь не создана"
                ))
                continue
            self._upsert(
                "compatibility", PartCompatibility,
                {"part": part, "vehicle_model": model,
                 "year_from": row.get("year_from"), "year_to": row.get("year_to")},
                {"note": (row.get("note") or "")[:255]},
            )

    def _reference_synonyms(self, base: Path) -> None:
        path = base / "search_synonyms.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"search_synonyms.json: некорректный JSON — {exc}") from exc
        groups = data.get("groups", [])
        self.stdout.write(
            f"search_synonyms.json: {len(groups)} групп(ы) — справочно, в поиск НЕ интегрируется."
        )

    # --- сводка --------------------------------------------------------------

    def _print_summary(self, preset: str) -> None:
        mode = "DRY-RUN (изменений в БД нет)" if self._dry else "ПРИМЕНЕНО"
        verb = "будет создано" if self._dry else "создано"
        self.stdout.write(self.style.SUCCESS(f"Preset '{preset}' — {mode}"))
        for section, stats in self._stats.items():
            self.stdout.write(
                f"  {section}: {verb}={stats['created']}, "
                f"существует={stats['existing']}, отложено={stats['deferred']}, "
                f"отсутствует={stats['missing']}"
            )

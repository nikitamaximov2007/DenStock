"""Shared metadata step used before regular Sale and Repair completion."""
from apps.inventory.presentation import part_exact_number

from .services import (
    QUICK_ACTION_APPLICATION_AREAS,
    get_or_create_customs,
    parse_application_area,
    parse_weight_g,
    record_customs_data_version,
    validate_weight_pair,
    weight_kg_as_grams,
)


def _has_valid_application_area(value: str) -> bool:
    return value in {str(area) for area in QUICK_ACTION_APPLICATION_AREAS}


def missing_parts(parts):
    result = []
    for part in {part.pk: part for part in parts}.values():
        customs = part.customs_info if hasattr(part, "customs_info") else None
        gross = customs.gross_weight_kg if customs else None
        net = customs.net_weight_kg if customs else None
        area = customs.application_area if customs else ""
        try:
            validate_weight_pair(gross, net)
            valid_pair = gross is not None and net is not None
        except ValueError:
            valid_pair = False
        if not valid_pair or not _has_valid_application_area(area):
            result.append({
                "part": part,
                "article": part_exact_number(part),
                "gross_weight_g": weight_kg_as_grams(gross),
                "net_weight_g": weight_kg_as_grams(net),
                "application_area": area,
            })
    return result


def save_completion_metadata(post, parts, *, by):
    """Validate and persist only the server-derived missing parts."""
    expected = {entry["part"].pk: entry["part"] for entry in missing_parts(parts)}
    submitted = {int(value) for value in post.getlist("part_id") if value.isdigit()}
    if submitted != set(expected):
        raise ValueError("Состав документа изменился. Обновите страницу.")
    for pk, part in expected.items():
        gross = parse_weight_g(post.get(f"gross_weight_g_{pk}"))
        net = parse_weight_g(post.get(f"net_weight_g_{pk}"))
        area = parse_application_area(post.get(f"application_area_{pk}"))
        if gross is None or net is None or not area:
            raise ValueError("Для каждой детали укажите оба веса и область применения.")
        validate_weight_pair(gross, net)
        customs = get_or_create_customs(part)
        customs.gross_weight_kg, customs.net_weight_kg = gross, net
        customs.application_area, customs.updated_by = area, by
        customs.save(update_fields=[
            "gross_weight_kg", "net_weight_kg", "application_area", "updated_by", "updated_at",
        ])
        record_customs_data_version(customs, by=by)

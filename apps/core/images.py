"""Слой 24 — сервисы управления изображениями (primary/soft-delete).

Работают над любым `BaseImage`-наследником: `add_image` принимает related-manager
владельца (`part.images` / `item.images`), `set_primary`/`deactivate_image` — конкретный
объект-изображение (использует его свойство `siblings`). Складскую физику не трогают.
"""
from django.db import transaction


@transaction.atomic
def add_image(images_manager, *, image, caption, by):
    """Создать изображение. Первое активное фото объекта становится primary."""
    has_active = images_manager.filter(is_active=True).exists()
    return images_manager.create(
        image=image, caption=caption, uploaded_by=by, is_primary=not has_active,
    )


@transaction.atomic
def set_primary(image_obj) -> None:
    """Сделать фото главным: сбросить primary у остальных активных того же владельца."""
    if not image_obj.is_active:
        return
    image_obj.siblings.filter(is_primary=True).exclude(pk=image_obj.pk).update(is_primary=False)
    if not image_obj.is_primary:
        image_obj.is_primary = True
        image_obj.save(update_fields=["is_primary"])


@transaction.atomic
def deactivate_image(image_obj) -> None:
    """Мягко удалить (is_active=False). Если было primary — назначить следующее активное."""
    was_primary = image_obj.is_primary
    image_obj.is_active = False
    image_obj.is_primary = False
    image_obj.save(update_fields=["is_active", "is_primary"])
    if was_primary:
        nxt = (
            image_obj.siblings.filter(is_active=True)
            .order_by("sort_order", "uploaded_at")
            .first()
        )
        if nxt:
            nxt.is_primary = True
            nxt.save(update_fields=["is_primary"])

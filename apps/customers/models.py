from django.db import models

from apps.core.phones import normalize_phone


class Customer(models.Model):
    """Постоянная карточка клиента.

    До этой модели клиент существовал только строкой внутри документа, поэтому
    «тот же клиент» нельзя было выразить: два документа с одинаковым текстом
    могли принадлежать разным людям, а один человек мог быть записан по-разному.
    Карточка даёт клиенту стабильный идентификатор (PK), и именно он связывает
    документы между собой.

    Чего здесь СОЗНАТЕЛЬНО нет:

    * уникальности имени: тёзки это норма, а не ошибка ввода;
    * уникальности телефона: один номер бывает семейным, рабочим или общим для
      организации, поэтому он не идентифицирует человека;
    * автоматического слияния карточек: решение «это один человек» принимает
      сотрудник, а не эвристика.

    Документы продолжают хранить СНИМОК имени и телефона на момент проведения.
    Переименование карточки завтра не переписывает историю: см. документы
    `Sale`, `RepairOrder`, `Reservation`.
    """

    name = models.CharField("Имя клиента", max_length=255)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    # Служебная форма только для поиска: цифры, российская 8 приведена к 7.
    # Индекс без уникальности: один номер законно встречается у разных карточек.
    phone_normalized = models.CharField(
        "Телефон для поиска", max_length=50, blank=True, db_index=True, editable=False
    )
    comment = models.TextField("Комментарий", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Клиент"
        verbose_name_plural = "Клиенты"
        ordering = ["name", "pk"]
        indexes = [models.Index(fields=["name"], name="customer_name_idx")]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        self.phone = (self.phone or "").strip()
        self.phone_normalized = normalize_phone(self.phone)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            if "phone" in fields:
                fields.add("phone_normalized")
                kwargs["update_fields"] = sorted(fields)
        super().save(*args, **kwargs)

    def snapshot(self) -> dict:
        """Значения, которые документ замораживает у себя на момент проведения."""
        return {"customer_name": self.name, "customer_phone": self.phone}

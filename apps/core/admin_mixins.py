"""Admin helpers shared by document apps."""


class ReadOnlyDocumentLinesMixin:
    """Document lines are visible in admin but never added, edited or deleted.

    Quantities, parts and lots of a sale, reservation, repair, return,
    write-off, count or batch line change only through each app's services,
    which validate them and write the stock movements. An admin inline was a
    second path around both (for example a fractional quantity on a completed
    sale line), so it is read-only.
    """

    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

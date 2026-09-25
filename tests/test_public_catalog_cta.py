"""Product page primary CTA: "Заказать", truthful for every stock/price state.

Covers task §44: the direct-order button reads "Заказать" for an available,
priced part (targeting that exact PartType's own add-to-cart action, so no
product page ever requires returning to the home page to request it),
"Узнать о поставке" when out of stock, and stays "Заказать" (never a price
claim) when the part is in stock but its price is not yet known - the price
line itself says "Уточнить цену" instead. It never implies online payment:
the disclaimer directly under the button always states this is a request.
"""


def test_available_priced_part_shows_order_cta_targeting_its_own_action(
    public_client, public_catalog
):
    part = public_catalog.part("GUIDE SCREW", article="404105500", price="1200")
    public_catalog.stock(part, "5")

    body = public_client.get(f"/parts/{part.public_id}/").content.decode()

    assert f'action="/cart/{part.public_id}/add/"' in body
    assert "Заказать" in body
    assert "Узнать о поставке" not in body


def test_out_of_stock_part_shows_truthful_supply_cta_not_order(public_client, public_catalog):
    part = public_catalog.part("OUT OF STOCK BOLT", article="OOS-1", price="300")

    body = public_client.get(f"/parts/{part.public_id}/").content.decode()

    assert "Узнать о поставке" in body
    assert ">Заказать<" not in body
    assert "Сейчас нет на складе" in body


def test_in_stock_unknown_price_still_offers_the_order_cta_never_fakes_a_price(
    public_client, public_catalog
):
    part = public_catalog.part("UNPRICED SEAL", article="UP-1", price=None)
    public_catalog.stock(part, "2")

    body = public_client.get(f"/parts/{part.public_id}/").content.decode()

    assert "Заказать" in body
    assert "Уточнить цену" in body
    assert "0 ₽" not in body and "0 ₽" not in body


def test_part_already_in_cart_shows_change_quantity_not_order_again(
    public_client, public_catalog
):
    part = public_catalog.part("CART BELT", article="CB-1", price="700")
    public_catalog.stock(part, "6")
    public_client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})

    body = public_client.get(f"/parts/{part.public_id}/").content.decode()

    assert "Изменить количество" in body
    assert ">Заказать<" not in body


def test_order_cta_disclaimer_states_this_is_a_request_not_a_payment(
    public_client, public_catalog
):
    part = public_catalog.part("HONESTY CHECK PART", article="HC-1", price="900")
    public_catalog.stock(part, "1")

    body = public_client.get(f"/parts/{part.public_id}/").content.decode()

    assert "Заказать" in body
    assert "заявка, а не оплата" in body
    assert "резервирует" in body


def test_product_page_never_requires_returning_home_to_request_this_part(
    public_client, public_catalog
):
    part = public_catalog.part("DIRECT REQUEST PART", article="DR-1", price="450")
    public_catalog.stock(part, "3")

    body = public_client.get(f"/parts/{part.public_id}/").content.decode()

    assert f'action="/cart/{part.public_id}/add/"' in body
    assert "<form" in body

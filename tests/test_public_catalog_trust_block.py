"""Trust/social block: real accounts only, never the private request bot.

Covers task §48/§42/§43: the block renders on home/product/about pages, all
three social links use the owner-confirmed real public accounts, an env
override still works, no internal bot/admin identifiers leak, links are
accessible, the about page is public and in the sitemap, and no fake social
statistics are ever generated.
"""

import re

from django.test import override_settings


def test_trust_block_renders_on_home_product_and_about_pages(public_client, public_catalog):
    part = public_catalog.part("BELT", article="TB-1")

    home = public_client.get("/").content.decode()
    detail = public_client.get(f"/parts/{part.public_id}/").content.decode()
    about = public_client.get("/about/").content.decode()

    assert 'id="trust-title"' in home
    assert 'id="trust-title"' in detail
    assert "Где нас можно найти" in about


def test_all_three_confirmed_social_links_render_by_default(public_client, public_catalog):
    body = public_client.get("/").content.decode()
    assert 'href="https://t.me/probrp1"' in body
    assert 'href="https://vk.ru/club226817030"' in body
    assert 'href="https://www.youtube.com/@pro-stor6592"' in body
    # Safe external-link attributes on every card, not only Telegram's.
    assert body.count('target="_blank"') >= 3
    assert body.count('rel="noopener noreferrer"') >= 3


def test_social_links_never_use_the_private_bot_username_or_max_identity(
    public_client, public_catalog, settings
):
    settings.TELEGRAM_BOT_USERNAME = "internal_support_bot"
    settings.MAX_BOT_TOKEN = "max-internal-secret-token"
    body = public_client.get("/").content.decode()
    assert "internal_support_bot" not in body
    assert "max-internal-secret-token" not in body


def test_social_urls_can_still_be_overridden_by_environment(public_client, public_catalog):
    with override_settings(
        PUBLIC_CATALOG_TELEGRAM_URL="https://t.me/replacement_channel",
        PUBLIC_CATALOG_VK_URL="https://vk.ru/replacement_club",
        PUBLIC_CATALOG_YOUTUBE_URL="https://www.youtube.com/@replacement",
    ):
        body = public_client.get("/").content.decode()
    assert 'href="https://t.me/replacement_channel"' in body
    assert 'href="https://vk.ru/replacement_club"' in body
    assert 'href="https://www.youtube.com/@replacement"' in body
    # The overridden default never leaks alongside the override.
    assert "probrp1" not in body
    assert "pro-stor6592" not in body


def test_social_links_have_accessible_names(public_client, public_catalog):
    body = public_client.get("/").content.decode()
    for key, label in (("telegram", "Telegram"), ("vk", "VK"), ("youtube", "YouTube")):
        match = re.search(
            rf'<a class="trust__social-card trust__social-card--{key}"[^>]*aria-label="([^"]+)"',
            body,
        )
        assert match, f"{key}: no accessible name found in {body}"
        assert label in match.group(1)
        assert "открыть в новой вкладке" in match.group(1)


def test_social_descriptions_make_no_unsupported_claims(public_client, public_catalog):
    body = public_client.get("/").content.decode()
    for unsupported in ("тысяч", "ежедневн", "официальный дилер", "лучш"):
        assert unsupported not in body.lower()


def test_no_fake_social_statistics_anywhere_in_the_trust_block(public_client, public_catalog):
    body = public_client.get("/").content.decode()
    trust_start = body.index('id="trust-title"')
    trust_section = body[trust_start:]
    for fake_claim in ("рейтинг", "отзыв", "клиентов", "подписчик", "официальный дилер"):
        assert fake_claim not in trust_section.lower()


def test_about_page_is_public_and_carries_no_internal_terminology(public_client, public_catalog):
    response = public_client.get("/about/")
    body = response.content.decode()
    assert response.status_code == 200
    assert "<h1" in body
    for internal_term in ("DenisStock", "Django", "PartType", "StockLot", "warehouse"):
        assert internal_term not in body


def test_about_page_includes_all_three_confirmed_social_links(public_client, public_catalog):
    body = public_client.get("/about/").content.decode()
    assert 'href="https://t.me/probrp1"' in body
    assert 'href="https://vk.ru/club226817030"' in body
    assert 'href="https://www.youtube.com/@pro-stor6592"' in body


def test_about_page_indexes_with_everything_else_once_indexing_is_on(
    public_client, public_catalog
):
    off = public_client.get("/about/")
    assert off["X-Robots-Tag"] == "noindex, nofollow"

    with override_settings(PUBLIC_CATALOG_INDEXING=True):
        on = public_client.get("/about/")
    assert "X-Robots-Tag" not in on
    assert 'name="robots"' not in on.content.decode()


def test_about_page_is_listed_in_the_sitemap(public_client, public_catalog):
    with override_settings(PUBLIC_CATALOG_BASE_URL="https://pro-brp.ru"):
        page = public_client.get("/sitemaps/parts-1.xml").content.decode()
    assert "<loc>https://pro-brp.ru/about/</loc>" in page


def test_product_page_trust_block_links_to_the_full_about_page(public_client, public_catalog):
    part = public_catalog.part("BELT", article="TB-2")
    body = public_client.get(f"/parts/{part.public_id}/").content.decode()
    assert 'href="/about/"' in body
    assert "Подробнее о сервисе" in body


def test_trust_block_is_usable_without_javascript_or_cookies(public_client, public_catalog):
    """No script, no client-side dependency - plain links, real markup."""
    body = public_client.get("/").content.decode()
    trust_start = body.index('id="trust-title"')
    trust_end = body.index("</section>", trust_start)
    trust_section = body[trust_start:trust_end]
    assert "<script" not in trust_section
    assert "onclick" not in trust_section


def test_trust_block_social_cards_are_mobile_friendly_markup(public_client, public_catalog):
    """No table/fixed-width layout, no JS-dependent stacking - grid classes
    that collapse to one column by default (see catalog.css .trust__social)."""
    body = public_client.get("/").content.decode()
    trust_start = body.index('id="trust-title"')
    trust_end = body.index("</section>", trust_start)
    trust_section = body[trust_start:trust_end]
    assert "<table" not in trust_section
    assert 'class="trust__social"' in trust_section

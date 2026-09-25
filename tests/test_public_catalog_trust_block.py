"""Trust/social block: real accounts only, never the private request bot.

Covers task §48: the block renders on home/product/about pages, Telegram
uses the confirmed public channel, VK/YouTube stay hidden until configured
(never guessed), no internal bot/admin identifiers leak, links are
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


def test_telegram_link_is_the_confirmed_public_channel(public_client, public_catalog):
    body = public_client.get("/").content.decode()
    assert 'href="https://t.me/probrp1"' in body
    assert 'target="_blank"' in body
    assert 'rel="noopener noreferrer"' in body


def test_telegram_link_never_uses_the_private_bot_username(
    public_client, public_catalog, settings
):
    settings.TELEGRAM_BOT_USERNAME = "internal_support_bot"
    body = public_client.get("/").content.decode()
    assert "internal_support_bot" not in body


def test_vk_and_youtube_are_hidden_until_configured(public_client, public_catalog):
    body = public_client.get("/").content.decode()
    assert 'trust__social-card--vk"' not in body
    assert 'trust__social-card--youtube"' not in body
    assert "vk.com" not in body
    assert "youtube.com" not in body


def test_vk_and_youtube_render_only_when_explicitly_configured(public_client, public_catalog):
    with override_settings(
        PUBLIC_CATALOG_VK_URL="https://vk.com/probrp1",
        PUBLIC_CATALOG_YOUTUBE_URL="https://youtube.com/@probrp",
    ):
        body = public_client.get("/").content.decode()
    assert 'href="https://vk.com/probrp1"' in body
    assert 'href="https://youtube.com/@probrp"' in body


def test_social_links_have_accessible_names(public_client, public_catalog):
    body = public_client.get("/").content.decode()
    match = re.search(
        r'<a class="trust__social-card trust__social-card--telegram"[^>]*aria-label="([^"]+)"',
        body,
    )
    assert match, body
    assert "Telegram" in match.group(1)
    assert "открыть в новой вкладке" in match.group(1)


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
    with override_settings(PUBLIC_CATALOG_BASE_URL="https://pro-stor.ru"):
        page = public_client.get("/sitemaps/parts-1.xml").content.decode()
    assert "<loc>https://pro-stor.ru/about/</loc>" in page


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

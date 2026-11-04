"""浏览器正文返回和CDP重连页面选择，不依赖外部站点。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.infrastructure.external.browser import playwright_browser as module
from app.infrastructure.external.browser.playwright_browser import PlaywrightBrowser


def page_at(url, closed=False):
    return SimpleNamespace(url=url, is_closed=lambda: closed)


@pytest.mark.parametrize("with_blank", [False, True])
def test_initialize_reuses_existing_document_instead_of_creating_blank_tab(monkeypatch, with_blank):
    async def check():
        document = page_at("https://example.test/pricing")
        context = SimpleNamespace(
            pages=[document] + ([page_at("about:blank")] if with_blank else []),
            new_page=AsyncMock(),
        )
        remote = SimpleNamespace(contexts=[context], is_connected=lambda: True)
        playwright = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=AsyncMock(return_value=remote)))
        monkeypatch.setattr(module, "async_playwright", lambda: SimpleNamespace(start=AsyncMock(return_value=playwright)))
        browser = PlaywrightBrowser("http://sandbox:9222")
        assert await browser.initialize()
        assert browser.page is document
        context.new_page.assert_not_awaited()
    asyncio.run(check())


def test_page_selection_follows_popup_but_stays_in_current_context():
    async def check():
        current, popup = page_at("https://example.test/"), page_at("https://example.test/details")
        context = SimpleNamespace(pages=[current, popup, page_at("about:blank")])
        current.context = context
        other = SimpleNamespace(pages=[page_at("https://other.test/")])
        browser = PlaywrightBrowser("http://sandbox:9222")
        browser.browser = SimpleNamespace(contexts=[context, other], is_connected=lambda: True)
        browser.page = current
        await browser._ensure_page()
        assert browser.page is popup
    asyncio.run(check())


def test_closed_page_is_replaced_by_surviving_document():
    async def check():
        closed, survivor = page_at("https://example.test/old", closed=True), page_at("https://example.test/new")
        context = SimpleNamespace(pages=[survivor, closed], new_page=AsyncMock())
        browser = PlaywrightBrowser("http://sandbox:9222")
        browser.browser = SimpleNamespace(contexts=[context], is_connected=lambda: True)
        browser.page = closed
        await browser._ensure_page()
        assert browser.page is survivor
        context.new_page.assert_not_awaited()
    asyncio.run(check())


def readable_browser(content="模型价格正文", status=200):
    browser = PlaywrightBrowser("http://sandbox:9222")
    browser._ensure_page = AsyncMock()
    browser.page = SimpleNamespace(
        url="https://example.test/pricing",
        goto=AsyncMock(return_value=SimpleNamespace(status=status)),
        title=AsyncMock(return_value="定价"),
        wait_for_load_state=AsyncMock(),
    )
    browser._extract_content = AsyncMock(return_value=content)
    browser._extract_interactive_elements = AsyncMock(return_value=["0:<a>更多</a>"])
    return browser


def test_navigation_returns_document_with_url_and_elements():
    async def check():
        browser = readable_browser()
        result = await browser.navigate("https://example.test/pricing")
        assert result.success
        assert result.data["content"] == "模型价格正文"
        assert result.data["url"] == "https://example.test/pricing"
        assert result.data["interactive_elements"]
        browser.page.goto.assert_awaited_once_with("https://example.test/pricing", wait_until="domcontentloaded")
    asyncio.run(check())


def test_empty_document_is_reported_even_when_links_exist():
    async def check():
        result = await readable_browser(content="  ").view_page()
        assert not result.success
        assert "未提取到页面正文" in result.message
        assert result.data["url"] and result.data["interactive_elements"]
    asyncio.run(check())


def test_http_error_page_is_not_reported_as_valid_source():
    async def check():
        result = await readable_browser(content="Access denied", status=403).navigate("https://example.test/pricing")
        assert not result.success
        assert "403" in result.message
        assert result.data["content"] == "Access denied"
    asyncio.run(check())


def test_load_timeout_keeps_available_text_with_warning():
    async def check():
        browser = readable_browser()
        browser.page.wait_for_load_state.side_effect = PlaywrightTimeoutError("timeout")
        result = await browser.view_page()
        assert result.data["content"] == "模型价格正文"
        assert "内容可能不完整" in result.message
        browser._extract_content.assert_awaited_once()
    asyncio.run(check())

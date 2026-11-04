#!/usr/bin/env python
# -*- coding: utf-8 -*-
import asyncio
import logging
from typing import Optional, List, Any

from markdownify import markdownify
from playwright.async_api import async_playwright, Playwright, Browser, Page, TimeoutError as PlaywrightTimeoutError

from app.domain.external.browser import Browser as BrowserProtocol
from app.domain.external.llm import LLM
from app.domain.models.tool_result import ToolResult
from .playwright_browser_fun import (
    GET_VISIBLE_CONTENT_FUNC,
    GET_INTERACTIVE_ELEMENTS_FUNC,
    INJECT_CONSOLE_LOGS_FUNC,
)

logger = logging.getLogger(__name__)


class PlaywrightBrowser(BrowserProtocol):
    """基础Playwright管理的浏览器扩展"""

    def __init__(
            self,
            cdp_url: str,  # CDP的连接地址
            llm: Optional[LLM] = None,  # 可选参数，传递LLM，如果传递了则会使用LLM对页面内容进行整理变成markdown格式
    ) -> None:
        """构造函数，完成Playwright浏览器的初始化"""
        # LLM相关
        self.llm: Optional[LLM] = llm

        # 浏览器相关
        self.cdp_url: str = cdp_url
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None

    async def _ensure_browser(self) -> None:
        """确保浏览器存在，如果不存在则初始化"""
        if not self.browser or not self.browser.is_connected():
            if not await self.initialize():
                raise Exception("初始化Playwright浏览器失败")

    async def _ensure_page(self) -> None:
        """复用当前上下文中最近的非空页面；只有没有可用标签页时才新建。"""
        # 1.先保证浏览器存在
        await self._ensure_browser()

        # 2.连接恢复时从现有上下文选择页面；已连接时保留所属上下文，
        # 不跳到其他上下文。点击打开新页面后仍可跟随最新非空标签页。
        if self.page and not self.page.is_closed():
            contexts = [self.page.context]
        else:
            contexts = self.browser.contexts
        pages = [page for context in contexts for page in context.pages if not page.is_closed()]
        blank_urls = {"", "about:blank", "chrome://newtab/", "chrome://new-tab-page/"}
        readable_pages = [page for page in pages if page.url not in blank_urls]
        if readable_pages or pages:
            # 避免新建的空白页盖过恢复前已经打开的资料页。
            self.page = (readable_pages or pages)[-1]
        else:
            context = contexts[0] if contexts else await self.browser.new_context()
            self.page = await context.new_page()

    async def _extract_content(self) -> str:
        """提取当前页面内容"""
        # 1.提取已加载正文，不逐个拼接父子节点，避免同一段文字重复出现。
        visible_content = await self.page.evaluate(GET_VISIBLE_CONTENT_FUNC)

        # 2.使用markdownify这个库将html文档转换为markdown
        markdown_content = markdownify(visible_content)

        # 部分页面仅包含纯文本；HTML正文提取为空时再尝试一次可见文本。
        if not markdown_content.strip():
            markdown_content = await self.page.locator("body").inner_text(timeout=5000)
        if not markdown_content.strip():
            return ""

        # 3.模型上下文长度有限，提取最大不超过50k个字符
        max_content_length = min(len(markdown_content), 50000)

        # 4.判断是否传递了llm，如果传递了，还可以使用llm对markdown_content进行整理
        if self.llm:
            # 5.调用llm对markdown_content内容进行二次整理
            response = await self.llm.invoke([
                {
                    "role": "system",
                    "content": "您是一名专业的网页信息提取助手。请从当前页面内容中提取所有信息并将其转换为markdown格式。",
                },
                {
                    "role": "user",
                    "content": markdown_content[:max_content_length],
                }
            ])
            return response.get("content") or markdown_content[:max_content_length]
        else:
            return markdown_content[:max_content_length]

    async def _extract_interactive_elements(self) -> List[str]:
        """提取当前页面上的可交互元素"""
        # 1.确保页面存在
        await self._ensure_page()

        # 2.清除当前页面上的缓存可交互元素列表
        self.page.interactive_elements_cache = []

        # 3.执行js脚本获取可交互的元素列表
        interactive_elements = await self.page.evaluate(GET_INTERACTIVE_ELEMENTS_FUNC)

        # 4.更新缓存的可交互元素列表
        self.page.interactive_elements_cache = interactive_elements

        # 5.格式化可交互元素为字符串
        formatted_elements = []
        for element in interactive_elements:
            formatted_elements.append(f"{element['index']}:<{element['tag']}>{element['text']}</{element['tag']}>")

        return formatted_elements

    async def _get_element_by_id(self, index: int) -> Optional[Any]:
        """根据传递的索引/id获取对应的元素"""
        # 1.判断也当前页面是否存在可交互元素缓存
        if (
                not hasattr(self.page, "interactive_elements_cache") or
                not self.page.interactive_elements_cache or
                index >= len(self.page.interactive_elements_cache)
        ):
            return None

        # 2.构建选择器
        selector = f'[data-pa-id="pa-element-{index}"]'
        return await self.page.query_selector(selector)

    async def click(
            self,
            index: Optional[int] = None,
            coordinate_x: Optional[float] = None,
            coordinate_y: Optional[float] = None,
    ) -> ToolResult:
        """根据传递的索引位置+xy坐标实现点击"""
        # 1.确保页面存在
        await self._ensure_page()

        # 2.判断传递的是xy坐标还是index
        if coordinate_x is not None and coordinate_y is not None:
            await self.page.mouse.click(coordinate_x, coordinate_y)
        elif index is not None:
            try:
                # 3.根据index获取元素
                element = await self._get_element_by_id(index)
                if not element:
                    return ToolResult(success=False, message=f"使用索引{index}查找该元素无效, 未找到")

                # 4.检查元素是否是可见的
                is_visible = await self.page.evaluate("""(element) => {
                    if (!element) return false;
                    const rect = element.getBoundingClientRect();
                    const style = window.getComputedStyle(element);
                    return !(
                        rect.width === 0 ||
                        rect.height === 0 ||
                        style.display === 'none' ||
                        style.visibility === 'hidden' ||
                        style.opacity === '0'
                    );
                }""", element)

                # 5.如果元素不可见则执行以下代码
                if not is_visible:
                    # 6.尝试将页面滚动到该元素的位置
                    await self.page.evaluate("""(element) => {
                        if (element) {
                            element.scrollIntoView({behavior: 'smooth', block: 'center'})
                        }
                    }""", element)
                    await asyncio.sleep(1)

                # 7.点击元素
                await element.click(timeout=5000)
            except Exception as e:
                return ToolResult(success=False, message=f"点击元素出错: {str(e)}")

        return ToolResult(success=True)

    async def initialize(self) -> bool:
        """初始化并确保资源是可用的"""
        # 1.定义重试次数+重试延迟确保资源存在
        max_retries = 5
        retry_interval = 1

        # 2.循环开始资源构建
        for attempt in range(max_retries):
            try:
                # 3.创建playwright上下文并连接到cdp浏览器
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url)

                # 4.恢复已有页面，而不是把非空页面视作污染并新建空白页。
                # CDP连接来自当前Session的沙箱；不在不同沙箱间共享页面。
                self.page = None
                await self._ensure_page()

                return True
            except Exception as e:
                # 5.连接失败只释放本地Playwright连接，保留沙箱标签页供重试。
                # cleanup()用于显式重启，会关闭远程页面，此处不能使用。
                if self.playwright:
                    await self.playwright.stop()
                self.page = self.browser = self.playwright = None

                # 11.判断重试次数是否等于最大重试次数
                if attempt == max_retries - 1:
                    logger.error(f"初始化Playwright浏览器失败(已重试{max_retries}次): {str(e)}")
                    return False

                # 12.使用指数级增长进行休眠，最大休眠时间为10s
                retry_interval = min(retry_interval * 2, 10)
                logger.warning(f"初始化Playwright浏览器失败, 即将进行第{attempt + 1}次重试: {str(e)}")
                await asyncio.sleep(retry_interval)

    async def cleanup(self) -> None:
        """清除Playwright资源，包含浏览器+页面+Playwright"""
        try:
            # 1.检测浏览器是否存在，如果存在则删除该浏览器下的所有tabs页面
            if self.browser:
                # 2.获取该浏览器的所有上下文
                contexts = self.browser.contexts
                if contexts:
                    # 3.循环遍历所有上下文逐个处理
                    for context in contexts:
                        # 4.获取每个上下文的所有页面
                        pages = context.pages
                        for page in pages:
                            # 5.循环遍历清除所有页面
                            if not page.is_closed():
                                await page.close()

            # 6.判断self.page是否关闭
            if self.page and not self.page.is_closed():
                await self.page.close()

            # 7.关闭浏览器
            if self.browser:
                await self.browser.close()

            # 8.停止playwright
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            # 9记录错误日志
            logger.error(f"清理Playwright浏览器资源出错: {str(e)}")
        finally:
            # 10.重置所有资源
            self.page = None
            self.browser = None
            self.playwright = None

    async def wait_for_page_load(self, timeout: int = 15) -> bool:
        """传递超时时间，等待当前页面是否加载完毕"""
        # 1.确保当前页面存在
        await self._ensure_page()

        # 2.使用异步任务事件循环中的时间来作为开始时间(只和异步任务相关)
        start_time = asyncio.get_event_loop().time()
        check_interval = 5

        # 3.循环检测网页是否加载成功
        while asyncio.get_event_loop().time() - start_time < timeout:
            # 4.使用js代码判断网页是否加载成功
            is_completed = await self.page.evaluate("""() => document.readyState === 'complete'""")
            if is_completed:
                return True

            # 5.未加载成功则休眠对应时间
            await asyncio.sleep(check_interval)

        return False

    async def navigate(self, url: str) -> ToolResult:
        """根据传递的url跳转到指定页面"""
        # 1.确保页面存在
        await self._ensure_page()

        try:
            # 2.在跳转之前先将可交互元素的缓存清空
            self.page.interactive_elements_cache = []

            # 3.DOM就绪即可读取正文，不因广告、统计等资源未加载完而超时。
            response = await self.page.goto(url, wait_until="domcontentloaded")
            result = await self._read_page()
            if response is not None and response.status >= 400:
                result.success = False
                result.message = f"页面返回HTTP {response.status}，返回内容可能是错误页，不能作为目标资料"
            return result
        except Exception as e:
            # 返回错误的工具结果
            return ToolResult(success=False, message=f"浏览器导航到[{url}]失败: {str(e)}")

    async def view_page(self) -> ToolResult:
        """获取当前网页的内容(内容+可交互元素列表)"""
        # 1.确保页面存在
        await self._ensure_page()

        # 2.等待DOM可用。超时仍可尝试读取已呈现的内容，但标明可能不完整。
        load_timed_out = False
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except PlaywrightTimeoutError:
            load_timed_out = True
        result = await self._read_page()
        if load_timed_out:
            result.message = "页面加载超时，内容可能不完整。" + result.message
        return result

    async def _read_page(self) -> ToolResult:
        """导航与查看共用的正文结果，URL帮助模型识别空白页或重定向。"""
        interactive_elements = await self._extract_interactive_elements()
        content = await self._extract_content()
        return ToolResult(
            success=bool(content.strip()),
            message=("" if content.strip() else "未提取到页面正文，请检查当前URL和页面状态；不能视为已经读取资料"),
            data={
                "url": self.page.url,
                "title": await self.page.title(),
                "content": content,
                "content_truncated": len(content) >= 50000,
                "interactive_elements": interactive_elements,
            }
        )

    async def input(
            self,
            text: str,
            press_enter: bool,
            index: Optional[int] = None,
            coordinate_x: Optional[float] = None,
            coordinate_y: Optional[float] = None,
    ) -> ToolResult:
        """根据传递的文本+换行标识+索引+xy位置实现输入框文本输入"""
        # 1.确保页面存在
        await self._ensure_page()

        # 2.判断下是传递xy还是index
        if coordinate_x is not None and coordinate_y is not None:
            # 3.点击指定位置后输入文本
            await self.page.mouse.click(coordinate_x, coordinate_y)
            await self.page.keyboard.type(text)
        elif index is not None:
            try:
                # 4.根据索引查找元素
                element = await self._get_element_by_id(index)
                if not element:
                    return ToolResult(success=False, message=f"输入文本失败, 该元素不存在")

                try:
                    # 5.先清空原始输入框的内容然后再填充
                    await element.fill("")
                    await element.type(text)
                except Exception as e:
                    # 6.如果填充失败则尝试点击后输入文本，而不是直接清空
                    await element.click()
                    await element.type(text)
            except Exception as e:
                return ToolResult(success=False, message=f"输入文本失败: {str(e)}")

        # 7.判断是否按Enter键
        if press_enter:
            await self.page.keyboard.press("Enter")

        return ToolResult(success=True)

    async def move_mouse(self, coordinate_x: float, coordinate_y: float) -> ToolResult:
        """传递xy坐标移动鼠标"""
        await self._ensure_page()
        await self.page.mouse.move(coordinate_x, coordinate_y)
        return ToolResult(success=True)

    async def press_key(self, key: str) -> ToolResult:
        """传递按键进行模拟"""
        await self._ensure_page()
        await self.page.keyboard.press(key)
        return ToolResult(success=True)

    async def select_option(self, index: int, option: int) -> ToolResult:
        """传递索引+下拉菜单选项选择指定的菜单信息"""
        # 1.确保页面存在
        await self._ensure_page()

        try:
            # 2.获取元素信息
            element = await self._get_element_by_id(index)
            if not element:
                return ToolResult(success=False, message=f"使用索引[{index}]查找该下拉菜单元素不存在")

            # 3.调用函数直接选择对应选项
            await element.select_option(index=option)
            return ToolResult(success=True)
        except Exception as e:
            return ToolResult(success=False, message=f"选择下拉菜单选项失败: {str(e)}")

    async def restart(self, url: str) -> ToolResult:
        """重启并跳转到指定URL"""
        await self.cleanup()
        return await self.navigate(url)

    async def scroll_up(self, to_top: Optional[bool] = None) -> ToolResult:
        """向上滚动浏览器一个屏幕或者整个页面"""
        # 1.确保页面存在
        await self._ensure_page()

        # 2.判断是否滚动到最顶部
        if to_top:
            await self.page.evaluate("window.scrollTo(0, 0)")
        else:
            await self.page.evaluate("window.scrollBy(0, -window.innerHeight)")

        return ToolResult(success=True)

    async def scroll_down(self, to_down: Optional[bool] = None) -> ToolResult:
        """向下滚动浏览器一个屏幕或者到最底部"""
        # 1.确保页面存在
        await self._ensure_page()

        # 2.判断是否滚动到最底部
        if to_down:
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        else:
            await self.page.evaluate("window.scrollBy(0, window.innerHeight)")

        return ToolResult(success=True)

    async def screenshot(self, full_page: Optional[bool] = None) -> bytes:
        """传递full_page完成网页截图"""
        # 1.确保页面存在
        await self._ensure_page()

        # 2.创建一个截图配置
        screenshot_options = {
            "full_page": full_page,
            "type": "png"
        }

        return await self.page.screenshot(**screenshot_options)

    async def console_exec(self, javascript: str) -> ToolResult:
        """传递js代码在当前页面控制台执行"""
        # 1.确保页面存在
        await self._ensure_page()

        # 2.在正式开始执行代码之前先注入logs
        try:
            await self.page.evaluate(INJECT_CONSOLE_LOGS_FUNC)
        except Exception as e:
            logger.warning(f"注入window.console.logs失败: {str(e)}")

        # 3.正式执行js脚本
        result = await self.page.evaluate(javascript)
        return ToolResult(success=True, data={"result": result})

    async def console_view(self, max_lines: Optional[int] = None) -> ToolResult:
        """根据传递的行数查看控制台的日志"""
        # 1.确保页面存在
        await self._ensure_page()

        # 2.可以指定另外一段js代码查看控制台的内容
        logs = await self.page.evaluate("""() => {
            return window.console.logs || [];
        }""")

        if max_lines is not None:
            logs = logs[-max_lines:]

        return ToolResult(success=True, data={"logs": logs})

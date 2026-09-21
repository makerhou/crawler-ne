"""正文 / 作者 / 发布时间抽取。

抓取策略由 `fetch_mode` 决定：
- `requests`  ：只用 HTTP 直连（最快，可能被反爬或拿不到 JS 渲染内容）
- `playwright`：只用 headless 浏览器（最稳，但慢）
- `auto`（推荐）：先 requests；失败或抽不到正文时自动用浏览器兜底

HTML → 结构化内容统一交给 trafilatura（text / author / date / title 一次拿到）。
"""

from __future__ import annotations

import json
import logging
from typing import Any
from lxml import html as lxml_html
from urllib.parse import urljoin

import requests
import trafilatura

from .browser_fetcher import BrowserFetcher

logger = logging.getLogger("reuters-crawler")

EMPTY_RESULT: dict[str, Any] = {
    "title": None,
    "content": None,
    "author": None,
    "publish_time": None,
    "images": [],
}


def fetch_html_with_requests(
    url: str,
    timeout: int = 30,
    user_agent: str = "",
    proxies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """HTTP 直连抓取 HTML（无 TLS 伪装）。

    :raises requests.RequestException: 网络失败（由调用方决定是否回退）
    """
    if headers is None:
        # 与 RSS 同理：带 UA 时一并带上浏览器常规头，降低被反爬拦截的概率
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if user_agent:
            headers["User-Agent"] = user_agent

    response = requests.get(url, headers=headers, timeout=timeout, proxies=proxies)
    response.raise_for_status()
    return response.text


def fetch_html_with_curl_cffi(
    url: str,
    timeout: int = 30,
    user_agent: str = "",
    proxies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    impersonate: str = "chrome124",
) -> str:
    """用 curl_cffi 抓取 HTML —— **模拟真实浏览器 TLS/JA3 指纹**。

    这是绕过 Cloudflare 的关键：Cloudflare 主要通过 TLS 指纹识别 requests/urllib，
    单纯换 UA 无效；curl_cffi 让 TLS 握手特征与真实 Chrome/Firefox 一致。

    :raises ImportError: 未安装 curl_cffi
    :raises Exception: curl_cffi 的请求异常（由调用方决定是否回退）
    """
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise ImportError(
            "未安装 curl_cffi，请执行: pip install curl_cffi"
        ) from exc

    if headers is None:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        if user_agent:
            headers["User-Agent"] = user_agent

    response = curl_requests.get(
        url,
        headers=headers,
        timeout=timeout,
        proxies=proxies,
        impersonate=impersonate,
    )
    response.raise_for_status()
    return response.text


def extract_article(html: str) -> dict[str, Any]:
    """从 HTML 抽取正文 / 作者 / 发布时间 / 标题。"""
    result = dict(EMPTY_RESULT)
    if not html:
        return result

    extracted = trafilatura.extract(
        html,
        output_format="json",
        with_metadata=True,
        include_comments=False,
        include_tables=False,
    )
    if not extracted:
        return result

    try:
        data = json.loads(extracted)
    except (TypeError, json.JSONDecodeError) as exc:
        logger.warning("trafilatura 返回非 JSON（跳过解析）: %s", exc)
        return result

    result["title"] = data.get("title")
    result["content"] = data.get("text")
    result["author"] = data.get("author")
    result["publish_time"] = data.get("date")
    return result


def _img_abs_url(img: "Any", base_url: str) -> str:
    """从 <img> 取可用绝对 URL（兼容 src / data-src / data-lazy-src / srcset 首个）。"""
    raw = (
        img.get("src")
        or img.get("data-src")
        or img.get("data-lazy-src")
        or ""
    )
    if not raw and img.get("srcset"):
        # srcset 形如 "url 2x, url2 1.5x"，取首个 URL
        raw = img.get("srcset", "").split(",")[0].split(" ")[0].strip()
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("data:"):
        return ""
    return urljoin(base_url, raw)


_NON_CONTENT_IMG = (
    "logo", "avatar", "icon", "advert", "banner",
    "placeholder", "spinner", "pixel", "1x1",
)


def _is_non_content_image(url: str) -> bool:
    """过滤 logo / 头像 / 广告 / 占位等非正文图。"""
    return any(k in url.lower() for k in _NON_CONTENT_IMG)


def extract_images(html: str, base_url: str, max_images: int = 20) -> list[str]:
    """从正文 HTML 抽取文章配图 URL（绝对地址），过滤广告/logo/头像类。

    优先在正文容器（<article>/<main>）内查找 <img>，退化为全文档；
    兼容懒加载（data-src / data-lazy-src）与 srcset，转绝对 URL 后去重。
    """
    if not html:
        return []
    try:
        tree = lxml_html.fromstring(html)
    except Exception as exc:
        logger.warning("图片解析失败（跳过）: %s", exc)
        return []

    container = tree.xpath("//article") or tree.xpath("//main") or [tree]
    node = container[0]

    seen: set[str] = set()
    images: list[str] = []
    for img in node.iter("img"):
        url = _img_abs_url(img, base_url)
        if not url or _is_non_content_image(url) or url in seen:
            continue
        seen.add(url)
        images.append(url)
        if len(images) >= max_images:
            break
    return images


def build_content_with_images(
    html: str, base_url: str, max_images: int = 20
) -> tuple[str, list[dict[str, Any]]]:
    """生成纯文本正文（保留段落）+ 图片位置列表（单一数据源，可按位置还原图文混排）。

    一次遍历正文容器（<article>/<main>）的块级元素：文本块拼入 `content`
    （块间以空行分隔，段落边界不丢失），图片块记录其在 `content` 中的字符偏移
    `position`（图片应插入在 `content[position:]` 之前）。返回的 `images` 为
    `[{"url": ..., "position": int}, ...]`，与 `content` 同源，后续按 `position`
    还原即可——无需再冗余存储一份带锚点的文本。
    """
    if not html:
        return "", []
    try:
        tree = lxml_html.fromstring(html)
    except Exception as exc:
        logger.warning("正文解析失败（跳过）: %s", exc)
        return "", []

    container = tree.xpath("//article") or tree.xpath("//main") or [tree]
    node = container[0]

    seen: set[str] = set()
    text_parts: list[str] = []
    images: list[dict[str, Any]] = []
    offset = 0          # 当前已拼入 content 的字符数（= 下一张图的 position）
    first_text = True
    block_tags = ("p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote")
    skip_tags = ("script", "style", "nav", "header", "footer", "aside", "noscript")
    for el in node.iter():
        if el.tag in skip_tags:
            continue
        if el.tag == "img":
            if len(images) >= max_images:
                continue
            url = _img_abs_url(el, base_url)
            if not url or _is_non_content_image(url) or url in seen:
                continue
            seen.add(url)
            images.append({"url": url, "position": offset})
        elif el.tag in block_tags:
            text = " ".join(t.strip() for t in el.itertext() if t and t.strip())
            if text:
                if not first_text:
                    offset += 2  # "\n\n" 段间分隔
                offset += len(text)
                first_text = False
                text_parts.append(text)

    content = "\n\n".join(text_parts)
    return content, images


def reconstruct_with_images(content: str, images: list[dict[str, Any]]) -> str:
    """按 `position` 把图片还原回正文（从右往左插入，保证多个同 position 顺序正确）。

    用于自测与下游复用；图片以 `[IMG:url]` 标记占位。
    """
    if not images:
        return content
    result = content
    for img in sorted(images, key=lambda x: x.get("position", 0), reverse=True):
        pos = img.get("position", 0)
        result = result[:pos] + f"[IMG:{img.get('url', '')}]" + result[pos:]
    return result


def fetch_article_detail(
    url: str,
    timeout: int = 30,
    user_agent: str = "",
    proxies: dict[str, str] | None = None,
    browser: BrowserFetcher | None = None,
    fetch_mode: str = "auto",
    ua_pool: "UserAgentPool | None" = None,
    impersonate: str = "",
    nodriver: "NodriverFetcher | None" = None,
) -> dict[str, Any]:
    """抓取并解析文章，返回 {"title", "content", "author", "publish_time"}。

    通道优先级：
    - `requests`  ：普通 HTTP（无 TLS 伪装）
    - `curl_cffi` ：模拟浏览器 TLS 指纹，但**不执行 JS**
    - `nodriver`  ：反检测浏览器（纯 CDP，无 webdriver 痕迹）——**突破 DataDome 的关键**
    - `playwright`：headless 浏览器（轻量兜底）
    - `auto`（推荐）：curl_cffi → nodriver → playwright，逐级兜底

    `ua_pool` 传入后每次请求轮换身份（UA + 配套头 + TLS 指纹三者一致）。

    浏览器同样失败时返回已得到的结果（可能 content 为空，由上层决定跳过或降级入库）。
    """
    html = ""
    profile = ua_pool.next_profile() if ua_pool else None
    headers = ua_pool.build_headers("html", profile) if ua_pool else None
    effective_impersonate = (
        impersonate
        or (profile.get("impersonate") if profile else "")
        or "chrome124"
    )

    # 1) HTTP 快通道（curl_cffi 默认，具备 TLS 指纹伪装）
    if fetch_mode in ("requests", "curl_cffi", "auto"):
        try:
            if fetch_mode == "requests":
                html = fetch_html_with_requests(url, timeout, user_agent, proxies, headers)
            else:
                html = fetch_html_with_curl_cffi(
                    url, timeout, user_agent, proxies, headers, effective_impersonate
                )
        except Exception as exc:
            if fetch_mode == "requests":
                raise
            logger.info(
                "%s 抓取失败，转浏览器: %s | url=%s",
                fetch_mode,
                exc,
                url[:80],
            )

    result = extract_article(html) if html else dict(EMPTY_RESULT)
    best_html = html  # 记录最终采用正文来源的那份 html，用于抽图片

    # 2) nodriver 反检测通道：能执行 JS 且无 webdriver 痕迹，是突破 DataDome 的关键。
    #    内部被识别时会自动切换身份重试（见 NodriverFetcher）。
    need_nodriver = fetch_mode == "nodriver" or (
        fetch_mode == "auto" and not result.get("content")
    )
    if need_nodriver:
        if nodriver is None:
            logger.debug("未注入 NodriverFetcher，跳过该通道 | url=%s", url[:80])
        else:
            nd_html = nodriver.get_html(url)
            if nd_html:
                nd_result = extract_article(nd_html)
                if nd_result.get("content") or not result.get("content"):
                    result = nd_result
                    best_html = nd_html

    # 3) playwright 轻量兜底
    need_browser = fetch_mode == "playwright" or (
        fetch_mode == "auto" and not result.get("content")
    )
    if need_browser:
        if browser is None:
            logger.warning("需要浏览器兜底但未注入 BrowserFetcher | url=%s", url[:80])
            content_text, img_list = build_content_with_images(best_html, url)
            result["content"] = content_text
            result["images"] = img_list
            return result

        browser_html = browser.get_html(url)
        if browser_html:
            browser_result = extract_article(browser_html)
            # 浏览器拿到正文，或原结果为空 → 采用浏览器结果
            if browser_result.get("content") or not result.get("content"):
                result = browser_result
                best_html = browser_html

    content_text, img_list = build_content_with_images(best_html, url)
    result["content"] = content_text
    result["images"] = img_list
    return result


def strip_html(text: str | None) -> str:
    """把 RSS 里的 HTML 片段转为纯文本（降级内容用）。"""
    if not text:
        return ""
    import html as html_lib
    import re as _re

    cleaned = _re.sub(r"<[^>]+>", " ", text)
    cleaned = html_lib.unescape(cleaned)
    return _re.sub(r"\s+", " ", cleaned).strip()


def build_excerpt(content: str | None, limit: int = 500) -> str | None:
    """从正文生成摘要片段。"""
    if not content:
        return None
    text = content.strip()
    return text[:limit] if len(text) > limit else text

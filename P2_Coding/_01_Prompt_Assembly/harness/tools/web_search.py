"""可选的网络搜索工具 —— 从 P0 `_02_Tool_Calling` 迁移进来的 Bing 搜索。

它是本练习"可选搜索"那一条：默认**不注册**（保持 harness 全离线、测试无网依赖），
只有 CLI 显式加 `--search` 时才注册进工具箱。

为什么单独一个模块：网络访问 + HTML 解析是这里唯一的外部不确定性，隔离在
一个文件里，harness 其余部分就干净、可离线复现。解析失败属于已知限制
（Bing 页面结构会变），失败以结构化错误回给模型（铁律 #7），不炸循环。
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from html import unescape
from typing import Any

from pydantic import BaseModel, Field


class SearchArgs(BaseModel):
    query: str = Field(description="搜索关键词，例如 '上海今天天气'")
    max_results: int = Field(default=5, ge=1, le=10, description="返回结果条数上限")


def parse_bing_results(page_html: str, max_results: int) -> list[dict[str, str]]:
    """从 Bing 搜索结果页里提取标题 / 链接 / 摘要。"""
    results: list[dict[str, str]] = []
    for chunk in page_html.split('<li class="b_algo"')[1:]:
        title_match = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S
        )
        if not title_match:
            continue
        title = unescape(re.sub(r"<[^>]+>", "", title_match.group(2))).strip()
        url = unescape(title_match.group(1))

        snippet = ""
        snippet_match = re.search(r"<p[^>]*>(.*?)</p>", chunk, re.S)
        if snippet_match:
            snippet = unescape(re.sub(r"<[^>]+>", "", snippet_match.group(1))).strip()

        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return results


def web_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """调用 Bing 完成一次真实搜索（需要网络）。失败抛异常，由管线转成错误结果。"""
    url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        page_html = response.read().decode("utf-8", errors="ignore")
    return {"query": query, "results": parse_bing_results(page_html, max_results)}


def make_web_search():
    """返回一个签名为 (**args) -> dict 的搜索实现，可直接注册进注册表。"""

    def run(query: str, max_results: int = 5) -> dict[str, Any]:
        return web_search(query, max_results)

    return run


__all__ = ["SearchArgs", "web_search", "parse_bing_results", "make_web_search"]

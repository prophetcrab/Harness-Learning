#!/usr/bin/env python3
"""Harness 入门 · 报告页本地服务（静态托管 + DeepSeek 代理）。

只属于 `docs/report/` 这一个文件夹：页面、接口、启动器都在这里，
不改项目的 P0–P4 任何代码。

    python docs/report/server.py            # http://127.0.0.1:8765/
    python docs/report/server.py --open     # 顺手打开浏览器
    python docs/report/server.py --port 9000

为什么需要服务端：浏览器直连 DeepSeek 会把 API key 写进页面源码/网络面板，
所以密钥只在服务端从项目根 `.env` 读取，页面只拿到模型回复。

接口：

    GET  /api/status
         {"ok": true, "configured": true, "model": "...", "endpoint": "..."}

    POST /api/chat
         {"messages": [{"role": "user", "content": "..."}], "max_tokens": 512}
         -> {"ok": true, "reply": "...", "model": "...", "usage": {...}}

兼容旧的报告页地址：`/docs/report/<file>` 会映射到本目录的 `<file>`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPORT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = REPORT_DIR.parents[1]
ENV_PATH = PROJECT_ROOT / ".env"

DEFAULT_PAGE = "Harness入门.html"
DEFAULT_BASE_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"

MAX_MESSAGES = 24
MAX_TOTAL_CHARS = 24000
REQUEST_TIMEOUT = 120

ROLES = {"system", "user", "assistant"}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def load_env_file(path: Path) -> dict[str, str]:
    """读 KEY=VALUE 的 .env；不解析行内注释（与项目约定一致）。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_config() -> dict[str, str]:
    """环境变量优先，其次项目根 .env；每次请求重读，改完不用重启。"""
    file_values = load_env_file(ENV_PATH)

    def pick(name: str) -> str:
        return (os.environ.get(name) or file_values.get(name) or "").strip()

    return {
        "api_key": pick("DEEPSEEK_API_KEY"),
        "model": pick("DEEPSEEK_MODEL") or DEFAULT_MODEL,
        "base_url": pick("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL,
    }


def chat_endpoint(raw: str) -> str:
    url = raw.strip().rstrip("/") or DEFAULT_BASE_URL
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


# ---------------------------------------------------------------------------
# DeepSeek 调用
# ---------------------------------------------------------------------------


class ApiError(RuntimeError):
    def __init__(self, message: str, *, status: int = 502, code: str = "upstream_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def clean_messages(raw: object) -> list[dict[str, str]]:
    if not isinstance(raw, list) or not raw:
        raise ApiError("messages 必须是非空数组", status=400, code="bad_request")
    if len(raw) > MAX_MESSAGES:
        raw = raw[-MAX_MESSAGES:]

    messages: list[dict[str, str]] = []
    total = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ApiError("messages 的每一项都必须是对象", status=400, code="bad_request")
        role = item.get("role")
        content = item.get("content")
        if role not in ROLES:
            raise ApiError(f"不支持的 role：{role!r}", status=400, code="bad_request")
        if not isinstance(content, str) or not content.strip():
            raise ApiError("content 必须是非空字符串", status=400, code="bad_request")
        total += len(content)
        messages.append({"role": role, "content": content})
    if total > MAX_TOTAL_CHARS:
        raise ApiError(
            f"消息总长度超过 {MAX_TOTAL_CHARS} 字符（当前 {total}）",
            status=413,
            code="too_large",
        )
    return messages


def call_deepseek(
    messages: list[dict[str, str]], max_tokens: int, temperature: float = 0.7
) -> dict[str, object]:
    config = load_config()
    if not config["api_key"]:
        raise ApiError(
            "服务端没有找到 DEEPSEEK_API_KEY：请在项目根 .env 配置后重启/重试",
            status=503,
            code="no_key",
        )

    payload = {
        "model": config["model"],
        "messages": messages,
        "stream": False,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        chat_endpoint(config["base_url"]),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {config['api_key']}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = _upstream_error(exc.read().decode("utf-8", "replace"), exc.code)
        raise ApiError(detail, status=502, code="upstream_error") from exc
    except TimeoutError as exc:
        raise ApiError("上游请求超时", status=504, code="timeout") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"连不上模型接口：{exc.reason}", status=504, code="network") from exc

    try:
        data = json.loads(raw)
        reply = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ApiError(f"模型返回格式异常：{raw[:200]}", status=502) from exc

    if not isinstance(reply, str) or not reply.strip():
        raise ApiError(
            "模型返回了空内容（max_tokens 可能被推理过程占满，可调大后重试）",
            status=502,
            code="empty_reply",
        )

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
    return {
        "reply": reply.strip(),
        "model": data.get("model") or config["model"],
        "usage": usage,
    }


def _upstream_error(raw: str, status: int) -> str:
    try:
        data = json.loads(raw)
    except ValueError:
        return f"模型接口返回 HTTP {status}：{raw[:200]}"
    error = data.get("error")
    if isinstance(error, dict) and error.get("message"):
        return f"模型接口返回 HTTP {status}：{error['message']}"
    return f"模型接口返回 HTTP {status}：{raw[:200]}"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class ReportHandler(SimpleHTTPRequestHandler):
    server_version = "HarnessReport/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(REPORT_DIR), **kwargs)

    # -- 路径：本目录为根，同时兼容 /docs/report/xxx 旧地址 --

    def translate_path(self, path: str) -> str:
        raw = unquote(urlsplit(path).path)
        if raw in ("", "/"):
            raw = "/" + DEFAULT_PAGE
        elif raw.startswith("/docs/report/"):
            raw = raw[len("/docs/report") :]
        elif raw in ("/docs/report", "/docs/report/"):
            raw = "/" + DEFAULT_PAGE

        candidate = (REPORT_DIR / raw.lstrip("/")).resolve()
        try:
            candidate.relative_to(REPORT_DIR)
        except ValueError:
            return str(REPORT_DIR / "__forbidden__")
        return str(candidate)

    def guess_type(self, path: str) -> str:
        ctype = super().guess_type(path)
        if ctype.startswith("text/") or ctype in ("application/json", "application/javascript"):
            return ctype + "; charset=utf-8"
        return ctype

    def end_headers(self) -> None:
        if self.path.split("?", 1)[0].endswith((".html", ".js", ".css", ".json")):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    # -- 路由 --

    def do_GET(self) -> None:  # noqa: N802 —— 基类命名
        if urlsplit(self.path).path == "/api/status":
            config = load_config()
            self._json(
                200,
                {
                    "ok": True,
                    "configured": bool(config["api_key"]),
                    "model": config["model"],
                    "endpoint": chat_endpoint(config["base_url"]),
                },
            )
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 —— 基类命名
        if urlsplit(self.path).path != "/api/chat":
            self.send_error(404, "Not Found")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 1_000_000:
                raise ApiError("请求体为空或过大", status=400, code="bad_request")
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ApiError("请求体必须是 JSON 对象", status=400, code="bad_request")
            messages = clean_messages(body.get("messages"))
            max_tokens = int(body.get("max_tokens", 512))
            max_tokens = max(16, min(8192, max_tokens))
            temperature = float(body.get("temperature", 0.7))
            temperature = max(0.0, min(2.0, temperature))
            result = call_deepseek(messages, max_tokens, temperature)
        except ApiError as exc:
            self._json(exc.status, {"ok": False, "code": exc.code, "error": str(exc)})
            return
        except (ValueError, json.JSONDecodeError):
            self._json(
                400,
                {"ok": False, "code": "bad_request", "error": "请求体不是合法 JSON"},
            )
            return
        except Exception as exc:  # noqa: BLE001 —— 兜底也要给页面一个结构化错误
            self._json(
                500,
                {"ok": False, "code": "internal_error", "error": f"{type(exc).__name__}: {exc}"},
            )
            return

        self._json(200, {"ok": True, **result})

    def _json(self, status: int, payload: dict[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        sys.stdout.write("%s - %s\n" % (self.address_string(), format % args))
        sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Harness 入门报告页（静态 + DeepSeek 代理）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    config = load_config()
    server = ThreadingHTTPServer((args.host, args.port), ReportHandler)
    url = f"http://{args.host}:{args.port}/{DEFAULT_PAGE}"

    print(f"报告页：{url}")
    print(f"页面目录：{REPORT_DIR}")
    print(
        "模型接口："
        + (chat_endpoint(config["base_url"]) + f"（模型 {config['model']}）")
    )
    print(
        "API key："
        + ("已配置（只保存在服务端）" if config["api_key"] else "未配置（聊天演示将不可用）")
    )
    print("Ctrl+C 停止")

    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

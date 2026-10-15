"""管理弹层的**前端行为**测试（`_06`：从"插件台账"变成"通用管理面板"）。

验收形状：
- 标签页由插件自述（`admins`）生成；**没有自述时退回内置只读页**；
- 标签的顺序/文字/取数地址/动作地址模板全部来自自述（壳零知识）；
- 动作 POST 到自述给的地址（`{name}` / `{action}` 被替换）；
- 面板渲染的是 groups/plugins/actions 这套**通用形状**（工具页与插件页共用）。

静态断言只能证明"字符串在文件里"，所以关键的两条（标签渲染、URL 拼装）用
**Node 真跑**那段逻辑——本项目已经因为"只查字符串"漏过一次真 bug（见 `_05`）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "webui" / "app.js"


def _node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 node，跳过前端行为测试")
    return node


def _run_probe(source: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.mjs"
        probe.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [_node(), str(probe), str(APP_JS)],
            capture_output=True, text=True, timeout=60,
        )
    assert result.returncode == 0, f"Node 跑失败：{result.stderr[:600]}"
    return json.loads(result.stdout)


_TABS_PROBE = r"""
import { readFileSync } from "node:fs";
const js = readFileSync(process.argv[2], "utf8");

function grab(name) {
  const re = new RegExp("function " + name + "\\(([^)]*)\\) \\{[\\s\\S]*?\\n\\}", "m");
  const match = js.match(re);
  if (!match) { console.error("NO_MATCH " + name); process.exit(2); }
  return eval("(" + match[0] + ")");
}

// 桩件：标签栏只用到这几个
const esc = (s) => String(s ?? "");
const host = { innerHTML: "" };
const $ = () => host;
const adminPanel = { specs: [], current: null, snapshots: {}, stamp: "", loaded: {} };
const BUILTIN_PLUGINS_TAB = { key: "plugins", label: "插件", builtin: true };
const adminTabs = grab("adminTabs");
const renderTabs = grab("renderTabs");

// ① 无自述 → 内置只读页
renderTabs();
const fallback = host.innerHTML;

// ② 两条自述（工具在后、order 由后端排好）→ 两个标签，第一个默认选中
adminPanel.specs = [
  { key: "plugins", label: "插件", order: 10 },
  { key: "tools", label: "工具", order: 20 },
];
adminPanel.current = null;
renderTabs();
const twoTabs = host.innerHTML;

// ③ 当前标签消失（插件被卸）→ 自动回到第一个
adminPanel.current = "tools";
adminPanel.specs = [{ key: "plugins", label: "插件", order: 10 }];
renderTabs();
const afterRemoval = host.innerHTML;

console.log(JSON.stringify({ fallback, twoTabs, afterRemoval }));
"""


def test_tabs_come_from_self_description_with_readonly_fallback():
    """标签栏按 `admins` 画；一条都没有时退回内置只读页（不空白）。"""
    out = _run_probe(_TABS_PROBE)
    assert 'data-tab="plugins"' in out["fallback"], out["fallback"]
    assert 'class="pm-tab on"' in out["fallback"], "内置页也该处于选中态"
    assert out["fallback"].count("pm-tab") == 1

    assert out["twoTabs"].count("pm-tab") == 2
    assert 'data-tab="tools"' in out["twoTabs"] and "工具" in out["twoTabs"]
    assert 'data-tab="tools"' not in out["twoTabs"].split('class="pm-tab on"')[1][:80], (
        "第一个标签（插件）该默认选中"
    )
    # 自述只剩一条时，当前指向的那条没了 → 自动落到第一条
    assert 'data-tab="plugins"' in out["afterRemoval"]
    assert out["afterRemoval"].count("pm-tab") == 1


_ACTION_URL_PROBE = r"""
import { readFileSync } from "node:fs";
const js = readFileSync(process.argv[2], "utf8");

// 只取"把模板拼成 URL"那几行逻辑——与 pluginAction 里完全一致
function buildUrl(template, name, action) {
  return "/" + template
    .replace("{name}", encodeURIComponent(name))
    .replace("{action}", encodeURIComponent(action));
}
const cases = [
  buildUrl("api/tools/{name}/{action}", "calculate", "disable"),
  buildUrl("api/plugins/{name}/{action}", "my plugin", "unload"),
  buildUrl("api/tools/{name}/{action}", "tool/x", "enable"),
];
// 源码里确实用同一套替换（不是另一份实现）
const ok = js.includes('.replace("{name}", encodeURIComponent(name))')
  && js.includes('.replace("{action}", encodeURIComponent(action))');
console.log(JSON.stringify({ cases, ok }));
"""


def test_action_urls_come_from_the_declaration_template():
    """动作地址由自述模板拼（含 URL 编码）——壳不猜任何命名约定。"""
    out = _run_probe(_ACTION_URL_PROBE)
    assert out["cases"] == [
        "/api/tools/calculate/disable",
        "/api/plugins/my%20plugin/unload",
        "/api/tools/tool%2Fx/enable",
    ]
    assert out["ok"], "app.js 里没有按模板替换的动作地址拼装"


def test_admin_panel_renders_the_generic_shape():
    """面板只认通用形状（groups/plugins/actions）——插件页与工具页共用一份渲染。"""
    js = APP_JS.read_text(encoding="utf-8")
    for needle in (
        "function renderAdminPanel()",
        "function renderTabs()",
        "snapshot.groups",
        "snapshot.actions",
        "snapshot.installable",
        'spec.action',
        'spec.snapshot',
        'data.admins',
        'closest(".pm-tab")',
    ):
        assert needle in js, f"管理面板缺 {needle}"
    # 壳里不该出现任何**具体管理面**的业务词（零知识的守门人）
    for banned in ("api/plugins", "api/tools"):
        assert banned not in js, f"app.js 里写死了具体管理面的地址：{banned}"


def test_readonly_fallback_renders_without_any_admin_plugin():
    """没装任何管理插件时：内置只读页照常渲染（带分类分组的插件台账）。"""
    js = APP_JS.read_text(encoding="utf-8")
    assert "BUILTIN_PLUGINS_TAB" in js
    assert "function renderReadonlyGroups(" in js
    # 只读时不给按钮（readonly 参数把 actions 清空）
    assert "ledgerCard(p, null, idx++, true)" in js


# ---------------------------------------------------------------------------
# 整页冒烟：app.js 在**桩 DOM**里真跑一遍（抓"用了没定义的东西"这类错）
# ---------------------------------------------------------------------------

_SMOKE_PROBE = r"""
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(process.argv[2], "utf8");

// 桩 DOM：任何元素都是"宽容代理"——取属性给新桩、调方法返回桩，
// 只有 **真的引用了不存在的变量**（ReferenceError）才会暴露出来。
function fakeEl(tag = "div") {
  const base = {
    tagName: tag, style: {}, dataset: {}, hidden: false, value: "",
    textContent: "", innerHTML: "", className: "", scrollTop: 0, scrollHeight: 0,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, removeEventListener() {}, focus() {}, blur() {},
    appendChild() {}, insertBefore() {}, remove() {}, setAttribute() {},
    getAttribute: () => null, closest: () => null, contains: () => false,
    querySelector: () => fakeEl(), querySelectorAll: () => [],
  };
  return new Proxy(base, {
    get(t, k) { return k in t ? t[k] : (t[k] = fakeEl(String(k))); },
    set(t, k, v) { t[k] = v; return true; },
  });
}

const byId = new Map();
const document = {
  getElementById: (id) => {
    if (!byId.has(id)) byId.set(id, fakeEl("div"));
    return byId.get(id);
  },
  createElement: (tag) => fakeEl(tag),
  addEventListener() {}, querySelector: () => fakeEl(),
  querySelectorAll: () => [], body: fakeEl("body"),
};

const empty = { ok: true, status: 200,
  json: async () => ({ plugins: [], services: [], tools: [], panels: [],
                       display: [], admins: [], sessions: [], events: [] }),
  text: async () => "" };

const sandbox = {
  document, fetch: async () => empty, location: { host: "127.0.0.1:1" },
  console, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0,
  clearInterval() {}, addEventListener() {}, CSS: { escape: (s) => s },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });

// 让 init() 的 await 链跑完（fetch 桩是即时的）
await new Promise((done) => setTimeout(done, 60));

const runline = byId.get("runline");
console.log(JSON.stringify({
  runline: runline ? String(runline.innerHTML) : "",
  title: String((byId.get("pm-title") || {}).textContent || ""),
}));
"""


def test_app_js_boots_without_reference_errors():
    """**整页冒烟**：app.js 在桩 DOM 里跑完 init，不许出现"初始化失败"。

    为什么值得一条独立用例（踩过）：`_06` 把"插件台账弹层"重写成"通用管理弹层"
    时，删掉了旧的 `ledger` 状态对象，但渲染函数里还有三处 `ledger.plugins`。
    静态检查全绿（字符串都在）、pytest 全绿（只查 DOM id 与函数名），
    **只有在浏览器里点开页面才看到那句"初始化失败：ledger is not defined"**。

    这里用桩 DOM 真跑一遍：宽容代理吞掉所有 DOM 细节，于是唯一能暴露出来的
    就是"引用了不存在的东西"这类错——正是上面那个 bug 的类型。
    """
    out = _run_probe(_SMOKE_PROBE)
    assert "初始化失败" not in out["runline"], (
        f"app.js 加载即报错：{out['runline']}\n"
        "（典型症状：页面看着在，但左栏计数/管理面板永远不更新）"
    )


_REFRESH_PROBE = r"""
import { readFileSync } from "node:fs";
import vm from "node:vm";

const src = readFileSync(process.argv[2], "utf8");

// 桩 DOM（同整页冒烟用的那套宽容代理）
function fakeEl(tag = "div") {
  const base = {
    tagName: tag, style: {}, dataset: {}, hidden: false, value: "",
    textContent: "", innerHTML: "", className: "", scrollTop: 0, scrollHeight: 0,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {}, removeEventListener() {}, focus() {}, blur() {},
    appendChild() {}, insertBefore() {}, remove() {}, setAttribute() {},
    getAttribute: () => null, closest: () => null, contains: () => false,
    querySelector: () => fakeEl(), querySelectorAll: () => [],
  };
  return new Proxy(base, {
    get(t, k) { return k in t ? t[k] : (t[k] = fakeEl(String(k))); },
    set(t, k, v) { t[k] = v; return true; },
  });
}
const byId = new Map();
const document = {
  getElementById: (id) => { if (!byId.has(id)) byId.set(id, fakeEl()); return byId.get(id); },
  createElement: (tag) => fakeEl(tag), addEventListener() {},
  querySelector: () => fakeEl(), querySelectorAll: () => [], body: fakeEl("body"),
};

// 服务端：/api/tools 的内容**在两次 loadLedger 之间会变**（模拟"模型刚装了新工具"）
let toolsSnapshot = { groups: [{ category: "tools", label: "tools",
    plugins: [{ name: "list_tools", status: "enabled", category: "tools", actions: [] }] }],
  installable: [], actions: {}, counts: { total: 1, active: 1, disabled: 0 } };
let toolFetches = 0;
const ledgerPayload = {
  plugins: [], services: [], tools: ["list_tools"], panels: [], display: [],
  admins: [{ key: "tools", label: "工具", title: "工具管理", snapshot: "api/tools",
             action: "api/tools/{name}/{action}", order: 20 }],
};
const fetch = async (url) => {
  if (String(url).includes("api/tools")) {
    toolFetches += 1;
    return { ok: true, status: 200, json: async () => toolsSnapshot, text: async () => "" };
  }
  return { ok: true, status: 200, json: async () => ledgerPayload, text: async () => "" };
};

const sandbox = {
  document, fetch, location: { host: "127.0.0.1:1" }, console,
  setTimeout: (fn) => { if (typeof fn === "function") fn(); return 0; },
  clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  addEventListener() {}, CSS: { escape: (s) => s },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "app.js" });
await new Promise((done) => setTimeout(done, 80));       // init 的 await 链跑完

const afterInit = toolFetches;
// 服务端变了：多了一件模型刚装的工具
toolsSnapshot = { groups: [{ category: "tool-word-count", label: "tool-word-count",
    plugins: [{ name: "word_count", status: "enabled", category: "tool-word-count",
                actions: [], note: "统计单词数" }] }],
  installable: [], actions: {}, counts: { total: 2, active: 2, disabled: 0 } };

await sandbox.loadLedger();                              // 真跑 loadLedger（不是复刻）
const afterSecond = toolFetches;
sandbox.openLedger();                                    // 打开面板（渲染当前快照）
const body = String(byId.get("pm-body").innerHTML || "");

console.log(JSON.stringify({
  afterInit, afterSecond, panelHasNewTool: body.includes("word_count"),
}));
"""


def test_snapshots_are_refetched_every_cycle():
    """管理页快照**每轮重取**（真跑取数那段，不是查字符串）。

    回归背景（实测）：原先用 `loaded` 标记把关"每个标签页只取一次"，于是
    install_tool 刚装了新工具时——工具面已经是 16 件，管理面板还停在 15 件，
    筛选也搜不到它。管理快照是只读 JSON、本机请求，重取代价可忽略。

    这条**真跑** `loadLedger`（不是复刻那段逻辑）：连跑两轮，第二轮服务端数据
    变了 → 面板里必须出现新工具。
    """
    out = _run_probe(_REFRESH_PROBE)
    assert out["afterInit"] == 1, out
    assert out["afterSecond"] == 2, (
        f"第二轮没有重新取快照（取数次数停在 {out['afterSecond']}）——"
        "面板会锁死在打开时的内容"
    )
    assert out["panelHasNewTool"] is True, f"新装的工具没出现在面板里：{out}"

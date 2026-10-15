"use strict";
/* ---------------- utils ---------------- */
const $ = (id) => document.getElementById(id);
const chatEl = $("chat"), wrapEl = chatEl.querySelector(".wrap");
const sessionsEl = $("sessions"), eventsEl = $("events");
const state = { current: null, sending: false, follow: true, display: [], runningIds: new Set() };
window.addEventListener("error", (e) => { window.__lastError = String(e.message || e); });

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const clip = (s, n = 120) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n) + "…" : s; };
const cssEsc = (s) => (window.CSS && CSS.escape) ? CSS.escape(String(s))
  : String(s).replace(/["\\]/g, "\\$&");
const fmtTime = (iso) => { try { return new Date(iso).toLocaleTimeString("zh-CN", { hour12: false }); }
  catch { return ""; } };
const el = (html) => { const t = document.createElement("template");
  t.innerHTML = html.trim(); return t.content.firstElementChild; };

/* ---------------- mini markdown ---------------- */
let codeSeq = 0;
const previewCode = {};   // code id → {lang, code}：预览内容按需在切换时注入
function codeBlockHtml(lang, code) {
  const id = `code-${++codeSeq}`;
  const l = String(lang || "").toLowerCase();
  const previewable = ["html", "htm", "svg", "css"].includes(l);
  previewCode[id] = { lang: l, code };
  return `<div class="codeblock"><div class="code-head"><span>${esc(lang || "text")}</span>`
    + `<span class="code-actions">`
    + (previewable ? `<button type="button" class="preview-btn" data-pre="${id}">预览</button>` : "")
    + `<button type="button" class="copy" data-pre="${id}">复制</button></span></div>`
    + `<pre id="${id}">${esc(code.replace(/\n$/, ""))}</pre>`
    + (previewable ? `<div class="previewbox" id="pv-${id}" hidden></div>` : "")
    + `</div>`;
}
function previewSrc(id) {
  const { lang, code } = previewCode[id] || {};
  if (lang === "css") {
    // CSS 片段单独预览没有意义：给一个最小示例骨架，让样式有东西可作用
    return `<!doctype html><html><head><meta charset="utf-8"><style>${code}</style></head>`
      + `<body style="font:14px/1.6 system-ui,'Microsoft YaHei',sans-serif;padding:16px">`
      + `<h3 style="margin:0 0 6px">CSS 预览（示例骨架）</h3>`
      + `<p>这段样式已应用到下面的元素。</p>`
      + `<button>按钮</button> <a href="#">链接</a>`
      + `<div class="card" style="margin-top:10px;padding:8px;border:1px solid #ddd">卡片内容</div>`
      + `</body></html>`;
  }
  // html / svg：已经是完整文档就直接用，否则包一层
  return /<html|<!doctype/i.test(code)
    ? code
    : `<!doctype html><html><head><meta charset="utf-8"></head><body>${code}</body></html>`;
}
function md(src) {
  const fences = [], inlines = [];
  let t = String(src ?? "");
  t = t.replace(/```([\w+#.-]*)[ \t]*\r?\n?([\s\S]*?)```/g, (m, lang, code) => {
    fences.push([lang, code]); return `\uE000F${fences.length - 1}\uE000`;
  });
  t = t.replace(/`([^`\n]+)`/g, (m, c) => { inlines.push(c); return `\uE000I${inlines.length - 1}\uE000`; });
  t = esc(t);
  t = t.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  t = t.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  t = t.replace(/(^|[\s(])\*([^*\n]+)\*(?=$|[\s).,，。！？!?；;])/g, "$1<em>$2</em>");
  t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  const lines = t.split("\n");
  const out = [];
  let list = null, quote = null, para = null;
  const flushList = () => { if (list) {
    out.push(`<${list.tag}>${list.items.map((i) => `<li>${i}</li>`).join("")}</${list.tag}>`);
    list = null; } };
  const flushQuote = () => { if (quote) {
    out.push(`<blockquote>${quote.join("<br>")}</blockquote>`); quote = null; } };
  const flushPara = () => { if (para) {
    out.push(`<p>${para.join("<br>")}</p>`); para = null; } };
  const flushAll = () => { flushList(); flushQuote(); flushPara(); };

  // 表格：表头行 + 分隔行（| --- | --- |），随后是数据行
  const isSeparator = (s) => s.includes("|") && s.includes("-")
    && /^\|?[\s:|-]+\|?$/.test(s);
  const splitCells = (s) => s.replace(/^\s*\|/, "").replace(/\|\s*$/, "")
    .split("|").map((c) => c.trim());
  const alignOf = (c) => (/^:-+:$/.test(c) ? "center"
    : /^:-+/.test(c) ? "left" : /-+:$/.test(c) ? "right" : "");

  for (let i = 0; i < lines.length; i++) {
    const s = lines[i].trim();
    if (/^\uE000F\d+\uE000$/.test(s)) { flushAll(); out.push(s); continue; }
    if (!s) { flushAll(); continue; }

    // 表格
    if (/^\|.*\|$/.test(s) && i + 1 < lines.length
        && isSeparator(lines[i + 1].trim())) {
      flushAll();
      const head = splitCells(s);
      const align = splitCells(lines[i + 1].trim()).map(alignOf);
      const rows = [];
      i += 2;
      while (i < lines.length && /^\|.*\|$/.test(lines[i].trim())) {
        rows.push(splitCells(lines[i].trim())); i++;
      }
      i--;
      const style = (k) => (align[k] ? ` style="text-align:${align[k]}"` : "");
      const th = head.map((c, k) => `<th${style(k)}>${c}</th>`).join("");
      const tb = rows.map((r) =>
        `<tr>${r.map((c, k) => `<td${style(k)}>${c}</td>`).join("")}</tr>`).join("");
      out.push(`<div class="tablewrap"><table><thead><tr>${th}</tr></thead>`
        + `<tbody>${tb}</tbody></table></div>`);
      continue;
    }
    // 标题
    let m = s.match(/^(#{1,6})\s+(.*)$/);
    if (m) { flushAll(); const lv = Math.min(m[1].length, 4);
      out.push(`<h${lv}>${m[2]}</h${lv}>`); continue; }
    // 分隔线
    if (/^([-*_])\1{2,}$/.test(s.replace(/\s/g, ""))) { flushAll(); out.push("<hr>"); continue; }
    // 引用（> 已被转义成 &gt;）
    m = s.match(/^&gt;\s?(.*)$/);
    if (m) { flushList(); flushPara(); (quote || (quote = [])).push(m[1]); continue; }
    // 无序列表
    m = s.match(/^[-*•]\s+(.*)$/);
    if (m) { flushQuote(); flushPara();
      if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; }
      list.items.push(m[1]); continue; }
    // 有序列表
    m = s.match(/^\d+[.)]\s+(.*)$/);
    if (m) { flushQuote(); flushPara();
      if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; }
      list.items.push(m[1]); continue; }
    // 普通段落（连续行合并，软换行保留为 <br>）
    flushList(); flushQuote();
    (para || (para = [])).push(s);
  }
  flushAll();
  let html = out.join("");
  html = html.replace(/\uE000I(\d+)\uE000/g, (m, i) => `<code>${esc(inlines[+i])}</code>`);
  html = html.replace(/\uE000F(\d+)\uE000/g, (m, i) => codeBlockHtml(fences[+i][0], fences[+i][1]));
  return html;
}

/* ---------------- chat nodes ---------------- */
function userNode(text) {
  return el(`<div class="row user"><div class="msg user">${esc(text).replace(/\n/g, "<br>")}</div></div>`);
}
function assistantNode(content) {
  return el(`<div class="row assistant"><div class="prose">${md(content)}</div></div>`);
}
function turnStatusNode(turn, status, steps) {
  const label = { done: "完成", max_steps: "步数用尽", cancelled: "已取消" }[status] || status;
  return el(`<div class="row status"><span>turn ${esc(turn)} · ${esc(label)} · ${esc(steps)} 步</span></div>`);
}
function systemChip(prompt) {
  return el(`<div class="row sys"><details class="syschip">`
    + `<summary>系统提示词（点开查看）</summary><pre>${esc(prompt)}</pre></details></div>`);
}
function sysNoteNode() { return el(`<div class="row sys"><div class="sysnote">系统提示词已更新</div></div>`); }
/* ---------------- 通用渲染器：壳按插件的"显示自述"分派 ----------------

   壳不认识任何具体事件名（如 `某个插件/某事发生了`）。它只做两件事：
     ① 从 /api/ledger 的 `display` 里查"这个 kind 该怎么画"；
     ② 按 renderer 名调下面四个通用渲染器之一。

   于是"加一个会产出事件的插件"不用改这个文件——插件声明 DisplaySpec 即可。
   四种渲染器覆盖了常规形态；特殊呈现（图表之类）是插件**自己页面**的事。 */

/** 按插件的显示自述渲染一条事件；查不到返回 null（壳不猜，宁可不画）。 */
function renderBySpec(kind, payload) {
  const spec = (state.display || []).find((d) => d.kind === kind);
  if (!spec) return null;
  const title = fillTemplate(spec.title, payload);
  switch (spec.renderer) {
    case "fold": {
      // 可折叠徽标：标题一行，点开看详情字段（如压缩摘要全文）
      const detail = spec.detail_field ? String(payload[spec.detail_field] ?? "") : "";
      const node = el(`<div class="row compact"><details class="cmpchip">`
        + `<summary>⟳ ${esc(title)}</summary>`
        + (detail ? `<pre>${esc(detail)}</pre>` : "")
        + `</details></div>`);
      return node;
    }
    case "warn":
      return el(`<div class="row sys"><div class="sysnote warn">${esc(title)}</div></div>`);
    case "note":
      return el(`<div class="row sys"><div class="sysnote">${esc(title)}</div></div>`);
    case "status":
      setIdle(title);
      return null;                       // 只更新状态栏，不进对话流
    default:
      return null;
  }
}

/** 模板取字段：`{folded}` → payload.folded。数字加千分位，缺值给 "?"。 */
function fillTemplate(template, payload) {
  return String(template || "").replace(/\{(\w+)\}/g, (_, key) => {
    const raw = payload ? payload[key] : undefined;
    if (raw == null || raw === "") return "?";
    return typeof raw === "number" ? raw.toLocaleString("en-US") : String(raw);
  });
}
function fmtTokens(n) {
  return typeof n === "number" && n > 0 ? n.toLocaleString("en-US") : "";
}

function toolCard({ callId, name, args, result, isError }) {
  const argsText = args == null ? "" : (typeof args === "string" ? args : JSON.stringify(args));
  const node = el(`<div class="toolcard" data-call="${esc(callId)}">`
    + `<div class="tool-head"><span class="tool-name">${esc(name)}</span>`
    + `<span class="tool-args">${esc(clip(argsText, 110))}</span>`
    + `<span class="tool-state">运行中…</span></div>`
    + `<div class="tool-body"></div></div>`);
  if (result !== undefined) fillToolCard(node, result, isError);
  return node;
}
function fillToolCard(node, result, isError) {
  node.classList.add(isError ? "error" : "ok");
  node.querySelector(".tool-state").textContent = isError ? "✗ 失败" : "✓ 完成";
  const body = node.querySelector(".tool-body");
  const text = typeof result === "string" ? result : JSON.stringify(result, null, 2);
  if (text == null || text === "{}" || text === "") { body.remove(); return; }
  body.innerHTML = (text.includes("\n") || text.length > 200)
    ? `<details><summary>结果（${text.length} 字符）</summary><pre>${esc(clip(text, 4000))}</pre></details>`
    : `<pre class="one">${esc(text)}</pre>`;
}

/* ---------------- chat rendering ---------------- */
function renderChatFromEvents(events) {
  wrapEl.innerHTML = "";
  if (!events.length) { wrapEl.innerHTML = emptyStateHtml(); return; }
  for (const ev of events) {
    const d = ev.data || {};
    switch (ev.type) {
      case "session/start": if (d.system_prompt) wrapEl.appendChild(systemChip(d.system_prompt)); break;
      case "system/message": wrapEl.appendChild(sysNoteNode()); break;
      case "user/message": wrapEl.appendChild(userNode(d.content)); break;
      case "assistant/message":
        if (d.content) wrapEl.appendChild(assistantNode(d.content));
        for (const call of (d.tool_calls || []))
          wrapEl.appendChild(toolCard({ callId: call.id, name: call.name, args: call.arguments }));
        break;
      case "tool/result": {
        const node = wrapEl.querySelector(`[data-call="${cssEsc(d.call_id)}"]`);
        if (node) fillToolCard(node, d.result, d.is_error);
        break;
      }
      case "turn/end": wrapEl.appendChild(turnStatusNode(d.turn, d.status, d.steps ?? 0)); break;
      default: {
        // 历史重放也走同一套渲染自述（插件的日志事件照样不该写死在壳里）
        const node = renderBySpec(ev.type, d);
        if (node) wrapEl.appendChild(node);
        break;
      }
    }
  }
  chatEl.scrollTop = chatEl.scrollHeight;
}
function emptyStateHtml() {
  return `<div class="empty"><div class="empty-ic">◍</div>
    <div class="empty-t">开始一段对话</div>
    <div class="empty-s">这个会话还没有消息。试试：</div>
    <div class="hints">
      <button type="button" class="hint" data-q="工作区里有哪些文件？">工作区里有哪些文件？</button>
      <button type="button" class="hint" data-q="帮我算 1234*56.78">帮我算 1234*56.78</button>
      <button type="button" class="hint" data-q="用 shell 看看当前目录">用 shell 看看当前目录</button>
    </div></div>`;
}

/* ---------------- approvals ---------------- */
function approvalNode(req) {
  const node = el(`<div class="approval" data-apv="${esc(req.id)}">
    <div class="apv-head"><span class="apv-ic">⚠</span>
      <span class="apv-title">需要你的批准</span>
      <span class="apv-tool">${esc(req.tool)}</span></div>
    <div class="apv-reason">${esc(req.reason || "该操作被标记为需要审批")}</div>
    ${req.preview ? `<div class="apv-code"><pre>${esc(req.preview)}</pre></div>` : ""}
    <div class="apv-actions">
      <button type="button" class="apv-approve">批准并执行</button>
      <button type="button" class="apv-deny">拒绝</button>
      <span class="apv-wait">等待你的决定…</span></div></div>`);
  return node;
}
async function answerApproval(node, approved) {
  const id = node.dataset.apv;
  node.querySelector(".apv-actions").remove();
  try {
    await fetch("/api/approvals/" + encodeURIComponent(id), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved }),
    });
    const done = el(`<div class="apv-done ${approved ? "ok" : "no"}">`
      + (approved ? "✓ 已批准，正在执行…" : "✗ 已拒绝（模型会收到拒绝原因）") + "</div>");
    node.appendChild(done);
  } catch (err) {
    node.appendChild(el(`<div class="apv-done no">提交失败：${esc(err.message)}</div>`));
  }
}

/** 补看**正在等待**的审批（`GET /api/approvals`）。
 *
 *  为什么必须有这一步：审批帧（`__approval`）只在"等待开始"那一刻推给当时的
 *  订阅者。刷新页面＝换了一个订阅者，那张卡片就永远不回来了——而 turn 还在
 *  服务端**阻塞等它**（默认 300 秒），到点按 fail-closed 拒绝。于是用户看到的是
 *  "页面刷新后工具莫名失败"。
 *
 *  **按会话区分**（`req.session`）：审批可能属于另一条会话（刷新后页面默认打开
 *  的是最近一条，未必是它）。画在当前会话里会让人以为"这条对话在等我批准"；
 *  而且批准之后也看不到结果——那条会话根本没被订阅。所以：
 *
 *    - 属于当前会话 → 画卡片（并挂上它的流）；
 *    - 属于别的会话 → 顶部横幅提示 + 一键切过去。
 */
async function recoverApprovals() {
  let data;
  try {
    const res = await fetch("/api/approvals", { cache: "no-store" });
    if (!res.ok) return;                      // 没装审批插件：正常，什么都不做
    data = await res.json();
  } catch (err) {
    return;                                   // 拉不到就当没有（不打扰用户）
  }
  const pending = data.pending || [];
  if (!pending.length) return;

  const mine = pending.filter((req) => !req.session || req.session === state.current);
  const others = pending.filter((req) => req.session && req.session !== state.current);

  for (const req of mine) {
    if (wrapEl.querySelector(`[data-apv="${cssEsc(req.id)}"]`)) continue;
    removeWorking();
    wrapEl.appendChild(approvalNode(req));
    setRunning(`等待你的批准：${req.tool}`);
    scrollBottom();
  }
  // 别的会话在等批准：横幅提示可切过去（擅自切会话会让用户莫名其妙）
  const b = $("banner");
  if (others.length) {
    b.innerHTML = `另一条会话正在等待批准（${others.map((r) => esc(r.session)).join("、")}）`
      + `<button type="button" class="bc-btn banner-jump">切过去处理</button>`;
    b.hidden = false;
    // **用 onclick 赋值而不是 addEventListener**：`recoverApprovals` 会被反复调用
    // （每次接回/初始化），addEventListener 会一层层叠加，点一下触发 N 次。
    b.onclick = (event) => {
      if (!event.target.closest(".banner-jump")) return;
      b.hidden = true;
      b.onclick = null;
      openSession(others[0].session, { silent: true });
    };
  }
}

/* ---------------- live frames ---------------- */
function setRunline(html, cls) {
  const line = $("runline");
  line.className = "runline" + (cls ? " " + cls : "");
  line.innerHTML = html;
}
function setIdle(text) { setRunline(`<span>${esc(text)}</span>`); }
function setRunning(text) {
  setRunline(`<span class="spinner"></span><span>${esc(text)}</span>`);
}
function ensureWorking(text) {
  let node = wrapEl.querySelector(".working");
  if (!node) { node = el(`<div class="row working"><span class="dots"><i></i><i></i><i></i></span><span class="wt"></span></div>`);
    wrapEl.appendChild(node); }
  node.querySelector(".wt").textContent = text;
  scrollBottom();
}
function removeWorking() { const n = wrapEl.querySelector(".working"); if (n) n.remove(); }
function scrollBottom() { if (state.follow) chatEl.scrollTop = chatEl.scrollHeight; }

function handleFrame(kind, payload) {
  switch (kind) {
    case "step_request": setRunning(`第 ${payload.step} 步 · 请求模型…`);
      ensureWorking(`第 ${payload.step} 步 · 请求模型（${payload.message_count} 条消息）`); break;
    case "step_response": {
      // 流式模式下：把刚收满的气泡"定稿"（换成正式 Markdown 渲染）；否则新建。
      const live = wrapEl.querySelector(".streaming-live");
      if (live) {
        const cursor = live.parentElement.querySelector(".cursor");
        if (cursor) cursor.remove();
        live.classList.remove("streaming-live");
        live.dataset.raw = "";
        live.innerHTML = payload.content ? md(payload.content) : "";
      } else if (payload.content) {
        wrapEl.appendChild(assistantNode(payload.content));
      }
      for (const c of (payload.tool_calls || []))
        wrapEl.appendChild(toolCard({ callId: c.id, name: c.name, args: c.arguments }));
      if (payload.tool_calls?.length) {
        setRunning(`第 ${payload.step} 步 · 执行工具…`);
        ensureWorking(`执行 ${payload.tool_calls.map((c) => c.name).join("、")}…`);
      }
      scrollBottom(); break;
    }
    case "tool_result": {
      const node = wrapEl.querySelector(`[data-call="${cssEsc(payload.call_id)}"]`);
      if (node) fillToolCard(node, payload.result, payload.is_error);
      scrollBottom(); break;
    }
    case "turn_end":
      removeWorking();
      wrapEl.querySelectorAll(".cursor").forEach((c) => c.remove());
      wrapEl.querySelectorAll(".streaming-live").forEach((n) => n.classList.remove("streaming-live"));
      wrapEl.appendChild(turnStatusNode(payload.turn, payload.status, payload.steps));
      scrollBottom(); break;
    case "step_delta": {
      // 逐段到达：同一 step 内往同一个气泡追加（首个文本增量创建它）
      if (!payload.text) break;
      let live = wrapEl.querySelector(".streaming-live");
      if (!live) {
        removeWorking();
        const node = el(`<div class="row assistant"><div class="prose streaming-live"></div></div>`);
        wrapEl.appendChild(node);
        live = node.querySelector(".streaming-live");
      }
      const cursor = live.parentElement.querySelector(".cursor");
      live.dataset.raw = (live.dataset.raw || "") + payload.text;
      live.textContent = live.dataset.raw;
      if (!cursor) live.after(el(`<span class="cursor"></span>`));
      setRunning(`第 ${payload.step} 步 · 流式接收中…`);
      scrollBottom();
      break;
    }
    case "stream_cancelled": {
      removeWorking();
      const live = wrapEl.querySelector(".streaming-live");
      if (live) {
        const cur = live.parentElement.querySelector(".cursor");
        if (cur) cur.remove();
        live.classList.remove("streaming-live");
      }
      setRunline("已取消（保留已收到的部分）", "err");
      break;
    }
    case "__approval": {
      // 审批卡片是**固定形态**（要按钮、要回传）——它不是"展示"而是"交互"，
      // 所以不走渲染自述，直接在这里画。其余事件一律交给通用渲染器。
      removeWorking();
      wrapEl.appendChild(approvalNode(payload)); scrollBottom();
      setRunline("等待你的审批…"); break;
    }
    case "__repair": showBanner(payload.message || "上次会话被强杀，日志尾部已自动修复"); break;
    case "__error": removeWorking(); setRunline(`出错：${esc(payload.message)}`, "err"); break;
    default: {
      // **插件自述的显示规则**（壳不认识具体事件名）——查得到就画，查不到就算了。
      const node = renderBySpec(kind, payload);
      if (node) { wrapEl.appendChild(node); scrollBottom(); }
      break;
    }
  }
}
function showBanner(text) { const b = $("banner"); b.textContent = text; b.hidden = false; }

/* ---------------- trace panel ---------------- */
let evRows = [];
function evCat(type) { return ({ session: 1, turn: 1, step: 1, user: 1, assistant: 1, tool: 1, system: 1 })[String(type).split("/")[0]] ? String(type).split("/")[0] : "other"; }
function evSummary(type, d) {
  d = d || {};
  switch (type) {
    case "session/start": return "会话建立（system_prompt + 装配单）";
    case "system/message": return clip(String(d.content || "").replace(/\s+/g, " "), 70);
    case "turn/start": return `turn ${d.turn}`;
    case "user/message": return clip(String(d.content || "").replace(/\s+/g, " "), 70);
    case "step/start": return `turn ${d.turn} · step ${d.step} · ${d.message_count} 条消息 / ${d.tool_count} 工具`;
    case "step/end": return `step ${d.step} 结束`;
    case "assistant/message": return d.tool_calls?.length
      ? `申请调用 ${d.tool_calls.map((c) => c.name).join("、")}`
      : clip(String(d.content || "").replace(/\s+/g, " "), 70);
    case "tool/result": return `${d.name} ${d.is_error ? "✗" : "✓"} ${clip(JSON.stringify(d.result), 46)}`;
    case "turn/end": return `${d.status} · ${d.steps} 步`;
    default: {
      // 轨迹栏同样按自述取摘要（模板去掉占位符就是一句话）
      const spec = (state.display || []).find((x) => x.kind === type);
      if (spec && spec.title) return clip(fillTemplate(spec.title, d), 70);
      return clip(JSON.stringify(d), 70);
    }
  }
}
function evRowHtml(i, seq, type, summary, ts) {
  return `<tr class="ev ${evCat(type)}" data-i="${i}" title="${esc(summary)}">`
    + `<td class="ev-seq">${seq ?? "·"}</td>`
    + `<td class="ev-type">${esc(type)}</td>`
    + `<td class="ev-sum">${esc(summary)}</td>`
    + `<td class="ev-time">${ts ? esc(fmtTime(ts)) : ""}</td></tr>`;
}
function renderEventsFromHistory(events) {
  evRows = events.map((e) => ({ seq: e.seq, type: e.type, data: e.data, ts: e.ts }));
  const body = eventsEl.querySelector("tbody");
  body.innerHTML = "";
  let lastTurn = null;
  for (let i = 0; i < evRows.length; i++) {
    const ev = evRows[i];
    if (ev.type === "turn/start" && ev.data?.turn !== lastTurn) {
      lastTurn = ev.data.turn;
      body.insertAdjacentHTML("beforeend", `<tr class="evse"><td colspan="4"><span>turn ${esc(lastTurn)}</span></td></tr>`);
    }
    body.insertAdjacentHTML("beforeend", evRowHtml(i, ev.seq, ev.type, evSummary(ev.type, ev.data), ev.ts));
  }
  $("seq-tag").textContent = evRows.length ? `≤ ${evRows.at(-1).seq}` : "";
  eventsEl.scrollTop = eventsEl.scrollHeight;
}
function appendLiveEvent(kind, payload) {
  const map = { turn_start: "turn/start", step_request: "step/start", step_response: "assistant/message", tool_result: "tool/result", step_end: "step/end", turn_end: "turn/end" };
  const type = map[kind] || kind;
  evRows.push({ seq: null, type, data: payload, ts: null });
  eventsEl.querySelector("tbody").insertAdjacentHTML("beforeend",
    evRowHtml(evRows.length - 1, null, type, evSummary(type, payload), null));
  eventsEl.scrollTop = eventsEl.scrollHeight;
}
eventsEl.addEventListener("click", (e) => {
  const row = e.target.closest("tr.ev"); if (!row) return;
  const next = row.nextElementSibling;
  if (next && next.classList.contains("ev-raw")) { next.remove(); return; }
  const item = evRows[+row.dataset.i]; if (!item) return;
  const data = { ...(item.data || {}) };
  if (data.prompt_trace) data.prompt_trace = "(装配单略)";
  const tr = document.createElement("tr"); tr.className = "ev-raw";
  tr.innerHTML = `<td colspan="4"><pre>${esc(JSON.stringify(data, null, 2))}</pre></td>`;
  row.after(tr);
});

/* ---------------- ledger / sessions ---------------- */
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}
let knownPlugins = null;
/* ---------------- 管理面板（弹出层 + 插件自述的标签页） ----------------

   **壳不认识任何具体管理面**：标签页、取数地址、动作地址模板全部来自插件的
   `AdminSpec` 自述（/api/ledger 的 `admins`），壳只渲染一套**通用形状**：

       groups[].plugins[]  → 卡片（名字 + 状态 + 标签 + 备注 + 按钮）
       installable[]       → 虚线"空位"卡片（还能装的）
       actions{}           → 动作名 → 按钮文字（壳不翻译动作名）

   内置一页**只读的**「插件」（直接读内核台账，不依赖任何插件）；插件声明了同
   key（plugins）的一页就顶掉它——于是没装 plugin-admin 时界面退回只读，
   而不是空白或报错（replace-by-key）。 */

/** 最近一次台账（左栏计数与内置只读页用它；管理页各自的快照在 adminPanel 里）。 */
const ledger = { plugins: [], added: [] };

/** 面板状态：标签页列表、当前标签、每页的快照（各自缓存，切回来不闪）。 */
const adminPanel = { specs: [], current: null, snapshots: {}, stamp: "" };

function ledgerOpen() {
  return !$("plugin-modal").hidden;
}

/** 内置的只读页（没有插件声明 plugins 时的降级）：取数走壳自己的 /api/ledger。 */
const BUILTIN_PLUGINS_TAB = {
  key: "plugins", label: "插件", title: "插件台账", order: 0, builtin: true,
  summary: "插件管理未装载（plugin-admin）——只读展示，不能操作。",
  snapshot: "", action: "",
};

function adminTabs() {
  const specs = adminPanel.specs || [];
  return specs.length ? specs : [BUILTIN_PLUGINS_TAB];
}

function openLedger(tab) {
  $("plugin-modal").hidden = false;
  adminPanel.stamp = "";                     // 重新打开：允许走一次入场动画
  if (tab) adminPanel.current = tab;
  renderTabs();
  renderAdminPanel();
  const box = $("pm-search");
  if (box) box.focus();
}

function closeLedger() {
  $("plugin-modal").hidden = true;
  const box = $("pm-search");
  if (box) box.value = "";                   // 下次打开是干净的全量视图
}

/** 标签栏：按自述画（order 已由后端排好）。 */
function renderTabs() {
  const host = $("pm-tabs");
  if (!host) return;
  const tabs = adminTabs();
  if (!tabs.some((t) => t.key === adminPanel.current)) {
    adminPanel.current = tabs[0].key;         // 当前标签没了（插件被卸）→ 回第一个
  }
  host.innerHTML = tabs.map((t) => `
    <button type="button" class="pm-tab${t.key === adminPanel.current ? " on" : ""}"
            data-tab="${esc(t.key)}">${esc(t.label)}</button>`).join("");
}

/** 面板内的一条回执（顶端横幅被遮罩挡住，所以自己说一句）。 */
let toastTimer = null;
function ledgerToast(text, bad) {
  const el = $("pm-toast");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("bad", !!bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2800);
}

/** 一次管理动作（安装/卸载/停用/启用…）。**动作名与地址都由插件给**。 */
async function pluginAction(name, action, card) {
  const spec = adminTabs().find((t) => t.key === adminPanel.current);
  if (!spec || !spec.action || spec.builtin) {
    ledgerToast("这一页只读：没有可用的管理插件", true);
    return;
  }
  if (card) card.classList.add("busy");
  const url = "/" + spec.action
    .replace("{name}", encodeURIComponent(name))
    .replace("{action}", encodeURIComponent(action));
  try {
    const res = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    ledgerToast(data.message || "完成", !data.ok);
    if (data.ok) setIdle(data.message || "完成");
    if (data.snapshot) {                      // 动作回包里带最新快照：就地更新
      adminPanel.snapshots[spec.key] = data.snapshot;
      adminPanel.stamp = "";
    }
    await loadLedger();                       // 台账/工具面一起刷新
  } catch (err) {
    ledgerToast(`操作失败：${err.message}`, true);
  } finally {
    if (card) card.classList.remove("busy");
  }
}

/** 状态的中文与配色（壳只翻译内核给的状态词，不含任何插件知识）。 */
const STATUS_TEXT = {
  active: "运行中", pending: "等待依赖", disabled: "已停用", failed: "激活失败",
  enabled: "可用",
};

/** 依赖行：必需 / 可选（有则显示，没有就整行不画）。 */
function ledgerDepHtml(p) {
  const bits = [];
  if (p.inject && p.inject.length) bits.push(`<span class="k">必需</span> ${p.inject.map((d) => `<code>${esc(d)}</code>`).join(" ")}`);
  if (p.optional && p.optional.length) bits.push(`<span class="k">可选</span> ${p.optional.map((d) => `<code>${esc(d)}</code>`).join(" ")}`);
  return bits.length ? `<div class="pm-dep">${bits.join(" · ")}</div>` : "";
}

/** 一张卡片（含它的动作按钮）。**标签与备注都是快照下发的通用字段**。 */
function ledgerCard(p, actionLabels, index, readonly) {
  const tags = [];
  if (p.category === "base") tags.push(`<span class="pm-tag">不可卸载</span>`);
  if (p.missing_optionals && p.missing_optionals.length) {
    tags.push(`<span class="pm-tag warn" title="可选依赖缺失：${esc(p.missing_optionals.join("、"))}">降级运行</span>`);
  }
  if (p.error) tags.push(`<span class="pm-tag err" title="${esc(p.error)}">错误</span>`);
  for (const tag of (p.tags || [])) {         // 管理面自己给的标签（如"需审批""已停用"）
    const kind = tag.kind ? ` ${esc(tag.kind)}` : "";
    tags.push(`<span class="pm-tag${kind}" title="${esc(tag.title || "")}">${esc(tag.text)}</span>`);
  }
  const notes = [];
  // 说明只在**它没能正常跑起来**时才写：正常运行的卡片不需要解释自己。
  if (p.blocked_reason && p.status !== "active") notes.push(p.blocked_reason);
  if (p.note) notes.push(p.note);
  if (p.error) notes.push(String(p.error));
  const acts = (readonly ? [] : (p.actions || [])).map((a) => {
    const cls = a === "unload" || a === "disable" ? "pm-btn danger" : "pm-btn go";
    return `<button type="button" class="${cls}" data-name="${esc(p.name)}"
      data-action="${esc(a)}">${esc((actionLabels && actionLabels[a]) || a)}</button>`;
  }).join("");
  const isNew = ledger.added.includes(p.name) ? " is-new" : "";
  return `
  <div class="pm-card${isNew} is-${esc(p.status)}" data-name="${esc(p.name)}" data-cat="${esc(p.category || "other")}"
       data-search="${esc(searchTextOf(p))}"
       style="--i:${index}">
    <div class="pm-top">
      <span class="pm-name">${esc(p.name)}</span>
      <span class="pm-st ${esc(p.status)}" title="${esc(STATUS_TEXT[p.status] || p.status)}">${esc(STATUS_TEXT[p.status] || p.status)}</span>
    </div>
    ${ledgerDepHtml(p)}
    ${tags.length ? `<div class="pm-tags">${tags.join("")}</div>` : ""}
    ${notes.map((n) => `<div class="pm-note">${esc(n)}</div>`).join("")}
    ${acts ? `<div class="pm-acts">${acts}</div>` : ""}
  </div>`;
}

/** 卡片的搜索文本：名字 + 依赖 + 标签 + 备注（筛选框按它匹配）。 */
function searchTextOf(p) {
  return [
    p.name, (p.inject || []).join(" "), (p.optional || []).join(" "),
    (p.tags || []).map((t) => t.text).join(" "), p.note || "",
  ].join(" ").toLowerCase();
}

/** 可安装的白名单项——虚线"空位"卡片，一眼看出还能装什么。 */
function ledgerGhostCard(x, index, actionLabels) {
  const label = (actionLabels && actionLabels.install) || "安装";
  return `
  <div class="pm-card ghost" data-name="${esc(x.name)}" data-cat="install"
       data-search="${esc(x.name.toLowerCase())}" style="--i:${index}">
    <div class="pm-top">
      <span class="pm-name">${esc(x.name)}</span>
      <span class="pm-st" style="color:var(--faint)">未装载</span>
    </div>
    <div class="pm-note">在白名单里，可随时装入</div>
    <div class="pm-acts">
      <button type="button" class="pm-btn go" data-name="${esc(x.name)}"
        data-action="install">${esc(label)}</button>
    </div>
  </div>`;
}

/** 把一页快照画进面板（首次打开、刷新、切标签、筛选都走这里）。
 *
 *  **刷新时保留滚动位置**：面板每 2.5 秒随 loadLedger 刷新，若直接重写 innerHTML
 *  会把用户读到一半的位置顶回顶部。这里先存后恢复；内容没变时调用方根本不调本函数。
 */
function renderAdminPanel() {
  const body = $("pm-body"), sub = $("pm-sub"), foot = $("pm-foot");
  if (!body) return;
  const spec = adminTabs().find((t) => t.key === adminPanel.current);
  if (!spec) return;
  const keepScroll = body.scrollTop;
  const titleEl = $("pm-title");
  if (titleEl) titleEl.textContent = spec.title || spec.label || "管理面板";

  if (spec.builtin) {                         // 只读降级：壳自己的台账
    sub.textContent = `${(ledger.plugins || []).length} 个插件`;
    body.innerHTML = `<div class="pm-ro">${esc(spec.summary || "只读展示")}</div>`
      + renderReadonlyGroups(ledger.plugins || []);
    foot.innerHTML = "只读视图：管理插件未装载";
    applyLedgerFilter();
    body.scrollTop = keepScroll;
    adminPanel.stamp = JSON.stringify({ tab: spec.key, p: ledger.plugins });
    return;
  }

  const snapshot = adminPanel.snapshots[spec.key];
  if (!snapshot) {
    sub.textContent = "";
    body.innerHTML = `<div class="pm-empty">读取中…</div>`;
    foot.innerHTML = "";
    return;
  }

  const counts = snapshot.counts || {};
  const installable = snapshot.installable || [];
  sub.textContent = `${counts.total || 0} 项 · ${counts.active || 0} 可用`;
  if (counts.disabled) sub.textContent += ` · ${counts.disabled} 停用`;
  if (installable.length) sub.textContent += ` · 可安装 ${installable.length}`;

  const labels = snapshot.actions || {};
  let idx = 0;
  const groups = (snapshot.groups || []).filter((g) => (g.plugins || []).length).map((g) => `
    <div class="pm-group" data-cat="${esc(g.category)}">
      <div class="pm-group-h">
        <span class="pm-group-t">${esc(g.label || g.category)}</span>
        <span class="pm-group-n">${(g.plugins || []).length}</span>
        <span class="pm-group-hint">${esc(g.hint || "")}</span>
      </div>
      <div class="pm-grid">
        ${(g.plugins || []).map((p) => ledgerCard(p, labels, idx++)).join("")}
      </div>
    </div>`).join("");

  const ghost = installable.length ? `
    <div class="pm-group" data-cat="install">
      <div class="pm-group-h">
        <span class="pm-group-t">可安装</span>
        <span class="pm-group-n">${installable.length}</span>
        <span class="pm-group-hint">白名单里有、当前没装</span>
      </div>
      <div class="pm-grid">
        ${installable.map((x, i) => ledgerGhostCard(x, idx + i, labels)).join("")}
      </div>
    </div>` : "";

  body.innerHTML = groups + ghost + `<div class="pm-empty pm-none" hidden>没有匹配的项</div>`;
  foot.innerHTML = spec.foot || "人点按钮不需要审批——审批拦的是模型发起的那条路径。";
  applyLedgerFilter();
  body.scrollTop = keepScroll;
  adminPanel.stamp = JSON.stringify({
    tab: spec.key, s: snapshot, a: ledger.added,
  });
}

/** 只读降级的分组渲染（没装任何管理插件时用；同样只按通用字段画）。 */
function renderReadonlyGroups(plugins) {
  const meta = {
    base: ["基础插件", "当前应用的最小集合"],
    capability: ["能力插件", "可按需开关的额外能力"],
    other: ["其他插件", "演示与示例"],
  };
  let idx = 0;
  return ["base", "capability", "other"].map((cat) => {
    const group = plugins.filter((p) => (p.category || "other") === cat);
    if (!group.length) return "";
    return `
    <div class="pm-group" data-cat="${cat}">
      <div class="pm-group-h">
        <span class="pm-group-t">${meta[cat][0]}</span>
        <span class="pm-group-n">${group.length}</span>
        <span class="pm-group-hint">${meta[cat][1]}</span>
      </div>
      <div class="pm-grid">
        ${group.map((p) => ledgerCard(p, null, idx++, true)).join("")}
      </div>
    </div>`;
  }).join("") + `<div class="pm-empty pm-none" hidden>没有匹配的插件</div>`;
}

/** 筛选：按名字/依赖/标签过滤卡片；整组都没匹配就把组头也收起来。 */
function applyLedgerFilter() {
  const box = $("pm-search");
  const term = (box ? box.value : "").trim().toLowerCase();
  const body = $("pm-body");
  if (!body) return;
  let visible = 0;
  for (const group of body.querySelectorAll(".pm-group")) {
    let shown = 0;
    for (const card of group.querySelectorAll(".pm-card")) {
      const hit = !term || (card.dataset.search || "").includes(term);
      card.hidden = !hit;
      if (hit) shown += 1;
    }
    group.hidden = shown === 0;
    visible += shown;
  }
  const none = body.querySelector(".pm-none");
  if (none) none.hidden = visible > 0;
}

/** 取一页的快照（每个管理面各自的地址，由自述给出）。 */
async function fetchAdminSnapshot(spec) {
  const res = await fetch("/" + spec.snapshot, { cache: "no-store" });
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

async function loadLedger() {
  const data = await api("/api/ledger");
  const tools = data.tools || [];
  // 面板自述（插件声明、壳遍历）——这是界面上的"零知识"入口：
  // 插件多一个少一个，这里只是列表长度变了，壳本身不用改。
  panelBar.panels = data.panels || [];
  renderPanelButtons();
  // 管理页自述（`_06`）：管理弹层的标签页由插件声明；**没有声明就退回内置只读页**。
  adminPanel.specs = data.admins || [];
  // **每一轮都重新取快照**（只读、本机 JSON，几十毫秒）。
  //
  // 曾经写成"每个标签页只取一次"（`loaded` 标记把关），后果是：**模型刚装上的
  // 工具永远不出现在管理面板里**——缓存早就填过了，`loadLedger` 每 2.5 秒跑一次
  // 也只会跳过它。实测里刚让模型用 install_tool 装了个 word_count，工具面已经有
  // 16 件，面板却还停在 15 件。
  for (const spec of adminPanel.specs) {
    if (!spec.snapshot) continue;
    try {
      adminPanel.snapshots[spec.key] = await fetchAdminSnapshot(spec);
    } catch (err) { /* 取不到就留着上一次的，下次刷新再试 */ }
  }
  // 显示自述（插件声明事件怎么画）——壳只按 renderer 名分派
  state.display = data.display || [];

  $("tools-badge").innerHTML = `工具 <b>${tools.length}</b>`;
  $("tools-badge").title = tools.join(" · ");

  const plugins = data.plugins || [];
  const names = plugins.map((p) => p.name);
  const added = knownPlugins ? names.filter((n) => !knownPlugins.includes(n)) : [];
  const removed = knownPlugins ? knownPlugins.filter((n) => !names.includes(n)) : [];
  knownPlugins = names;

  ledger.plugins = plugins;
  ledger.added = added;

  // 左栏"门卡"：一行说清状态（活跃数 + 可安装数）
  const active = plugins.filter((p) => p.status === "active").length;
  const pluginSnap = adminPanel.snapshots["plugins"];
  const installable = pluginSnap ? (pluginSnap.installable || []).length : 0;
  $("plugin-count").textContent = `${active}/${plugins.length} 活跃`;
  const go = $("ledger-entry-go");
  if (go) go.textContent = installable ? `管理 · 可装 ${installable} ›` : "管理 ›";

  if (added.length) setIdle(`插件台账更新：新增 ${added.join("、")}`
    + (removed.length ? `；移除 ${removed.join("、")}` : ""));
  // 面板开着就地刷新，但**只有内容真的变了才重画**：
  // 每 2.5 秒无脑重写 innerHTML 会让卡片反复播入场动画、并把滚动位置顶回顶部。
  if (ledgerOpen()) {
    renderTabs();
    const stamp = JSON.stringify({
      tab: adminPanel.current, p: ledger.plugins,
      s: adminPanel.snapshots[adminPanel.current], a: ledger.added,
    });
    if (stamp !== adminPanel.stamp) renderAdminPanel();
  }
}

/* 面板交互：入口按钮 / 关闭 / Esc / 点遮罩 / 切标签 / 筛选 / 动作按钮（事件委托）。 */
$("ledger-entry").addEventListener("click", () => openLedger());
$("pm-close").addEventListener("click", closeLedger);
$("plugin-modal").addEventListener("click", (e) => {
  if (e.target.id === "plugin-modal") closeLedger();     // 点遮罩关闭
});
$("pm-tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".pm-tab");
  if (!tab || tab.dataset.tab === adminPanel.current) return;
  adminPanel.current = tab.dataset.tab;
  adminPanel.stamp = "";
  const box = $("pm-search"); if (box) box.value = "";
  renderTabs();
  renderAdminPanel();
});
$("pm-search").addEventListener("input", applyLedgerFilter);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && ledgerOpen()) closeLedger();
});
$("pm-body").addEventListener("click", (e) => {
  const btn = e.target.closest(".pm-btn");
  if (!btn) return;
  e.preventDefault();
  pluginAction(btn.dataset.name, btn.dataset.action, btn.closest(".pm-card"));
});

async function loadSessions() {
  const data = await api("/api/sessions");
  const items = data.sessions || [];
  sessionsEl.innerHTML = items.length ? items.map((s) => `
    <div class="item ${s.id === state.current ? "active" : ""} ${s.running ? "running" : ""}"
         data-id="${esc(s.id)}">
      <div class="item-t">${s.running ? '<span class="run-dot" title="正在跑 turn"></span>' : ""}${esc(s.id)}</div>
      <div class="item-s">${s.turns} turns · seq ${s.last_seq}${s.running ? " · 运行中" : (s.last_status ? ` · ${esc(s.last_status)}` : "")}</div>
    </div>`).join("") : `<div class="side-empty">还没有会话</div>`;
  state.runningIds = new Set(items.filter((s) => s.running).map((s) => s.id));
}

async function openSession(id, opts = {}) {
  // **切会话先断开上一条流的视图**（服务端的 turn 不受影响，仍在跑）。
  // 不断开的话，上一条会话的帧会继续画进这条会话的聊天区与事件流——
  // 这正是"多个会话同步进行时展示内容混乱"的根因。
  if (activeWatch && activeWatch.session !== id) abortWatch();
  state.current = id;
  if (!opts.silent) setIdle("加载中…");
  const data = await api(`/api/sessions/${encodeURIComponent(id)}/events`);
  renderChatFromEvents(data.events || []);
  renderEventsFromHistory(data.events || []);
  await loadSessions();
  if (!opts.silent) setIdle(`会话 ${id} · ${(data.events || []).length} 条事件`);
  // **接回正在跑的 turn**（`_05` 收尾）：刷新页面/切会话时，服务端那个 turn
  // 从没停过——这里只是重新订阅它，帧继续往页面上流。没有在跑的就 404，静默跳过。
  //
  // 判据只看 `activeWatch`：它非空说明**已经在跟随某条流**（自己刚发的 / 正在
  // 接回的），再挂一个会让同一帧渲染两遍。而上面刚做过"不是这条会话就断开"，
  // 所以此刻还在跟随的一定就是这条会话，直接跳过即可。
  //
  // **别再加 `!state.sending`**：那是"我正握着一次发送"的标志，而 abortWatch
  // 刚把它清掉，于是刚切过来的这条会话明明在跑却不接回——界面一片安静，用户要
  // 手动刷新才看得到（真实报告："切换对话时正在运行标志只有第一个起效"）。
  if (!opts.noWatch && !activeWatch && (state.runningIds || new Set()).has(id)) {
    watchTurn(id, { silent: true });
  }
}

/* ---------------- send ---------------- */
/** 只推给调用方、不显示的控制帧（流的**收尾**）。
 *
 *  为什么要把这些列出来，而不是"凡 __ 开头都算收尾"：`__approval` 也是
 *  `__` 开头，但它是**有人在等你点头**——吞掉它整个 turn 会卡死在等审批上
 *  （实测：模型调 write_file 后页面停在第 2 步"执行工具…"，审批卡片始终
 *  不出现，等 300 秒超时后按 fail-closed 拒绝 → 表现成"工具莫名失败"）。 */
/* 壳**内置**的收尾帧：这两个是壳与循环之间的通用协议（一次 turn/一次流对话的
   结束），不属于任何插件。插件自己的收尾帧由它**自述**（DisplaySpec.terminal）
   ——见 terminalFrames()。 */
const TERMINAL_FRAMES = new Set(["__done", "__error"]);

/** 收尾帧全集 = 壳内置的 + 插件自述的（带 terminal:true 的那些）。
 *  这样"新增一个会返回结果的插件端"不用改壳——插件声明即可。 */
function terminalFrames() {
  const extra = (state.display || []).filter((d) => d.terminal).map((d) => d.kind);
  return new Set([...TERMINAL_FRAMES, ...extra]);
}

/** 逐行解析一条 NDJSON 响应：每帧交给 onFrame，返回**末帧**。
 *
 *  收尾帧（`__done` / `__error` / 插件自述的 terminal）只记不发——它们是协议，
 *  不是内容；其余一律交给 onFrame（包括 `__approval` 这类"要人看见"的帧）。
 *
 *  **本函数不碰界面**：画到哪里由 onFrame（即 `frameSinkFor` 造的那个 sink）决定，
 *  因为它才知道"这些帧属于哪条会话、当前显示的是不是它"。 */
async function readNDJSON(res, onFrame) {
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
  let last = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim(); buf = buf.slice(idx + 1);
      if (!line) continue;
      let frame; try { frame = JSON.parse(line); } catch { continue; }
      if (terminalFrames().has(frame.kind)) {
        last = { kind: frame.kind, payload: frame.payload };
        if (frame.kind === "__error") onFrame(frame.kind, frame.payload);  // 错误要显示
        continue;
      }
      onFrame(frame.kind, frame.payload);
    }
  }
  return last;
}

/** 读一条 NDJSON 流（POST）：逐帧交给 onFrame；返回**末帧**。
 *
 *  `signal` 可中断订阅（AbortController）。**注意它只断"看"，不断 turn**：
 *  turn 跑在服务端后台线程里，断开订阅它照跑完（这正是刷新页面不掉进度的原因）。 */
async function streamFrames(url, body, onFrame, signal) {
  const res = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
    signal,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return readNDJSON(res, onFrame);
}

/** 手动压缩（/compact）：不跑 turn，只折叠历史；结果以帧到达。 */
async function runCompact() {
  if (state.sending) return;
  if (!state.current) state.current = "default";
  const sid = state.current;                 // 同上：切走之后不再画
  const ctl = new AbortController();
  activeWatch = { session: sid, ctl };
  state.sending = true;
  setBusy(true);
  removeWorking(); setRunning("正在压缩上下文…");
  ensureWorking("压缩中（会调用一次模型做摘要）…");
  let last = null;
  try {
    last = await streamFrames(
      `/api/sessions/${encodeURIComponent(sid)}/compact`, {}, frameSinkFor(sid), ctl.signal,
    );
    if (state.current !== sid) return;       // 已经切走：什么都不画
    removeWorking();
    if (last && last.kind === "__error") {
      setRunline(last.payload.message || "压缩失败", "err");
    } else if (last) {
      // **不认帧名**：由插件自述的显示规则决定怎么告诉用户
      // （`status` 渲染器会更新状态栏；查不到就回一句通用的）。
      const node = renderBySpec(last.kind, last.payload);
      if (node) { wrapEl.appendChild(node); scrollBottom(); }
      else if (!(state.display || []).some((d) => d.kind === last.kind)) setIdle("完成");
    } else setIdle("完成");
  } catch (err) {
    if (err.name !== "AbortError" && state.current === sid) {
      removeWorking();
      setRunline(`出错：${esc(err.message)}`, "err");
    }
  } finally {
    if (activeWatch && activeWatch.ctl === ctl) activeWatch = null;
    state.sending = false;
    setBusy(false);
    if (state.current === sid) await openSession(sid, { silent: true, noWatch: true });
  }
}

/* ---------------- 快捷命令（斜杠命令面板） ----------------
   一条命令 = 一行声明：名字 + 说明 + 用法（可选）+ 执行函数。
   输入框里敲 "/" 就弹出全表；继续打字即前缀过滤；↑↓ 选、Enter 或 Tab 补全、
   Esc 关掉。**加新命令只需要往 COMMANDS 里加一行**——提示、过滤、补全、
   执行全自动（这是"输入 / 主动提示"相对"摆一个专属按钮"的价值）。 */
const COMMANDS = [
  {
    name: "/compact",
    desc: "压缩上下文：把最后一轮之前的历史折成一条摘要（跳过 90% 阈值）",
    usage: "/compact",
    run: runCompact,
  },
];

const cmdMenu = $("cmdmenu");
let cmdItems = [];      // 当前可见的候选
let cmdActive = 0;      // 键盘高亮的下标

/** 取当前应该展示的候选（null = 不展示菜单）。 */
function cmdCandidates(value) {
  if (state.sending) return null;
  // 只在"整行是一个还没打完的命令"时提示：以 / 开头且不含空格
  if (!value.startsWith("/") || /\s/.test(value)) return null;
  return COMMANDS.filter((c) => c.name.startsWith(value));
}

function renderCmdMenu() {
  const items = cmdCandidates(ta.value);
  if (!items || !items.length) { hideCmdMenu(); return; }
  cmdItems = items;
  cmdActive = 0;
  cmdMenu.innerHTML = items.map((c, i) => `
    <div class="cmd ${i === 0 ? "on" : ""}" data-i="${i}">
      <span class="cmd-name">${esc(c.name)}</span>
      <span class="cmd-desc">${esc(c.desc)}</span>
      ${c.usage ? `<span class="cmd-hint">Enter 执行</span>` : ""}
    </div>`).join("");
  cmdMenu.hidden = false;
}

function highlightCmd(index) {
  cmdActive = (index + cmdItems.length) % cmdItems.length;
  cmdMenu.querySelectorAll(".cmd").forEach((node, i) =>
    node.classList.toggle("on", i === cmdActive));
}

function hideCmdMenu() { cmdMenu.hidden = true; cmdItems = []; }

/** 执行第 index 条命令（清空输入框、收起菜单、跑 run()）。 */
async function runCommand(index) {
  const command = cmdItems[index];
  if (!command) return;
  hideCmdMenu();
  ta.value = ""; autosize();
  await command.run();
}

/* ---------------- send / 接回 / 中断 ----------------

   turn 跑在**服务端的后台线程**里，HTTP 请求只是订阅者（见 plugins/webui.py）。
   于是三件事成立，前端只需要配合：

     发消息       → POST /send（起 turn 并订阅）。该会话已有 turn 在跑 → 409，
                    此时**问用户"中断还是等"**，不替用户做决定（也不排队：
                    排队会让人以为发出去了）。
     刷新页面     → 打开会话后 GET /turn 接回正在跑的那个（没有就是 404），
                    帧继续往页面上流——**turn 从没被打断过**。
     中断         → POST /interrupt（协作式：正在跑的工具调用要等它返回）。

   "运行中"的可视化有三处：左栏会话列表的标记、状态栏的转圈文案、
   运行中的 turn 自己那张"进行中"卡片。 */

/** 当前正在跟随的流：`{ session, ctl }`（null = 没在跟随）。
 *
 *  **必须带 session**：带上了才知道"这些帧该不该画"。多会话并行时，A 的流在
 *  后台继续跑（服务端不受影响），但用户可能已经切到 B——那时把 A 的帧画进 B 的
 *  聊天区与事件流，就是"展示内容混乱"（真实报告：多个会话同步进行时内容串台）。 */
let activeWatch = null;
/** 发送代次：见 sendMessage 里"中断后重发"的竞态说明。 */
let sendGeneration = 0;

/** 造一个"属于某条会话"的写帧端。
 *
 *  两道闸门都在这里：
 *    1. **只有当前显示的会话是它时**才画（否则整帧丢弃，服务端照常跑）；
 *    2. 事件流（右栏）与聊天区（中栏）**一起**受这个判断管——它们都是"这条会话
 *       的视图"，漏掉一个就会出现"聊天区是 B、右栏却在长 A 的事件"。
 *
 *  被丢弃的帧不会丢内容：所有事件都已经落在会话日志里，切回去时
 *  `openSession` 会按日志重渲染出完整历史。
 */
function frameSinkFor(sessionId) {
  return (kind, payload) => {
    if (state.current !== sessionId) return;      // 不是当前会话：只丢渲染
    handleFrame(kind, payload);
    if (!String(kind).startsWith("__")) appendLiveEvent(kind, payload);
  };
}

/** 断开当前跟随的流（**只断"看"，不断服务端的 turn**）。
 *
 *  切会话时必须调：否则上一条会话的帧会继续往新会话的界面上画。
 *  `activeWatch` 先清空再 abort —— abort 会让那条流的 finally 跑起来，
 *  里面靠 `activeWatch` 判身份，先清掉它就不会去动新会话的界面状态。
 *
 *  同时把 `state.sending` 与忙碌态一并放开：那条流的 finally 因为身份不匹配
 *  不会再碰这些标志，不放的话界面会卡在"发送中"（按钮永远禁用）。
 */
function abortWatch() {
  const watch = activeWatch;
  if (!watch) return;
  activeWatch = null;
  state.sending = false;
  setBusy(false);
  removeWorking();
  try { watch.ctl.abort(); } catch (e) { /* 已结束 */ }
}

/** 中断请求 + 收尾（409 分支与"中断"按钮共用）。 */
async function interruptTurn(sessionId) {
  try {
    const res = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/interrupt`,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" },
    );
    const data = await res.json();
    if (!res.ok) return data.error || `${res.status}`;
    return "";
  } catch (err) {
    return err.message || String(err);
  }
}

/** 订阅某会话正在跑的 turn（GET /turn）：帧照常走 handleFrame。
 *  `silent` = 接回时不刷状态栏（页面刚打开，别和"加载中"抢）。 */
async function watchTurn(sessionId, opts = {}) {
  if (activeWatch) { try { activeWatch.ctl.abort(); } catch (e) { /* 已结束 */ } }
  const ctl = new AbortController();
  activeWatch = { session: sessionId, ctl };
  state.sending = true;
  setBusy(true);
  let last = null;
  let wroteTerminal = false;          // 是否已经按终帧写过状态栏（见 finally）
  try {
    const res = await fetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/turn`,
      { method: "GET", signal: ctl.signal },
    );
    if (res.status === 404) {                // 没有在跑的 turn：正常情形，不是错误
      return;
    }
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    setRunning("接回运行中的 turn…");
    ensureWorking("这个会话还在跑，继续跟随…");
    // **补看正在等待的审批**：审批帧只在"等待开始"那一刻推给当时的订阅者；
    // 刷新之后卡片不会自己回来，而 turn 还在服务端阻塞等它（到点 fail-closed）。
    // 放在订阅**已挂上之后**：这样补看与实时之间没有空窗。
    recoverApprovals();
    last = await readNDJSON(res, frameSinkFor(sessionId));
    if (last && last.kind === "__error") {
      setRunline(last.payload.message || "turn 失败", "err");
      wroteTerminal = true;
    } else if (last) {
      // 收尾了：状态栏必须跟着变。`silent` 只压"就绪"以外的噪音，不能压这个——
      // 否则接回的那条 turn 跑完后，状态栏还停在"第 N 步 · 流式接收中…"骗人
      // （实测：日志里 turn/end 已经 done，界面仍显示在收流）。
      const p = last.payload || {};
      if (last.kind === "__done") {
        setIdle(`turn ${p.turn} · ${p.status} · ${p.steps} 步 · 最终回答已渲染`);
      } else {
        setIdle("完成");
      }
      wroteTerminal = true;
    }
  } catch (err) {
    if (err.name !== "AbortError") setRunline(`接回失败：${esc(err.message)}`, "err");
  } finally {
    // **身份判定用 ctl**（而不是"activeWatch 还在不在"）：被 abortWatch 换掉的
    // 那条流不该再动界面——它属于上一条会话，用户已经不在看它了。
    if (activeWatch && activeWatch.ctl === ctl) {
      activeWatch = null;
      state.sending = false;
      setBusy(false);
      removeWorking();
      // 只在"还在看这条会话"时才重渲染与写状态栏（否则会盖掉新会话的界面）
      if (state.current === sessionId) {
        await openSession(sessionId, { silent: true, noWatch: true });
        if (!wroteTerminal && !$("runline").classList.contains("err")) setIdle("就绪");
      }
    }
  }
}

/** 真正发一条消息（发出去 → 跟随流 → 收尾渲染）。
 *
 *  与 `$("composer")` 的 submit 分开：submit 负责"该不该发"（命令 / 忙不忙），
 *  本函数只负责"发"。这样"中断并重新发送"可以直接调它，不会又被忙碌判断拦一次
 *  （踩过：重发走了 submit，而那条订阅流的 finally 刚好把闸门关回去）。
 *
 *  `retryBusy`：中断后的重发用它。**取消是协作式的**——正在跑的工具调用要等它
 *  返回（可能几秒），这期间服务端仍会回 409。重发时不该把用户又弹回"中断/等"
 *  的选择条（他刚选完），所以这里等它真的让开为止（最多约 15 秒）。
 */
async function sendMessage(text, { retryBusy = false } = {}) {
  if (!state.current) state.current = "default";
  // **这条流属于哪条会话**：下面所有的渲染与状态栏更新都要拿它对照。
  // 用户切走之后，这条流仍在后台读（服务端 turn 不受影响），但**一个像素都不许
  // 再画**——否则 A 的流式文本会长进 B 的聊天区（真实报告的多会话串台）。
  const sid = state.current;
  // 「第几次发送」：中断后重发时，旧那条流的收尾 finally 可能还没跑完。
  // 它如果照常去 setIdle / 重渲染，就会盖掉新 turn 的状态（踩过：
  // 重发成功但状态栏停在"已中断"）。所以收尾前先核对代次。
  const gen = ++sendGeneration;
  const ctl = new AbortController();
  activeWatch = { session: sid, ctl };
  state.sending = true;
  setBusy(true);
  ta.value = ""; autosize();
  if (wrapEl.querySelector(".empty")) wrapEl.innerHTML = "";
  wrapEl.appendChild(userNode(text)); scrollBottom();      // 即时反馈（完成后会用日志重渲染）
  wrapEl.querySelectorAll(".cursor").forEach((c) => c.remove());
  setRunning("运行中…");
  ensureWorking("思考中…");
  let done = null;
  const attempts = retryBusy ? 30 : 1;
  try {
    for (let attempt = 0; attempt < attempts; attempt++) {
      try {
        const last = await streamFrames(
          `/api/sessions/${encodeURIComponent(sid)}/send`, { text },
          frameSinkFor(sid), ctl.signal,
        );
        if (last && last.kind === "__done") done = last.payload;
        break;
      } catch (err) {
        if (err.name === "AbortError") break;      // 切走断流：正常收尾，不报错
        const busy = String(err.message || "").includes("409");
        if (busy && attempt + 1 < attempts) {
          // 上一个 turn 还在收尾（协作式取消要等工具返回）：等一会儿再试
          if (attempt === 0) setRunning("等上一个 turn 收尾…");
          await new Promise((r) => setTimeout(r, 500));
          continue;
        }
        if (busy) {                       // 老规矩：让用户来选（中断 / 等）
          removeWorking();
          if (gen === sendGeneration) { state.sending = false; setBusy(false); }
          showBusyChoice(text);
          return;
        }
        removeWorking();
        setRunline(`出错：${esc(err.message)}`, "err");
        break;
      }
    }
  } finally {
    if (gen === sendGeneration) {          // 还是"我这一发"：正常收尾
      state.sending = false;
      setBusy(false);
    }
    if (activeWatch && activeWatch.ctl === ctl) activeWatch = null;
    // **只在还看着这条会话时才重渲染与写状态栏**（切走了就别动新会话的界面）
    if (state.current === sid) {
      await openSession(sid, { silent: true, noWatch: true });
      if (gen === sendGeneration) {
        if (done) setIdle(`turn ${done.turn} · ${done.status} · ${done.steps} 步 · 最终回答已渲染`);
        else if (!$("runline").classList.contains("err")) setIdle("就绪");
      }
    }
  }
}

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = ta.value.trim();
  if (!text) return;
  // 菜单开着且正好是某条完整命令 → 走命令，别当普通消息发出去
  const exact = COMMANDS.find((c) => c.name === text);
  if (exact && !cmdMenu.hidden) {
    hideCmdMenu();
    ta.value = ""; autosize();
    await exact.run();
    return;
  }
  // **正在跑 turn**：不静默吞掉，也不排队——把选择交给用户
  //（中断重发 / 等它跑完）。用户明确要求"运行中发送新对话应该先选择中断"。
  if (state.sending) { showBusyChoice(text); return; }
  await sendMessage(text);
});

/** 「这条会话正在跑」时给用户的选择：中断它 / 等它跑完（不替用户决定）。
 *
 *  元素在 index.html 里（壳的家具，与 #banner 同类）——这里只管填字与显隐。 */
function showBusyChoice(pendingText) {
  const node = $("busy-choice");
  node.dataset.text = pendingText;
  node.hidden = false;
}

$("busy-choice").addEventListener("click", async (e) => {
  const btn = e.target.closest(".bc-btn");
  if (!btn) return;
  const node = $("busy-choice");
  const act = btn.dataset.act;
  node.hidden = true;
  if (act === "dismiss") return;
  if (act === "wait") { await watchTurn(state.current); return; }
  // 中断可能"扑空"：点下去的那一刻 turn 刚好跑完了（模型回答快时很常见）。
  // 那不是失败——用户想要的是"让我的消息发出去"，所以照样重发。
  const problem = await interruptTurn(state.current);
  const missed = problem.includes("没有在跑的 turn");
  if (problem && !missed) { setRunline(`中断失败：${esc(problem)}`, "err"); return; }
  if (!missed) setRunning("正在中断…");
  // 等它真的收尾（协作式：执行中的工具要跑完），最长 20 秒
  for (let i = 0; i < 40; i++) {
    if (missed && i > 0) break;
    await new Promise((r) => setTimeout(r, 500));
    const data = await api("/api/sessions");
    const item = (data.sessions || []).find((s) => s.id === state.current);
    if (!item || !item.running) break;
  }
  await openSession(state.current, { silent: true, noWatch: true });
  // turn 确实已经收尾（上面轮询确认过），把"正在发"的闸门放开——
  // 客户端那条订阅流的 finally 可能还没跑到，不放开会把重发吞掉。
  state.sending = false;
  setBusy(false);
  const text = node.dataset.text || "";
  if (!text) { setIdle("已中断"); return; }
  setIdle("已中断，重新发送…");
  ta.value = "";
  await sendMessage(text, { retryBusy: true });   // 直接发；等上一个 turn 让开再发
});

/* ---------------- composer + interactions ---------------- */
const ta = $("text");
function autosize() { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 180) + "px"; }

/** 忙闲开关：发送键禁用 + 命令菜单收起（跑 turn / 压缩期间不接受新输入）。 */
function setBusy(busy) {
  $("send").disabled = busy;
  if (busy) hideCmdMenu();
}

ta.addEventListener("input", () => { autosize(); renderCmdMenu(); });
ta.addEventListener("keydown", (e) => {
  const menuOpen = !cmdMenu.hidden && cmdItems.length > 0;
  if (menuOpen) {
    if (e.key === "ArrowDown") { e.preventDefault(); highlightCmd(cmdActive + 1); return; }
    if (e.key === "ArrowUp") { e.preventDefault(); highlightCmd(cmdActive - 1); return; }
    if (e.key === "Tab" || e.key === "Enter") {
      e.preventDefault();
      if (e.key === "Tab") {                      // Tab = 补全到输入框（还能改）
        ta.value = cmdItems[cmdActive].name;
        autosize(); renderCmdMenu();
        return;
      }
      runCommand(cmdActive);                      // Enter = 直接执行
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); hideCmdMenu(); return; }
  }
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
});
ta.addEventListener("blur", () => setTimeout(hideCmdMenu, 120));   // 迟一点：别抢点击
cmdMenu.addEventListener("mousedown", (e) => {                     // mousedown 早于 blur
  const row = e.target.closest(".cmd");
  if (!row) return;
  e.preventDefault();
  runCommand(Number(row.dataset.i));
});
cmdMenu.addEventListener("mousemove", (e) => {
  const row = e.target.closest(".cmd");
  if (row) highlightCmd(Number(row.dataset.i));
});
chatEl.addEventListener("scroll", () => {
  state.follow = chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 90;
});
chatEl.addEventListener("click", async (e) => {
  const hint = e.target.closest(".hint");
  if (hint) { ta.value = hint.dataset.q; autosize(); ta.focus(); return; }
  const apvOk = e.target.closest(".apv-approve");
  if (apvOk) { answerApproval(apvOk.closest(".approval"), true); return; }
  const apvNo = e.target.closest(".apv-deny");
  if (apvNo) { answerApproval(apvNo.closest(".approval"), false); return; }
  const pv = e.target.closest(".preview-btn");
  if (pv) {
    const id = pv.dataset.pre;
    const box = document.getElementById(`pv-${id}`);
    const pre = document.getElementById(id);
    if (!box || !pre) return;
    if (box.hidden) {
      if (!box.dataset.ready) {           // 惰性注入：第一次切换时才构造 iframe
        const frame = document.createElement("iframe");
        // 只给 allow-scripts（不给 allow-same-origin）：脚本能在里面跑，
        // 但拿不到父页面的 DOM / cookie —— 渲染模型给的 HTML 的安全底线。
        frame.setAttribute("sandbox", "allow-scripts");
        frame.className = "preview-frame";
        frame.srcdoc = previewSrc(id);
        box.appendChild(frame);
        box.dataset.ready = "1";
      }
      box.hidden = false; pre.hidden = true; pv.textContent = "源码";
    } else {
      box.hidden = true; pre.hidden = false; pv.textContent = "预览";
    }
    return;
  }
  const btn = e.target.closest(".copy");
  if (btn) {
    const pre = document.getElementById(btn.dataset.pre);
    if (!pre) return;
    try { await navigator.clipboard.writeText(pre.textContent);
      btn.textContent = "已复制"; setTimeout(() => (btn.textContent = "复制"), 1200);
    } catch { btn.textContent = "复制失败"; }
  }
});
sessionsEl.addEventListener("click", (e) => {
  const item = e.target.closest(".item");
  if (item) openSession(item.dataset.id);
});
$("new-session").addEventListener("click", () => {
  openSession("web-" + Date.now().toString(36));
});
$("toggle-left").addEventListener("click", () => {
  document.body.classList.toggle("no-left");
  $("toggle-left").classList.toggle("on", !document.body.classList.contains("no-left"));
});
$("toggle-right").addEventListener("click", () => {
  document.body.classList.toggle("no-right");
  $("toggle-right").classList.toggle("on", !document.body.classList.contains("no-right"));
});

/* ---------------- 插件面板：壳零知识 ----------------

   这里是"完全插件化"在界面上的落点：**壳不认识任何插件**。

   机制：插件在自己那个服务对象上声明 `panels = (PanelSpec(...),)`（见
   components/shell/panel.py）→ webui 的 /api/ledger 把它连同服务地址一起交给
   页面 → 本段遍历这个列表，为每个面板生成一个顶栏按钮 + 一个 iframe。

   于是"加一个面板插件"**不用改这个文件、也不用改 index.html**：插件自己
   声明按钮叫什么、页面在哪、徽标读哪个 JSON 字段。壳只做三件通用的事——
   放按钮、点开显示那个插件的页面、按声明读个数字贴到按钮上。 */

/** 按点号路径取嵌套字段（"totals.total_tokens"）；取不到返回 undefined。 */
function pickField(data, path) {
  if (!path) return undefined;
  return String(path).split(".").reduce(
    (acc, key) => (acc == null ? undefined : acc[key]), data);
}

/** 按插件自述格式出一个徽标数字。 */
function badgeText(panel, data) {
  const value = pickField(data, panel.badge_field);
  if (value == null) return panel.label;
  const number = Number(value);
  if (!Number.isFinite(number)) return panel.label;
  if (panel.badge_kind === "percent") {
    // 窗口大到 1M 之后百分比常常是 0.x——取整会一直显示 0%，
    // 所以小于 10 时保留一位（这种"显示策略"是壳的事，不是插件的事）。
    const shown = number < 10 ? number.toFixed(1) : number.toFixed(0);
    return `${panel.label} ${shown}${panel.badge_suffix || "%"}`;
  }
  return `${panel.label} ${number.toLocaleString("zh-CN")}${panel.badge_suffix || ""}`;
}

const panelBar = {
  panels: [],          // 最近一次 ledger 里的面板自述
  base: new Map(),     // service → 解析后的地址
  badge: new Map(),    // service → 最近一次徽标数据
  current: null,       // 当前打开的面板（service 名）
};

function panelBase(panel) {
  // 地址解析顺序：URL 参数（手工指定）→ 全局变量 → 服务自报 → 自述里没有兜底
  const fromQuery = new URLSearchParams(location.search).get(
    panel.service.replace(/-/g, "_"));
  const fromGlobal = window[panel.service.toUpperCase().replace(/-/g, "_") + "_URL"];
  let base = String(fromQuery || fromGlobal || panel.url || "");
  if (!base) return "";
  if (!/^https?:\/\//i.test(base)) base = "http://" + base;
  return base.endsWith("/") ? base : base + "/";
}

/** 点开某个面板：填标题/地址、装上 iframe（惰性）、刷新徽标。 */
async function openPanel(service) {
  const panel = panelBar.panels.find((p) => p.service === service);
  if (!panel) return;
  const box = document.getElementById("plugin-panel");
  const frame = document.getElementById("pp-frame");
  const base = panelBase(panel);
  panelBar.current = service;
  document.getElementById("pp-title").textContent = panel.title || panel.label;
  const urlEl = document.getElementById("pp-url");
  urlEl.textContent = base;
  urlEl.title = base;
  document.getElementById("pp-open").href = base;
  if (base && frame.dataset.for !== service) {     // 换面板才重装 iframe
    frame.src = base;
    frame.dataset.for = service;
  }
  box.hidden = false;
  refreshPanelBadges();
}

function closePanel() {
  document.getElementById("plugin-panel").hidden = true;
  panelBar.current = null;
}

/** 刷新所有面板按钮上的徽标（跨源读插件自己的 JSON，靠它的 CORS 头）。 */
async function refreshPanelBadges() {
  for (const panel of panelBar.panels) {
    const btn = document.querySelector(`[data-panel="${CSS.escape(panel.service)}"]`);
    if (!btn) continue;
    const base = panelBase(panel);
    if (!panel.badge_path) { btn.textContent = panel.label; continue; }
    try {
      const res = await fetch(base + panel.badge_path, { cache: "no-store" });
      const data = await res.json();
      btn.textContent = badgeText(panel, data);
      btn.title = `${panel.title || panel.label}（${panel.service} 插件） · ${base}`;
    } catch (err) {
      btn.textContent = panel.badge_fallback || panel.label;
      btn.title = `读不到 ${base}${panel.badge_path}（插件未装载？点击仍可打开页面）`;
    }
  }
}

/** 按面板自述建/更新顶栏按钮（ledger 每次刷新都会调）。 */
function renderPanelButtons() {
  const host = document.getElementById("panel-btns");
  if (!host) return;
  host.innerHTML = "";
  for (const panel of panelBar.panels) {
    const btn = document.createElement("button");
    btn.className = "tbtn";
    btn.type = "button";
    btn.dataset.panel = panel.service;
    btn.textContent = panel.badge_fallback || panel.label;
    btn.title = `${panel.title || panel.label}（${panel.service} 插件）`;
    btn.addEventListener("click", () => {
      if (panelBar.current === panel.service
          && !document.getElementById("plugin-panel").hidden) closePanel();
      else openPanel(panel.service);
    });
    host.appendChild(btn);
  }
  refreshPanelBadges();
}

document.getElementById("pp-close").addEventListener("click", closePanel);
document.getElementById("plugin-panel").addEventListener("click", (e) => {
  if (e.target.id === "plugin-panel") closePanel();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
setInterval(refreshPanelBadges, 5000);

/* ---------------- init ---------------- */
(async function init() {
  $("host-badge").textContent = location.host;
  try {
    await Promise.all([loadLedger(), loadSessions()]);
    // 默认打开最近一个会话；没有就停在空态
    const first = sessionsEl.querySelector(".item");
    if (first) await openSession(first.dataset.id, { silent: true });
    else setIdle("就绪 · 点「＋ 新会话」开始");
    // 页面一打开就补看一次待审批：如果刷新发生在"等审批"那一刻，卡片要能回来
    //（正常路径是 watchTurn 里补看，但那条只在会话仍在跑时走；这里兜住"没有
    //  正在跑的 turn、但服务端还挂着待批"的边角——例如 turn 刚收尾）。
    await recoverApprovals();
  } catch (err) {
    setRunline(`初始化失败：${esc(err.message)}`, "err");
  }
  setInterval(loadSessions, 5000);
  setInterval(loadLedger, 2500);
})();


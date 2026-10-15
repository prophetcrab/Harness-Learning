"use strict";
/* ---------------- utils ---------------- */
const $ = (id) => document.getElementById(id);
const chatEl = $("chat"), wrapEl = chatEl.querySelector(".wrap");
const sessionsEl = $("sessions"), pluginsEl = $("plugins"), eventsEl = $("events");
const state = { current: null, sending: false, follow: true };
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
/* 压缩标记（`_05`）：日志里一条 compact/checkpoint，界面上一个折叠徽标。
   点开能看到摘要全文——"模型眼前的历史被折成了什么"是可见的。 */
function compactNode(d) {
  const before = fmtTokens(d.before_tokens), after = fmtTokens(d.after_tokens);
  const node = el(`<div class="row compact"><details class="cmpchip">`
    + `<summary>⟳ 上下文已压缩：折叠 ${esc(d.folded ?? "?")} 条消息`
    + (before && after ? `（约 ${before} → ${after} tokens）` : "")
    + `</summary><pre>${esc(d.summary || "")}</pre></details></div>`);
  return node;
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
      case "compact/checkpoint": wrapEl.appendChild(compactNode(d)); break;
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
    case "compact_applied": {
      // 压缩发生（`_05`）：聊天流里插一个折叠徽标，runline 说明一句。
      wrapEl.appendChild(compactNode(payload));
      const how = payload.trigger === "manual" ? "手动压缩" : "上下文已压缩";
      setRunline(`${how}：折叠 ${payload.folded} 条 → 约 ${fmtTokens(payload.after_tokens) || "?"} tokens`);
      scrollBottom();
      break;
    }
    case "__compact_noop": {
      // 收尾帧由 runCompact 统一处理（这里兜底：事件流里也留个痕）
      showBanner(payload.message || "没有可压缩的历史");
      break;
    }
    case "compact_failed": {
      // 失败是瞬态的：只提示一句，对话照常继续（fail-soft）
      showBanner("上下文压缩未成功（已跳过本次）：" + (payload.error || "未知原因"));
      break;
    }
    case "__approval": {
      removeWorking();
      const node = approvalNode(payload);
      wrapEl.appendChild(node); scrollBottom();
      setRunline("等待你的审批…"); break;
    }
    case "__repair": showBanner(payload.message || "上次会话被强杀，日志尾部已自动修复"); break;
    case "__error": removeWorking(); setRunline(`出错：${esc(payload.message)}`, "err"); break;
    default: break;
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
    case "compact/checkpoint": return `折叠 ${d.folded ?? "?"} 条 → 摘要 ${clip(String(d.summary || "").replace(/\s+/g, " "), 46)}`;
    default: return clip(JSON.stringify(d), 70);
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
async function loadLedger() {
  const data = await api("/api/ledger");
  const tools = data.tools || [], plugins = data.plugins || [];
  // 面板自述（插件声明、壳遍历）——这是界面上的"零知识"入口：
  // 插件多一个少一个，这里只是列表长度变了，壳本身不用改。
  panelBar.panels = data.panels || [];
  renderPanelButtons();
  // 插件集合变化 → 高亮新增的那一行（"装完立刻出现在左栏"的视觉反馈）
  const names = plugins.map((p) => p.name);
  const added = knownPlugins ? names.filter((n) => !knownPlugins.includes(n)) : [];
  const removed = knownPlugins ? knownPlugins.filter((n) => !names.includes(n)) : [];
  knownPlugins = names;
  $("tools-badge").innerHTML = `工具 <b>${tools.length}</b>`;
  $("tools-badge").title = tools.join(" · ");
  pluginsEl.innerHTML = plugins.map((p) => {
    // 可选依赖缺了 → 标"降级"（插件还活着，只是暂时用不了那几样服务）
    const missingOpt = p.missing_optionals || [];
    const deps = [
      `必需：${p.inject.length ? p.inject.join(", ") : "无"}`,
      (p.optional || []).length ? `可选：${p.optional.join(", ")}` : "",
      missingOpt.length ? `当前缺：${missingOpt.join(", ")}` : "",
    ].filter(Boolean).join(" · ");
    return `
    <div class="plug ${added.includes(p.name) ? "plug-new" : ""}" title="依赖：${esc(deps)}">
      <span class="dot ${esc(p.status)}${added.includes(p.name) ? " new" : ""}"></span>
      <span class="plug-n">${esc(p.name)}</span>
      ${missingOpt.length ? `<span class="plug-degraded" title="可选依赖缺失：${esc(missingOpt.join(", "))}">降级</span>` : ""}
      ${p.inject.length ? `<span class="plug-i mono">← ${esc(p.inject.join(","))}</span>` : ""}
    </div>`;
  }).join("") || `<div class="side-empty">（无）</div>`;
  if (added.length) setIdle(`插件台账更新：新增 ${added.join("、")}`
    + (removed.length ? `；移除 ${removed.join("、")}` : ""));
  $("plugin-count").textContent =
    `${plugins.filter((p) => p.status === "active").length}/${plugins.length} 活跃`;
}
async function loadSessions() {
  const data = await api("/api/sessions");
  const items = data.sessions || [];
  sessionsEl.innerHTML = items.length ? items.map((s) => `
    <div class="item ${s.id === state.current ? "active" : ""}" data-id="${esc(s.id)}">
      <div class="item-t">${esc(s.id)}</div>
      <div class="item-s">${s.turns} turns · seq ${s.last_seq}${s.last_status ? ` · ${esc(s.last_status)}` : ""}</div>
    </div>`).join("") : `<div class="side-empty">还没有会话</div>`;
}
async function openSession(id, opts = {}) {
  state.current = id;
  if (!opts.silent) setIdle("加载中…");
  const data = await api(`/api/sessions/${encodeURIComponent(id)}/events`);
  renderChatFromEvents(data.events || []);
  renderEventsFromHistory(data.events || []);
  await loadSessions();
  if (!opts.silent) setIdle(`会话 ${id} · ${(data.events || []).length} 条事件`);
}

/* ---------------- send ---------------- */
/** 只推给调用方、不显示的控制帧（流的**收尾**）。
 *
 *  为什么要把这些列出来，而不是"凡 __ 开头都算收尾"：`__approval` 也是
 *  `__` 开头，但它是**有人在等你点头**——吞掉它整个 turn 会卡死在等审批上
 *  （实测：模型调 write_file 后页面停在第 2 步"执行工具…"，审批卡片始终
 *  不出现，等 300 秒超时后按 fail-closed 拒绝 → 表现成"工具莫名失败"）。 */
const TERMINAL_FRAMES = new Set(["__done", "__error", "__compact_done", "__compact_noop"]);

/** 读一条 NDJSON 流：逐帧交给 onFrame；返回末帧（__done / __compact_done …）。 */
async function streamFrames(url, body, onFrame) {
  const res = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
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
      if (TERMINAL_FRAMES.has(frame.kind)) {
        last = { kind: frame.kind, payload: frame.payload };
        if (frame.kind === "__error") onFrame(frame.kind, frame.payload);  // 错误要显示
        continue;
      }
      // 其余一律当事件显示——包括 `__approval` / `__repair` 这类"要人看见"的帧。
      onFrame(frame.kind, frame.payload);
      if (!String(frame.kind).startsWith("__")) appendLiveEvent(frame.kind, frame.payload);
    }
  }
  return last;
}

/** 手动压缩（/compact）：不跑 turn，只折叠历史；结果以帧到达。 */
async function runCompact() {
  if (state.sending) return;
  if (!state.current) state.current = "default";
  state.sending = true;
  setBusy(true);
  removeWorking(); setRunning("正在压缩上下文…");
  ensureWorking("压缩中（会调用一次模型做摘要）…");
  let last = null;
  try {
    last = await streamFrames(`/api/sessions/${encodeURIComponent(state.current)}/compact`, {}, handleFrame);
    removeWorking();
    if (last && last.kind === "__error") setRunline(last.payload.message || "压缩失败", "err");
    else if (last && last.kind === "__compact_noop") setIdle("没有可压缩的历史（只有当前一轮）");
    else if (last && last.kind === "__compact_done") {
      const before = fmtTokens(last.payload.before_tokens), after = fmtTokens(last.payload.after_tokens);
      setIdle(`✓ 压缩完成：折叠 ${last.payload.folded} 条`
        + (before && after ? `（约 ${before} → ${after} tokens）` : ""));
    } else setIdle("压缩完成");
  } catch (err) {
    removeWorking();
    setRunline(`出错：${esc(err.message)}`, "err");
  } finally {
    state.sending = false;
    setBusy(false);
    await openSession(state.current, { silent: true });
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

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = ta.value.trim();
  if (!text || state.sending) return;
  // 菜单开着且正好是某条完整命令 → 走命令，别当普通消息发出去
  const exact = COMMANDS.find((c) => c.name === text);
  if (exact && !cmdMenu.hidden) {
    hideCmdMenu();
    ta.value = ""; autosize();
    await exact.run();
    return;
  }
  if (!state.current) state.current = "default";
  state.sending = true;
  setBusy(true);
  ta.value = ""; autosize();
  if (wrapEl.querySelector(".empty")) wrapEl.innerHTML = "";
  wrapEl.appendChild(userNode(text)); scrollBottom();      // 即时反馈（完成后会用日志重渲染）
  wrapEl.querySelectorAll(".cursor").forEach((c) => c.remove());
  setRunning("运行中…");
  ensureWorking("思考中…");
  let done = null;
  try {
    const last = await streamFrames(
      `/api/sessions/${encodeURIComponent(state.current)}/send`, { text }, handleFrame,
    );
    if (last && last.kind === "__done") done = last.payload;
  } catch (err) {
    removeWorking();
    setRunline(`出错：${esc(err.message)}`, "err");
  } finally {
    state.sending = false;
    setBusy(false);
    await openSession(state.current, { silent: true });
    if (done) setIdle(`turn ${done.turn} · ${done.status} · ${done.steps} 步 · 最终回答已渲染`);
    else if (!$("runline").classList.contains("err")) setIdle("就绪");
  }
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
   components/panel/spec.py）→ webui 的 /api/ledger 把它连同服务地址一起交给
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
  } catch (err) {
    setRunline(`初始化失败：${esc(err.message)}`, "err");
  }
  setInterval(loadSessions, 5000);
  setInterval(loadLedger, 2500);
})();


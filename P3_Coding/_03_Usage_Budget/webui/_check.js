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
  // 插件集合变化 → 高亮新增的那一行（"装完立刻出现在左栏"的视觉反馈）
  const names = plugins.map((p) => p.name);
  const added = knownPlugins ? names.filter((n) => !knownPlugins.includes(n)) : [];
  const removed = knownPlugins ? knownPlugins.filter((n) => !names.includes(n)) : [];
  knownPlugins = names;
  $("tools-badge").innerHTML = `工具 <b>${tools.length}</b>`;
  $("tools-badge").title = tools.join(" · ");
  pluginsEl.innerHTML = plugins.map((p) => `
    <div class="plug ${added.includes(p.name) ? "plug-new" : ""}"
         title="依赖：${p.inject.length ? esc(p.inject.join(", ")) : "无"}">
      <span class="dot ${esc(p.status)}${added.includes(p.name) ? " new" : ""}"></span>
      <span class="plug-n">${esc(p.name)}</span>
      ${p.inject.length ? `<span class="plug-i mono">← ${esc(p.inject.join(","))}</span>` : ""}
    </div>`).join("") || `<div class="side-empty">（无）</div>`;
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
$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const ta = $("text");
  const text = ta.value.trim();
  if (!text || state.sending) return;
  if (!state.current) state.current = "default";
  state.sending = true; $("send").disabled = true;
  ta.value = ""; autosize();
  if (wrapEl.querySelector(".empty")) wrapEl.innerHTML = "";
  wrapEl.appendChild(userNode(text)); scrollBottom();      // 即时反馈（完成后会用日志重渲染）
  wrapEl.querySelectorAll(".cursor").forEach((c) => c.remove());
  setRunning("运行中…");
  ensureWorking("思考中…");
  let done = null;
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(state.current)}/send`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done: rdone } = await reader.read();
      if (rdone) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim(); buf = buf.slice(idx + 1);
        if (!line) continue;
        let frame; try { frame = JSON.parse(line); } catch { continue; }
        if (frame.kind === "__done") done = frame.payload; else handleFrame(frame.kind, frame.payload);
        if (frame.kind !== "__done") appendLiveEvent(frame.kind, frame.payload);
      }
    }
  } catch (err) {
    removeWorking();
    setRunline(`出错：${esc(err.message)}`, "err");
  } finally {
    state.sending = false; $("send").disabled = false;
    await openSession(state.current, { silent: true });
    if (done) setIdle(`turn ${done.turn} · ${done.status} · ${done.steps} 步 · 最终回答已渲染`);
    else if (!$("runline").classList.contains("err")) setIdle("就绪");
  }
});

/* ---------------- composer + interactions ---------------- */
const ta = $("text");
function autosize() { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 180) + "px"; }
ta.addEventListener("input", autosize);
ta.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); }
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

/* ---------------- token 用量：token-usage 插件的挂载点 ----------------
   页面不需要认识任何插件：地址按这个顺序解析——
     ① URL 参数 ?token_usage=http://host:port/（手工指定，最优先）
     ② window.TOKEN_USAGE_URL（部署时注入）
     ③ /api/ledger 的 endpoints["token-usage"]（服务自报地址，通用发现）
     ④ 默认 http://127.0.0.1:8767/（token-usage 插件的默认端口）
   按钮上的数字 = 该服务 /api/usage 的合计 tokens（跨源读，靠服务端 CORS 头）；
   点开 = 同页 iframe 面板，不打断对话（Esc / 点空白 / 「关闭」收起）。      */
(function () {
  const btn = document.getElementById("token-btn");
  const panel = document.getElementById("token-panel");
  const frame = document.getElementById("token-frame");
  const urlEl = document.getElementById("token-url");
  const openEl = document.getElementById("token-open");
  const closeEl = document.getElementById("token-close");
  if (!btn || !panel || !frame) return;

  const fromQuery = new URLSearchParams(location.search).get("token_usage");
  let base = String(fromQuery || window.TOKEN_USAGE_URL || "http://127.0.0.1:8767/");
  if (!/^https?:\/\//i.test(base)) base = "http://" + base;
  if (!base.endsWith("/")) base += "/";

  function setUrl(url) {   // 只接受 http(s)：不给 javascript: 之类留任何口子
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return;
    base = url.endsWith("/") ? url : url + "/";
    urlEl.textContent = base;
    urlEl.title = base;
    openEl.href = base;
    frame.dataset.ready = "";
    if (!panel.hidden) { frame.src = base; frame.dataset.ready = "1"; }
  }

  async function discover() {   // ③ 服务自报地址（需要 webui 的 /api/ledger 带 endpoints）
    try {
      const res = await fetch("/api/ledger", { cache: "no-store" });
      const data = await res.json();
      setUrl(data && data.endpoints && data.endpoints["token-usage"]);
    } catch (err) { /* 老版本 ledger 没有该字段：用当前 base */ }
  }

  async function refreshBadge() {   // 服务在不在 / 合计多少 tokens
    try {
      const res = await fetch(base + "api/usage", { cache: "no-store" });
      const data = await res.json();
      const total = Number(data.totals.total_tokens || 0).toLocaleString("zh-CN");
      btn.textContent = "📊 Token " + total;
      btn.title = "token-usage 插件：" + data.totals.calls + " 次调用 · 合计 "
        + total + " tokens · " + base;
    } catch (err) {
      btn.textContent = "📊 Token 用量";
      btn.title = "读不到 " + base + "api/usage（插件未装载？点击仍可打开页面）";
    }
  }

  btn.addEventListener("click", async () => {
    panel.hidden = false;
    if (frame.dataset.ready !== "1") {
      await discover();
      frame.src = base;
      frame.dataset.ready = "1";
    }
    refreshBadge();
  });
  closeEl.addEventListener("click", () => { panel.hidden = true; });
  panel.addEventListener("click", (e) => { if (e.target === panel) panel.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") panel.hidden = true; });

  setUrl(base);
  discover().then(refreshBadge);
  setInterval(refreshBadge, 5000);
})();

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
</script>
<script>
"use strict";
/* ---------------- token 用量：token-usage 插件的挂载点 ----------------
   页面不需要认识任何插件：地址按这个顺序解析——
     ① URL 参数 ?token_usage=http://host:port/（手工指定，最优先）
     ② window.TOKEN_USAGE_URL（部署时注入）
     ③ /api/ledger 的 endpoints["token-usage"]（服务自报地址，通用发现）
     ④ 默认 http://127.0.0.1:8767/（token-usage 插件的默认端口）
   按钮上的数字 = 该服务 /api/usage 的合计 tokens（跨源读，靠服务端 CORS 头）；
   点开 = 同页 iframe 面板，不打断对话（Esc / 点空白 / 「关闭」收起）。      */
(function () {
  const btn = document.getElementById("token-btn");
  const panel = document.getElementById("token-panel");
  const frame = document.getElementById("token-frame");
  const urlEl = document.getElementById("token-url");
  const openEl = document.getElementById("token-open");
  const closeEl = document.getElementById("token-close");
  if (!btn || !panel || !frame) return;

  const fromQuery = new URLSearchParams(location.search).get("token_usage");
  let base = String(fromQuery || window.TOKEN_USAGE_URL || "http://127.0.0.1:8767/");
  if (!/^https?:\/\//i.test(base)) base = "http://" + base;
  if (!base.endsWith("/")) base += "/";

  function setUrl(url) {   // 只接受 http(s)：不给 javascript: 之类留任何口子
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return;
    base = url.endsWith("/") ? url : url + "/";
    urlEl.textContent = base;
    urlEl.title = base;
    openEl.href = base;
    frame.dataset.ready = "";
    if (!panel.hidden) { frame.src = base; frame.dataset.ready = "1"; }
  }

  async function discover() {   // ③ 服务自报地址（需要 webui 的 /api/ledger 带 endpoints）
    try {
      const res = await fetch("/api/ledger", { cache: "no-store" });
      const data = await res.json();
      setUrl(data && data.endpoints && data.endpoints["token-usage"]);
    } catch (err) { /* 老版本 ledger 没有该字段：用当前 base */ }
  }

  async function refreshBadge() {   // 服务在不在 / 合计多少 tokens
    try {
      const res = await fetch(base + "api/usage", { cache: "no-store" });
      const data = await res.json();
      const total = Number(data.totals.total_tokens || 0).toLocaleString("zh-CN");
      btn.textContent = "📊 Token " + total;
      btn.title = "token-usage 插件：" + data.totals.calls + " 次调用 · 合计 "
        + total + " tokens · " + base;
    } catch (err) {
      btn.textContent = "📊 Token 用量";
      btn.title = "读不到 " + base + "api/usage（插件未装载？点击仍可打开页面）";
    }
  }

  btn.addEventListener("click", async () => {
    panel.hidden = false;
    if (frame.dataset.ready !== "1") {
      await discover();
      frame.src = base;
      frame.dataset.ready = "1";
    }
    refreshBadge();
  });
  closeEl.addEventListener("click", () => { panel.hidden = true; });
  panel.addEventListener("click", (e) => { if (e.target === panel) panel.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") panel.hidden = true; });

  setUrl(base);
  discover().then(refreshBadge);
  setInterval(refreshBadge, 5000);
})();
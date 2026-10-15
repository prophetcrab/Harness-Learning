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
      refreshTaskPanel();                              // `_06`：一轮结束核对一次清单
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
  refreshTaskPanel();          // `_06`：会话换了，清单也要跟着换
  if (!opts.silent) setIdle(`会话 ${id} · ${(data.events || []).length} 条事件`);
}

/* ---------------- send ---------------- */
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
      if (String(frame.kind).startsWith("__")) {
        // 收尾帧（__done / __error / __compact_done …）由调用方看返回值；
        // **但 __approval 必须立刻交给 onFrame** —— 它是"有人在等你点头"，
        // 吞掉它整个 turn 就卡死在等审批上了（实测：模型调 write_file 后
        // 页面停在第 2 步"执行工具…"，审批卡片始终不出现）。
        last = { kind: frame.kind, payload: frame.payload };
        if (frame.kind === "__approval" || frame.kind === "__repair") {
          onFrame(frame.kind, frame.payload);
        }
      } else {
        onFrame(frame.kind, frame.payload);
        appendLiveEvent(frame.kind, frame.payload);
      }
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

/** 执行一条 `/todo ...`（人命令，不花模型的钱）：服务端解析 + 排版，这里只显示。 */
async function runTodo(line) {
  if (!state.current) state.current = "default";
  const args = String(line || "").replace(/^\/todo\s*/, "");
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(state.current)}/todo`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ line: args }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.status);
    // `/todo run`：服务端把清单拼成提示词回给我们，**这一步要跑一个真 turn**
    // （那次花模型的钱）。走既有的 send 流，所以对话、工具卡片、清单面板
    // 全都照常更新——不新增任何通道。
    if (data.run_prompt) {
      refreshTaskPanel();
      setRunning("把清单交给模型执行…");
      ensureWorking("模型按清单开始工作…");
      let done = null;
      try {
        const last = await streamFrames(
          `/api/sessions/${encodeURIComponent(state.current)}/send`,
          { text: data.run_prompt }, handleFrame,
        );
        if (last && last.kind === "__done") done = last.payload;
      } finally {
        removeWorking();
        await openSession(state.current, { silent: true });
        if (done) setIdle(`清单执行完毕 · turn ${done.turn} · ${done.status}`);
        refreshTaskPanel();
      }
      return;
    }
    showBanner(String(data.text || "").split("\n").join("  ·  "));
    setIdle(data.summary ? "清单：" + data.summary : "清单已更新");
    if (data.changed) refreshTaskPanel();
  } catch (err) {
    removeWorking();
    setRunline(`/todo 出错：${esc(err.message)}`, "err");
  }
}

/* ---------------- 任务清单面板（右上角常驻） ----------------
   清单是"模型自己拆的步骤"，用户随时该看得到——所以不做成弹窗，而是常驻
   在事件轨迹上方；`todo_write` 或 `/todo` 改了就刷新（靠 /api/.../todo 读）。 */
async function refreshTaskPanel() {
  const box = $("todo-box");
  if (!box || !state.current) return;
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(state.current)}/todo`,
      { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) { box.hidden = true; return; }
    const todos = data.todos;
    if (!todos || !todos.length) { box.hidden = true; return; }
    const mark = { completed: "✓", in_progress: "▸", pending: "○" };
    box.hidden = false;
    $("todo-count").textContent = data.summary || "";
    $("todo-list").innerHTML = todos.map((t) => `
      <div class="todo-row ${esc(t.status)}">
        <span class="todo-mark">${mark[t.status] || "○"}</span>
        <span class="todo-text">${esc(t.content)}</span>
      </div>`).join("");
  } catch (err) { box.hidden = true; }
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
  {
    name: "/todo",
    desc: "任务清单：直接写内容即可加一条；勾选用 done <序号>，还有 doing / rm / clear",
    takesArgs: true,
    run: (line) => runTodo(line),
  },
];

const cmdMenu = $("cmdmenu");
let cmdItems = [];      // 当前可见的候选
let cmdActive = 0;      // 键盘高亮的下标

/** 取当前应该展示的候选（null = 不展示菜单）。 */
function cmdCandidates(value) {
  if (state.sending) return null;
  if (!value.startsWith("/")) return null;
  // 已经打了空格：只可能是"带参数的命令正在填参数"——把该命令的用法继续
  // 当提示挂着（人照着打，不用记）。否则空格一出现菜单就消失，
  // `/todo ` 之后人就得凭记忆敲子命令了。
  const spaced = /\s/.test(value);
  if (spaced) {
    const head = value.split(/\s/)[0];
    const command = COMMANDS.find((c) => c.name === head && c.takesArgs);
    return command ? [command] : null;
  }
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
  const line = ta.value.trim();                  // 原始整行（含子命令与参数）
  hideCmdMenu();
  ta.value = ""; autosize();
  await command.run(line);
}

$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = ta.value.trim();
  if (!text || state.sending) return;
  // 命中的命令 → 走命令，别当普通消息发给模型。
  // 两种命中：完全等于命令名，或"命令名 + 空格 + 参数"（带参数的命令）。
  const exact = COMMANDS.find(
    (c) => c.name === text || (c.takesArgs && text.startsWith(c.name + " ")),
  );
  if (exact && !cmdMenu.hidden) {
    hideCmdMenu();
    ta.value = ""; autosize();
    await exact.run(text);
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
      const command = cmdItems[cmdActive];
      const typingArgs = /\s/.test(ta.value);      // 已在填参数了
      if (e.key === "Tab" || command.takesArgs && !typingArgs) {
        e.preventDefault();
        // 带参数的命令：补全成 "/todo "（加个空格）把光标交给参数，
        // 不猜他想 add 还是 done
        ta.value = command.name + (command.takesArgs ? " " : "");
        autosize(); renderCmdMenu();
        return;
      }
      e.preventDefault();
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

/* ---------------- 仪表面板的通用挂载（`_05` 重构抽出的共用件） ----------------
   原先 token 用量与上下文仪表两块各写一遍同样的逻辑（解析地址 → 发现 endpoint →
   iframe 打开 → 轮询刷新徽标），两份几乎逐字重复。现在只剩这一份工厂，两处
   调用各给一个配置。

   地址解析顺序（页面不认识任何插件，只认服务自报的地址）：
     ① URL 参数 ?<queryKey>=http://host:port/（手工指定，最优先）
     ② window.<globalKey>（部署时注入）
     ③ /api/ledger 的 endpoints["<service>"]（服务自报地址，通用发现）
     ④ 默认地址（该插件的默认端口）

   返回一个句柄（含 base 只读属性），便于测试与排错；元素不全时返回 null。 */
function mountPanel(config) {
  const { queryKey, globalKey, service, title, badge } = config;
  const btn = document.getElementById(config.btnId);
  const panel = document.getElementById(config.panelId);
  const frame = document.getElementById(config.frameId);
  const urlEl = document.getElementById(config.urlId);
  const openEl = document.getElementById(config.openId);
  const closeEl = document.getElementById(config.closeId);
  if (!btn || !panel || !frame) return null;

  const fromQuery = new URLSearchParams(location.search).get(queryKey);
  let base = String(fromQuery || window[globalKey] || config.defaultUrl);
  if (!/^https?:\/\//i.test(base)) base = "http://" + base;   // 只接受 http(s)
  if (!base.endsWith("/")) base += "/";

  function setUrl(url) {
    if (typeof url !== "string" || !/^https?:\/\//i.test(url)) return;
    base = url.endsWith("/") ? url : url + "/";
    urlEl.textContent = base;
    urlEl.title = base;
    openEl.href = base;
    frame.dataset.ready = "";
    if (!panel.hidden) { frame.src = base; frame.dataset.ready = "1"; }
  }

  async function discover() {   // 服务自报地址（需要 /api/ledger 带 endpoints）
    try {
      const data = await api("/api/ledger");
      setUrl(data && data.endpoints && data.endpoints[service]);
    } catch (err) { /* 保留当前 base：老版本 ledger 没有 endpoints 字段 */ }
  }

  async function refreshBadge() {   // 服务在不在 / 徽标显示什么
    if (!badge) return;
    try {
      const res = await fetch(base + badge.path, { cache: "no-store" });
      const data = await res.json();
      btn.textContent = badge.text(data);
      btn.title = title + " 插件：" + badge.tip(data) + " · " + base;
    } catch (err) {
      btn.textContent = badge.fallback;
      btn.title = "读不到 " + base + badge.path + "（插件未装载？点击仍可打开页面）";
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
  return { get base() { return base; } };
}

/* ---------------- 上下文仪表（context-meter 插件） ---------------- */
mountPanel({
  btnId: "ctx-btn", panelId: "ctx-panel", frameId: "ctx-frame",
  urlId: "ctx-url", openId: "ctx-open", closeId: "ctx-close",
  queryKey: "context_meter", globalKey: "CONTEXT_METER_URL",
  service: "context-meter", defaultUrl: "http://127.0.0.1:8768/",
  title: "context-meter",
  badge: {
    path: "api/context",
    // 窗口 1M 之后百分比几乎恒为 0.x%，取整会一直显示 "0%"——小数位按量级给：
    // 小于 10% 时保留 1 位（0.2% 这种才看得见趋势），够大就取整省地方。
    text: (d) => {
      const percent = Number(d.percent ?? 0);
      const shown = percent < 10 ? percent.toFixed(1) : percent.toFixed(0);
      return "📐 上下文 " + shown + "%";
    },
    tip: (d) => "已用 " + Number(d.used ?? 0).toLocaleString("zh-CN")
      + " / " + Number(d.capacity ?? 0).toLocaleString("zh-CN") + " tokens",
    fallback: "📐 上下文",
  },
});

/* ---------------- token 用量（token-usage 插件） ---------------- */
mountPanel({
  btnId: "token-btn", panelId: "token-panel", frameId: "token-frame",
  urlId: "token-url", openId: "token-open", closeId: "token-close",
  queryKey: "token_usage", globalKey: "TOKEN_USAGE_URL",
  service: "token-usage", defaultUrl: "http://127.0.0.1:8767/",
  title: "token-usage",
  badge: {
    path: "api/usage",
    text: (d) => "📊 Token " + Number(d.totals?.total_tokens || 0).toLocaleString("zh-CN"),
    tip: (d) => (d.calls ?? 0) + " 次调用 · 合计 "
      + Number(d.totals?.total_tokens || 0).toLocaleString("zh-CN") + " tokens",
    fallback: "📊 Token 用量",
  },
});

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

/* 清单面板在会话切换时也要跟着换（openSession 里会调 refreshTaskPanel）。 */


# 0051 会话日志对齐 dsh：时间戳 / header / 目录布局 / 读写句柄 / 未知事件

- 日期：2026-10-16
- 阶段：P4_Coding（M2 收尾）
- 状态：已采纳

## 背景

对照 dsh 的 `session-persistence-jsonl` 后，我们原来的实现只有"一行一条事件"
这个骨架，缺六件事。用户点名要做其中六项（压缩暂不做）：

1. 每条事件带时间戳；
2. 撕裂的尾行截掉坏字节并重写（不是只读时跳过）；
3. 读 / 写句柄分离 + `flush()` 全局耐久屏障；
4. `stat` / `list` 只读 header（事件数 / 大小 / revision）；
5. 未知事件类型的策略（不认识且非 `ignorable` → 拒绝解读）；
6. 每会话一个目录 + 按 cwd 分项目目录（`--<cwd>--/<id>/session.vN.jsonl`）。

## 决策

### 一、磁盘布局

    <root>/
      --<cwd>--/                 # 可读的项目目录（无 cwd → _no-cwd_）
        <encoded-id>/            # 会话自己的目录（id 单射转义）
          session.v1.jsonl       # 首行 header + 每行一条事件

- **首行是 header**：`{"format": "p4-session", "version": 1, "session_id", "created_at", "cwd"}`；
- **版本在文件名里**（`session.vN.jsonl`），读时选版本号最高的；不认识的版本
  拒绝解读（`SessionFormatError`）；
- **id 单射转义**（`urllib.parse.quote(safe="")`）：`a/b` 与 `a_b` 不会撞名；
- 项目目录名把 cwd 的两种分隔符归一后折成下划线（Windows 盘符的 `:` 也变成
  `_`，所以是 `--D__project_...--`）。

### 二、事件时间戳

`Session.append()` 的 `at` 缺省取**当前 UTC 时间**（ISO 8601）。之前默认空串，
日志里所有事件都是 `"at": ""`——排障和统计都没法用，是实打实的缺失。

### 三、读写句柄 + 全局屏障

    SessionReader   只读：header() / events()
    SessionWriter   写：打开即占位（header 立刻 fsync）、修复撕裂尾巴、append
    Store.flush()   全局屏障：所有打开的写句柄一次 fsync

**耐久点从"每条事件"改成"每轮结束"**：

    session/start → flush
    每个 turn/end → flush
    中间事件     → 只进缓冲

理由：一个 turn 有十几条事件，逐条 fsync 是明显的 I/O 放大（真实 API 下每个
step 都写磁盘）。代价如实记：崩溃最多丢当前这一轮的尾部；**已经结束的轮次
都在盘上**。

### 四、撕裂尾巴：截掉而不是只跳过

    truncate_torn_tail(path) -> 被截掉的字节数

- **只读打开**：跳过坏末行，`RepairReport.truncated_bytes` 报告；
- **写打开**：先截掉坏字节再继续追加——文件不会一直带着坏尾巴；
- **中间行损坏**：直接 `SessionFormatError`（不是"跳过了事"——静默跳过中间
  一条事件会重建出错误会话）。

### 五、未知事件类型：`ignorable` 标记

    kind 不认识 且 ignorable=False  → UnknownEventTypeError（拒绝解读整份日志）
    kind 不认识 且 ignorable=True   → 跳过（RepairReport.ignored_unknown += 1）

理由（dsh 同款）：新版本 harness 写入的**必需**事件被旧版本静默跳过，会投影出
一个错误的会话；而可选事件（遥测、调试）跳过无害。写日志的一方显式声明。

`KNOWN_EVENT_TYPES` 是当前 11 种事件的白名单，有测试钉住"我们写的类型都在里面"。

### 六、stat / list 只读 header

    store.stat(id)   → {session_id, cwd, created_at, version, size_bytes,
                        revision, path}
    store.list()     → 每个会话一行（含 project 目录名）

`revision` = `{size}:{mtime_ns}`（尽力而为的变更令牌）。有测试用 monkeypatch
钉住"调用 stat/list 时 `SessionReader.events()` 一次都没被调用"。

## 实测

真实 DeepSeek 端到端：

```
$ python app.py --realtime --yes --session persist1 --ask "用 calc 算 12*12…"
  steps=2 tokens=3408
[done] 12 × 12 = **144**

磁盘: sessions/--D__project_Harness-Learning_P4_Coding_workspace--/persist1/session.v1.jsonl
header: {'format': 'p4-session', 'version': 1, 'session_id': 'persist1',
         'created_at': '2026-10-19T08:35:12…', 'cwd': 'D:\\…\\workspace'}
事件数: 11，首条事件 at: 2026-10-19T08:35:12.828323+00:00

$ python app.py --realtime --yes --session persist1 --ask "刚才我问了什么？"
[done] 你刚才问的是：用 calc 算 12*12…（恢复正确）
```

## 反向验证（每项都撤掉机制看测试变红）

| 撤掉 | 变红的测试 |
|---|---|
| 撕裂尾巴截断 | `test_truncate_torn_tail_removes_partial_line`、`test_writer_repairs_torn_tail_on_open` |
| 未知事件拒绝 | `test_unknown_event_without_marker_is_rejected` |
| header 立即落盘 | `test_writer_header_is_durable_immediately` |

第三条第一次没抓到时也发现了问题：原测试把 `SessionWriter` 当临时对象，
CPython 回收时会关闭文件从而隐式 flush。改成"句柄还活着时检查文件大小"才
真正钉住。

## 不做（本次范围外）

- **压缩**（zstd + 校验和）：用户明确说先不做；
- **跨进程单写者认领**（文件锁）：同一进程内由 store 保证唯一写句柄；多进程
  同时写同一会话没有保护；
- **格式迁移**（v1 → v2）：版本号已经在文件名与 header 里，迁移器等到真要
  改格式时再写；
- **事件数**：`stat` 不做 `eventCount`——那需要扫全文件，违背"只读 header"
  的初衷。要的话应由 `list` 的调用方按需读一次事件。

## 相关

- [0050](0050-minimal-app.md)（最小应用）、
  [0047](0047-sessions-workspace-project.md)（会话与投影）；
- dsh 对照：`dsh-session-persistence-jsonl` 的目录布局、header、读写句柄、
  撕裂尾部恢复与 `ignorable` 策略。

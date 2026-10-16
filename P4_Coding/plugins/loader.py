"""loader —— 插件世界的「行政系统」：条目、装配单、树、按单装配。

**loader 为什么是插件**（决策 0042；与 dsh 同形）：内核只管"一个插件"的生死
（load / unload / disable / enable）；"哪些插件、怎么分组、各配什么参数"是
**装配**这件事本身——它自己就是一个可装卸、可替换的插件。dsh 里 `ctx.plugin`
在 cordis 核心，条目树与 `ctx.loader.*` 在 `@cordisjs/plugin-loader`；
本模块是后者的最小版。好处（0042 记过）：「装配」可替换、可停用——停用
loader，已装配的树照常运行，只是不能再变。

**四个概念**：

    条目 Entry        装配单里的一行：id（地址）、name（目录里的名字）、
                      config（构造参数）、role（装配方声明的角色，**只搬运不解释**）、
                      enabled、children（分组用）
    装配单 manifest    条目的列表——**产品形态就是它**（换一份单子 = 换一个产品）
    目录 catalog       name → 工厂：这个运行时能装配的名字（"可安装白名单"升格）
    树 tree            运行中的节点：条目 + 运行时开关 + 装载句柄 + 父子链

**路径 id**：`:` 是父子分隔符。条目自己的 id 不许含 `:` 或空白（它是地址）；
完整路径由嵌套位置决定（`app:ui`）。**id 即路径，不做移动**——改位置 = 改装配单
重装子树（0042 的立场）。

**运行时的开关不回写装配单**（0042 的第二条立场）：disable / enable 只改运行中
的树，重启回到装配单；条目自己的 `enabled=False` 是"声明就不装"。

**两条级联各管各的，一起生效**：

    loader 的**所有权级联**   卸 / 停一个条目，它的子树跟着走（深的先退）
    core 的**依赖级联**       插件退位时，依赖它服务的插件自动退位（步 2 已有）

**开关继承**：父关着，子即使自己的开关是开也不运行；enable 回来时，之前单独
关掉的条目依然是关的（各自的状态留着）——"整层一起开关"不该抹掉单个的意愿。

**本步做到哪**（M1 步 6）：树与所有权级联——unload / disable / enable 以子树为
单位、深的先退。**没做**：热插拔 load / update / replace 与 revision（步 7）、
启动器（步 8）。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from core import Context, Disposer, PluginInfo, plugin_name

#: loader 服务的名字（loader 插件提供它；启动器 / 管理面 / 模型工具都用这一条通道）。
LOADER_SERVICE = "loader"

#: 装配单条目允许出现的字段——写错一个字母就是装配期的错误，而不是运行时的怪事。
_ENTRY_KEYS = frozenset({"id", "name", "config", "role", "enabled", "children"})

#: 条目在快照里的状态。
#:
#:     group     分组条目（没有代码的容器）
#:     skipped   开关说它不该运行（自己的或祖先的——开关是继承的）
#:     unloaded  该运行、但不在 core 台账里（被绕过 loader 卸了 / 还没装到它）
#:     其余       core 台账里的实时状态（active / pending / failed / disabled）
EntryStatus = Literal[
    "group", "skipped", "active", "pending", "failed", "disabled", "unloaded"
]


class AssemblyError(RuntimeError):
    """装配 / 树操作失败（fail loud）：结构非法、名字不在目录、构造 / 装载失败。"""


@dataclass(frozen=True)
class Entry:
    """装配单里的一行（数据，不是代码）。

    `name=None` 的条目是**分组**：没有代码的容器，作用是"一整层一起开关"
    （一份单子用分组把"web 应用"打包成一个可折叠的单元）。
    `config` 会被整份交给工厂（`factory(**config)`）——**参数写错就是装配期
    错误**，不存在"改一半旧一半"的合并语义。
    """

    id: str
    name: str | None = None
    config: Mapping[str, Any] = field(default_factory=dict)
    role: str | None = None
    enabled: bool = True
    children: tuple[Entry, ...] = ()

    def __post_init__(self) -> None:
        # 声明要真的"冻"：config 拷贝一份、children 定死成元组——
        # 装配单是产品形态的真相，不接受外面悄悄改。
        # （类型不对的 config / children 原样留着，交给校验去 fail loud——
        # 在这里强转会抛出与装配不相干的 TypeError，反而看不出是哪条条目错。）
        if isinstance(self.config, Mapping):
            object.__setattr__(self, "config", dict(self.config))
        if not isinstance(self.children, tuple):
            object.__setattr__(self, "children", tuple(self.children))


@dataclass(frozen=True)
class EntryState:
    """一条条目的当前状态（观测用：启动器 dump / 管理面 / 测试读的就是它）。"""

    path: str
    parent: str | None
    name: str | None
    role: str | None
    status: EntryStatus
    plugin: str | None  # 实际装载进 ctx 的插件名（None = 分组 / 没装载过）
    missing: tuple[str, ...] = ()  # pending 时缺的依赖（其余情况为空）
    error: BaseException | None = None  # failed 时的错误


@dataclass
class _Node:
    """loader 对一条条目的记账（装配 / 树操作 / 热插拔都读它）。"""

    path: str
    entry: Entry
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    #: 运行时开关（装配时取 entry.enabled；disable / enable 改它）。
    #: **生效还要看祖先**——父关着，子即使开关是开也不运行（见 `_effective_enabled`）。
    own_enabled: bool = True
    plugin_name: str | None = None
    handle: Disposer | None = None  # 核心给的卸载句柄（认"这一次装载"，见 core 的句柄语义）
    #: 装载调用抛错、但插件其实装上了（失败在**别人**的激活里）——此时拿不到
    #: 句柄，回滚只能按名字卸。见 `_assemble_one`。
    name_unload: bool = False


# ---------------------------------------------------------------------------
# 装配单解析（原始数据 → Entry；结构问题一律 fail loud）
# ---------------------------------------------------------------------------


def entry_from_dict(data: Mapping[str, Any]) -> Entry:
    """把一行原始数据（dict）变成 Entry；不认识的字段、缺 id 都在这里拦下。"""
    unknown = sorted(set(data) - _ENTRY_KEYS)
    if unknown:
        raise AssemblyError(
            f"条目里有不认识的字段：{unknown}（可用：{sorted(_ENTRY_KEYS)}）"
        )
    if "id" not in data:
        raise AssemblyError(f"条目缺少 id：{data!r}")
    children = data.get("children", ())
    if isinstance(children, (str, bytes, Mapping)) or not isinstance(children, Iterable):
        raise AssemblyError(f"条目 {data.get('id')!r} 的 children 必须是条目列表")
    return Entry(
        id=data["id"],
        name=data.get("name"),
        config=data.get("config", {}),
        role=data.get("role"),
        enabled=data.get("enabled", True),
        children=tuple(
            entry_from_dict(item) if isinstance(item, Mapping) else item for item in children
        ),
    )


def _parse_manifest(manifest: Sequence[Entry | Mapping[str, Any]]) -> list[Entry]:
    if isinstance(manifest, (str, bytes, Mapping)) or not isinstance(manifest, Iterable):
        raise AssemblyError("装配单必须是条目列表")
    out: list[Entry] = []
    for item in manifest:
        if isinstance(item, Entry):
            out.append(item)
        elif isinstance(item, Mapping):
            out.append(entry_from_dict(item))
        else:
            raise AssemblyError(f"装配单里出现既不是 Entry 也不是字典的东西：{item!r}")
    return out


def _check_entry(entry: object, where: str) -> None:
    if not isinstance(entry, Entry):
        raise AssemblyError(f"{where or '装配单顶层'}里出现不是条目的东西：{entry!r}")
    if not isinstance(entry.id, str) or not entry.id:
        raise AssemblyError(f"条目的 id 必须是非空字符串，收到：{entry.id!r}")
    if ":" in entry.id:
        raise AssemblyError(
            f"条目 id 不能含 ':'（它是父子分隔符，地址由嵌套位置决定）：{entry.id!r}"
        )
    if any(ch.isspace() for ch in entry.id):
        raise AssemblyError(f"条目 id 不能含空白（它是地址）：{entry.id!r}")
    if entry.name is not None and (not isinstance(entry.name, str) or not entry.name):
        raise AssemblyError(
            f"条目 {entry.id!r} 的 name 必须是非空字符串或 None（None = 分组条目）"
        )
    if not isinstance(entry.config, Mapping):
        raise AssemblyError(f"条目 {entry.id!r} 的 config 必须是字典")
    if entry.role is not None and not isinstance(entry.role, str):
        raise AssemblyError(f"条目 {entry.id!r} 的 role 必须是字符串或 None")
    if not isinstance(entry.enabled, bool):
        raise AssemblyError(f"条目 {entry.id!r} 的 enabled 必须是布尔值")


def _walk(
    entries: Sequence[Entry], prefix: str = "", parent: str | None = None
) -> Iterable[tuple[str, Entry, str | None]]:
    """深度优先展开：产出 (完整路径, 条目, 父路径)（父先子后 = 装配顺序）。"""
    for entry in entries:
        _check_entry(entry, prefix)
        path = f"{prefix}:{entry.id}" if prefix else entry.id
        yield path, entry, parent
        yield from _walk(entry.children, path, path)


def _check_catalog(
    catalog: Mapping[str, Callable[..., object]],
) -> dict[str, Callable[..., object]]:
    """目录是声明（name → 工厂）：写错在装载期就报，别等运行时。"""
    if not isinstance(catalog, Mapping):
        raise ValueError("目录必须是 name → 工厂 的映射")
    checked: dict[str, Callable[..., object]] = {}
    for name, factory in catalog.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"目录里的名字必须是非空字符串：{name!r}")
        if ":" in name:
            raise ValueError(f"目录名字不能含 ':'：{name!r}")
        if not callable(factory):
            raise ValueError(f"目录里 {name!r} 的工厂不可调用：{factory!r}")
        checked[name] = factory
    return checked


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------


class LoaderService:
    """按装配单装配、维护树、按路径操作子树、报告当前状态。

    操作一览（全部**唯一装配路径**：启动器、管理面、模型侧工具将来都走它）：

        assemble(manifest)      整批装配（失败**整批回滚**——世界不留半截树）
        unload(path)            卸载子树（深的先退；条目离开树）
        disable(path) / enable(path)   开关子树（继承；登记保留，一键恢复）

    这是**唯一装配路径**：启动器（步 8）、管理面、模型侧工具将来都走它——
    "两处各写一份装配逻辑"是明确的禁止项（P3 的白名单漂移教训）。
    """

    def __init__(self, ctx: Context, catalog: Mapping[str, Callable[..., object]]) -> None:
        self._ctx = ctx
        self._catalog = dict(catalog)
        self._nodes: dict[str, _Node] = {}

    @property
    def catalog_names(self) -> tuple[str, ...]:
        """目录里可装配的名字（排序：报错文案与 dump 用）。"""
        return tuple(sorted(self._catalog))

    # ------------------------------------------------------------------
    # 装配（整批）
    # ------------------------------------------------------------------

    def assemble(self, manifest: Sequence[Entry | Mapping[str, Any]]) -> tuple[EntryState, ...]:
        """按一份装配单整批装配；成功返回装配后的快照，失败**整批回滚**并抛错。

        顺序：解析校验 → 把条目挂进树（分组 / 关闭的也挂——快照里看得见单子的
        全貌）→ 按单子顺序装载生效的插件（`factory(**config)`）→ 检查本批没有
        等待依赖的残留 → 返回快照。

        失败 = 回到装配前：本批装过的插件逆序卸载、本批挂上的条目一并摘掉
        （世界不能因为一次失败的装配留下"注册了但没装"的半截树——同一条单子
        修好之后要能直接重试）。

        **严格残留检查**：本批不该有等待依赖的条目（有 = 装配单缺了提供者、
        或依赖名写错）——装配期的 fail loud。单条目的热插拔（步 7）没有这条：
        那是"变更"，语义不同。
        """
        entries = _parse_manifest(manifest)
        flat = list(_walk(entries))
        # 路径唯一：同一层里重复 id、或与已装配的路径相撞，都在这里拦下。
        seen = set(self._nodes)
        for path, _entry, _parent in flat:
            if path in seen:
                raise AssemblyError(f"条目路径重复或已被占用：{path!r}（id 在同一层里要唯一）")
            seen.add(path)

        nodes: list[_Node] = []
        for path, entry, parent in flat:
            node = _Node(path=path, entry=entry, parent=parent, own_enabled=entry.enabled)
            self._nodes[path] = node
            if parent is not None:
                self._nodes[parent].children.append(path)
            nodes.append(node)

        try:
            for node in nodes:
                self._assemble_one(node)
            pending = [
                name
                for name in self._ctx.pending_plugins
                if any(node.plugin_name == name for node in nodes)
            ]
            if pending:
                detail = "；".join(
                    f"{self._label_of(name)} 缺 {self._ctx.missing_dependencies(name)}"
                    for name in pending
                )
                raise AssemblyError(
                    f"装配后仍有插件等待依赖（装配单缺了提供者，或依赖名写错）：{detail}"
                )
        except AssemblyError:
            self._rollback(nodes)
            self._detach_nodes(nodes)
            raise
        except BaseException as exc:
            self._rollback(nodes)
            self._detach_nodes(nodes)
            raise AssemblyError(f"装配失败：{exc}") from exc
        return self.snapshot()

    # ------------------------------------------------------------------
    # 树操作（按路径，作用于子树）
    # ------------------------------------------------------------------

    def unload(self, path: str) -> tuple[str, ...]:
        """卸载一棵子树：条目连同后代一起离开树（返回被卸载的路径，装配顺序）。

        顺序"**深的先退**"（子先父后）：每个插件的清理代码运行时，它依赖的
        （父的）服务都还在——与 core 依赖级联同一条理由。core 那边还会顺带
        处理"依赖这些服务的外部插件"（自动退位，与子树无关的也照顾到）。

        某个条目的清理抛错时：**继续卸载其余条目**、节点照常摘除（core 已经
        保证该回卷的都回卷了，只是报了第一个错），最后抛 `AssemblyError`。

        卸载 = 条目离开树；要回来请重新装条目（步 7 的 load / 或重新 assemble）。
        路径不存在 → AssemblyError（名字是地址，敲错必须报出来）。
        """
        nodes = self._subtree(path)
        errors: list[tuple[str, BaseException]] = []
        for node in reversed(nodes):  # 深的先退
            record = self._record_of(node)
            if record is None:
                continue  # 分组 / 从没装载过 / 已被绕过 loader 卸掉
            try:
                self._ctx.unload_plugin(node.plugin_name)  # type: ignore[arg-type]
            except BaseException as exc:
                errors.append((node.path, exc))
            node.handle = None
        self._detach_nodes(nodes)
        if errors:
            where, exc = errors[0]
            raise AssemblyError(
                f"卸载 {path!r} 时条目 {where!r} 的清理失败（其余条目已照常卸载）：{exc}"
            ) from exc
        return tuple(node.path for node in nodes)

    def disable(self, path: str) -> tuple[str, ...]:
        """停用一棵子树（返回实际被停用的路径，**深的先退**的顺序）。

        "一键关掉一层"：core 的停用语义——effect 回卷、服务撤掉、状态标
        disabled，但插件对象与配置都留着，`enable` 用**同一个实例**恢复
        （setup 重跑）。

        只关**根**的开关：后代是"继承"关掉——各自原来的开关状态留着，
        `enable` 回来时，之前单独关掉的条目依然是关的。

        某个条目的清理抛错时：其余照常退位，`AssemblyError` 最后抛出。
        """
        nodes = self._subtree(path)
        nodes[0].own_enabled = False
        affected: list[str] = []
        errors: list[tuple[str, BaseException]] = []
        for node in reversed(nodes):  # 深的先退
            record = self._record_of(node)
            if record is None or record.status == "disabled":
                continue  # 分组 / 没装载过 / 已经是停用状态
            try:
                self._ctx.disable_plugin(node.plugin_name)  # type: ignore[arg-type]
            except BaseException as exc:
                errors.append((node.path, exc))
                continue
            affected.append(node.path)
        if errors:
            where, exc = errors[0]
            raise AssemblyError(
                f"停用 {path!r} 时条目 {where!r} 失败（其余条目已照常退位）：{exc}"
            ) from exc
        return tuple(affected)

    def enable(self, path: str) -> tuple[str, ...]:
        """启用一棵子树（返回被重新装载 / 恢复的路径，**浅的先来**的顺序）。

        顺序"**浅的先来**"（父先子后）：父的服务先就位，子才有依赖可用。
        三种情况：

            之前没装载过（装配时被关）   → 现装（factory(**config)）
            登记还在（停用过 / 失败过）  → 用**同一个实例**恢复（setup 重跑）
            本来就跑着                  → 不动

        **不保证全部跑起来**（core 的 enable 同款诚实语义）：依赖还没就位的
        停在 pending（快照里看得见缺什么）；祖先的开关还关着 → 整体仍是
        skipped——开关是继承的。

        某个条目的恢复抛错时：其余照常处理，`AssemblyError` 最后抛出。
        """
        nodes = self._subtree(path)
        nodes[0].own_enabled = True
        affected: list[str] = []
        errors: list[tuple[str, BaseException]] = []
        for node in nodes:  # 浅的先来
            if node.entry.name is None or not self._effective_enabled(node):
                continue  # 分组 / 自己或祖先的开关还关着：不动
            record = self._record_of(node)
            try:
                if record is None:
                    self._assemble_one(node)
                    affected.append(node.path)
                elif record.status in ("disabled", "failed"):
                    self._ctx.enable_plugin(node.plugin_name)  # type: ignore[arg-type]
                    affected.append(node.path)
            except BaseException as exc:
                errors.append((node.path, exc))
        if errors:
            where, exc = errors[0]
            raise AssemblyError(
                f"启用 {path!r} 时条目 {where!r} 失败（其余条目已照常处理）：{exc}"
            ) from exc
        return tuple(affected)

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------

    def snapshot(self) -> tuple[EntryState, ...]:
        """全部条目的当前状态（按装配顺序，**实时**从核心台账读）。

        实时读取是刻意的：绕过 loader 直接卸了某个插件，快照会如实显示
        `unloaded`，而不是显示 loader 以为的样子。
        """
        states: list[EntryState] = []
        for node in self._nodes.values():
            entry = node.entry
            if not self._effective_enabled(node):
                # 开关说它不该运行（自己的或祖先的）：比"它是个分组"更相关。
                status: EntryStatus = "skipped"
                missing: tuple[str, ...] = ()
                error: BaseException | None = None
            elif entry.name is None:
                status, missing, error = "group", (), None
            else:
                info = self._record_of(node)
                if info is None:
                    status, missing, error = "unloaded", (), None
                else:
                    status, missing, error = info.status, info.missing, info.error
            states.append(
                EntryState(
                    path=node.path,
                    parent=node.parent,
                    name=entry.name,
                    role=entry.role,
                    status=status,
                    plugin=node.plugin_name,
                    missing=missing,
                    error=error,
                )
            )
        return tuple(states)

    # ------------------------------------------------------------------
    # 内部：装配一条 / 失败处理
    # ------------------------------------------------------------------

    def _assemble_one(self, node: _Node) -> None:
        entry = node.entry
        if entry.name is None or not self._effective_enabled(node):
            return  # 分组（没有代码）/ 开关关着的条目：只进快照，不装载
        factory = self._catalog.get(entry.name)
        if factory is None:
            available = "、".join(self.catalog_names) or "（目录是空的）"
            raise AssemblyError(
                f"条目 {node.path!r} 的名字 {entry.name!r} 不在目录里（可用：{available}）"
            )
        try:
            instance = factory(**entry.config)
        except BaseException as exc:
            raise AssemblyError(
                f"条目 {node.path!r} 构造失败（name={entry.name!r}、"
                f"config={dict(entry.config)!r}——参数写错了吗）：{exc}"
            ) from exc
        node.plugin_name = plugin_name(instance)
        before = {info.name for info in self._ctx.plugins}
        try:
            node.handle = self._ctx.load_plugin(instance)
        except BaseException as exc:
            # 装载调用抛错 ≠ 什么都没装：失败可能发生在**别人**的激活里
            # （被这次装卸解开依赖的那个），本插件此刻就在台账里。抛错路径上
            # 拿不到句柄，所以记下来让回滚按名字卸。
            node.name_unload = node.plugin_name in (
                {info.name for info in self._ctx.plugins} - before
            )
            raise AssemblyError(
                f"装配条目 {node.path!r} 时失败（插件 {node.plugin_name!r}）：{exc}"
                + self._failed_hint()
            ) from exc

    def _failed_hint(self) -> str:
        """装载失败时常有"真凶"：炸的是**别的**插件（被这次装卸解开依赖的那个）。

        读一眼当前 failed 的插件附在消息里——否则用户盯着"正在装的那条"，
        看不出到底哪里错了。
        """
        failed = [info for info in self._ctx.plugins if info.status == "failed"]
        if not failed:
            return ""
        names = "、".join(info.name for info in failed)
        return f"（当前处于 failed 的插件：{names}——常是被这次装配解开的依赖在激活时失败）"

    def _label_of(self, plugin_name_: str) -> str:
        """插件名 → 它在装配单里的位置（报错文案用）。"""
        for node in self._nodes.values():
            if node.plugin_name == plugin_name_:
                return f"{node.path!r}({plugin_name_})"
        return repr(plugin_name_)

    def _rollback(self, nodes: list[_Node]) -> None:
        """逆序卸载本批装载过的插件。

        **尽力而为**：某个卸载抛错不打断其余——原始异常更重要，不遮蔽它。
        卸载本身在核心那边已经保证"该跑的清理都跑完"（只是可能报第一个错），
        所以这里丢掉的是报告，不是清理。
        """
        for node in reversed(nodes):
            handle, node.handle = node.handle, None
            name_unload, node.name_unload = node.name_unload, False
            if handle is not None:
                try:
                    handle()
                except Exception:  # 回滚的报错不遮蔽原始异常
                    continue
            elif name_unload and node.plugin_name is not None:
                try:
                    self._ctx.unload_plugin(node.plugin_name)
                except Exception:
                    continue

    # ------------------------------------------------------------------
    # 内部：树
    # ------------------------------------------------------------------

    def _record_of(self, node: _Node) -> PluginInfo | None:
        """这个节点对应的 core 插件状态（分组 / 没装载过 / 已被卸掉 → None）。"""
        if node.plugin_name is None:
            return None
        return next(
            (info for info in self._ctx.plugins if info.name == node.plugin_name), None
        )

    def _effective_enabled(self, node: _Node) -> bool:
        """生效开关 = 自己开着、并且所有祖先都开着（开关继承）。"""
        current: _Node | None = node
        while current is not None:
            if not current.own_enabled:
                return False
            current = self._nodes.get(current.parent) if current.parent else None
        return True

    def _subtree(self, path: str) -> list[_Node]:
        """一棵子树，按装配顺序（父先子后）；路径不存在 → AssemblyError。"""
        node = self._nodes.get(path)
        if node is None:
            known = "、".join(self._nodes) or "（树是空的）"
            raise AssemblyError(f"条目不存在：{path!r}（当前：{known}）")
        out: list[_Node] = []
        stack = [node]
        while stack:
            current = stack.pop()
            out.append(current)
            stack.extend(self._nodes[child] for child in reversed(current.children))
        return out

    def _detach_nodes(self, nodes: list[_Node]) -> None:
        """把一棵子树从树上摘掉（先与父链断开，再删节点）。"""
        root = nodes[0]
        parent = self._nodes.get(root.parent) if root.parent else None
        if parent is not None and root.path in parent.children:
            parent.children.remove(root.path)
        for node in nodes:
            self._nodes.pop(node.path, None)


class LoaderPlugin:
    """提供 `loader` 服务（装配 / 树操作 / 快照）。

    **它自己是一个插件**：可装卸、可替换——停用它，已装配的树照常运行，
    只是不能再变（0042「装配不是特权」的可测形式）。它装载的插件**不挂在
    它的 effect 下**：loader 不是那些插件的主人，只是装配它们的行政系统。
    """

    name = "loader"

    def __init__(self, catalog: Mapping[str, Callable[..., object]]) -> None:
        self._catalog = _check_catalog(catalog)

    def setup(self, ctx: Context) -> None:
        ctx.provide(LOADER_SERVICE, LoaderService(ctx, self._catalog))


def loader_plugin(catalog: Mapping[str, Callable[..., object]]) -> LoaderPlugin:
    """工厂：loader 需要一张目录（name → 工厂）。"""
    return LoaderPlugin(catalog=catalog)


__all__ = [
    "LOADER_SERVICE",
    "AssemblyError",
    "Entry",
    "EntryState",
    "EntryStatus",
    "LoaderPlugin",
    "LoaderService",
    "entry_from_dict",
    "loader_plugin",
]

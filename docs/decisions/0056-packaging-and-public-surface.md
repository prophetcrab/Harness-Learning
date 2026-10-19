# 0056 分发形态：`pip install p4-harness` 后 `import harness`，启动器随 core 分发

- 日期：2026-10-19
- 阶段：P4_Coding（M3 起步）
- 状态：已采纳

## 背景

用户明确要求：

> 把启动的 launcher 做到 core 里（未来打包成 python 包，harness 开发只需要
> import python 包即可）。

对照 dsh：它的启动器在 `apps/cli` + `packages/boot`，框架包（vendored cordis）
不认识装配单。我们的 `core/launcher.py` 已经在 core 里，但还缺"能装、能 import"
的那层壳——没有 `[build-system]`、没有对外的 import 名、也没有验证"外部使用方
只靠公开面就能起一个世界"。

## 决策

**分发单元 = `core`（框架 + 引导）+ 随包 `plugins/base/loader.py`；对外名是
`harness`。**

```python
import harness

runtime = harness.boot(entries, catalog=my_catalog)      # 或
entries = harness.load_manifest("app.yaml", variables={...})
runtime = harness.boot(entries, catalog=my_catalog)
```

### 一、为什么门面叫 `harness` 而不是让用户 `import core`

`core` 是极通用的顶层名，装进 site-packages 后几乎必然与别人的包冲突。
`harness/__init__.py` 只做 re-export，不增加任何语义：
`harness.boot is core.boot`，有测试钉住。

### 二、为什么 loader 跟着分发

`boot()` 的第一件事是挂 loader——"先有鸡才能装鸡"。没有它，空装配单也起不来。
所以 loader 是**唯一**随 core 分发的插件；其余插件（base 的 llm / sessions /
agent…、capability 的全部）都是参考实现，使用方可以完全不用，只提供自己的
`catalog`。

`import harness` 不会加载任何插件：`core/launcher.py` 里的 plugins import 是
`boot()` 内的函数内懒加载，`test_purity.py` 与本次新增的
`test_import_harness_does_not_load_plugins` 都钉住这一点。

### 三、插件作者不用 import 这个包

插件协议是鸭子类型（`name` / `inject` / `optional` / `requires` / `setup`），
一个普通类就是插件。`harness` 是给**装配方**（组合根、产品入口、测试）用的。
这条让"第三方插件包"不需要依赖 `p4-harness` 的 API 面——只依赖协议形状，
版本耦合最小。

## 备选与排除理由

**A. 把 launcher 移出 core，像 dsh 那样放 `apps/cli`**。用户明确要"启动器做进
core、import 即用"；而且 Python 没有 dsh 那样的 profile 安装模型，多一层
`apps/` 只会增加使用方的心智负担。放弃。

**B. 让使用方 `import core`**。名字太泛，且会把"内部包名"固化成公开 API。
放弃。

**C. 只分发 core，把参考插件做成独立包**。现在是 M3，参考插件还在快速变化；
拆包会让每次改动都要同步版本。等 M4/M5 插件面稳定后再考虑拆
（`p4-harness-core` / `p4-harness-plugins`）。**暂缓**。

## 后果

- 新增 `harness/__init__.py`（纯 re-export）与 `[build-system]` /
  setuptools 包发现配置；`manifests/*.yaml` 作为 package-data 随包分发；
- 新增 `tests/test_package_surface.py`（6 个用例）：门面等价、import 不拖插件、
  空装配单能起、外部插件只用公开面、YAML 走公开面、异常类型公开；
- 实测 `pip wheel .` 构建成功，wheel 装进独立目录后
  `import harness; harness.boot([], catalog={})` 能起（`loader` 是唯一服务）；
- 全量 **506 用例**（500 + 6）通过；
- 打包产物（`build/` / `dist/` / `*.whl`）加入 `.gitignore`，不入库。

## 相关

- [0042](0042-p4-base-boundary.md)（启动器在 core 的边界）、
  [0054](0054-yaml-manifest.md)（装配单是数据）、
  [0053](0053-plugin-dependency-direction.md)（依赖方向）。

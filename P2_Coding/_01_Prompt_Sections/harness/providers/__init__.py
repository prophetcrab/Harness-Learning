"""harness.providers —— M5：能力接缝（三角色：Definition + Provider + Consumer）。

把 M2 里"能用"的文件/命令工具重构成可整体替换的能力接缝，体验"换 provider 换产品"。

规划：
- 抽象 `FileSystem`（read/write/edit/列表）与 `SubprocessService`（spawn/捕获）；
- 本地实现；工具只依赖抽象；
- 单槽服务：重复注册即报错；provider 选择在显式 resolve 步骤完成；
- 至少两个新 provider：`MemoryFS`（测试用）、`WorkspaceJailFS`（越出工作目录的写被拒）。

验收：同一套工具测试在 local 与 memory 下全绿；jail 越权错误结构与其他错误一致。
决策记录：0007-capability-seams。
"""

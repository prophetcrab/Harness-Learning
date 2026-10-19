"""fs 能力插件组：两层文件策略。

    sandbox.py             fs-sandbox             四种审批模式
    observation_policy.py  fs-observation-policy  修改文件前必须先读

两者都挂在 `fs/approve` 上，互不依赖；可以只装其中一个。
"""

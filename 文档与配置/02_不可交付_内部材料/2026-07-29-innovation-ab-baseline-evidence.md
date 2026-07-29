# 创新 A/B 实施前基线证据

- 冻结日期：2026-07-29
- Python 解释器：`source_package/.venv/Scripts/python.exe`
- 后端固定桩：`1064 passed, 22 deselected`
- 前端测试：`28 files, 278 tests passed`
- 前端生产构建：成功
- 状态转移数量：21
- 状态转移 ID：`T01`～`T21`

## 不可变文件 SHA-256

| 文件 | SHA-256 |
|---|---|
| `orchestrator/transitions.py` | `8a9bd911654fccba9c4365b50c1b8943619a5a3b6f2e4bd1dd4536fe166a02a0` |
| `orchestrator/engine.py` | `cbb01651215d3f179a49a73db0133472562ec82555a47eb7fdd8dda6c372cbf6` |
| `agents/review_agent.py` | `9b94879dc58285fb38dae2d224b4dfdc89cb2bf7666e96b12110373023fec8b8` |
| `agents/sandbox.py` | `c2660ba97ca2f926230d4c02edb27b88f0f75deb544c075c9fa06d772b3f8d4f` |
| `eval/metrics/adaptation.py` | `6d5c0d7cd10f2d7f531c427d228572fffae10c25257513394a057966c509c8b0` |
| `eval/metrics/coverage.py` | `0387a197049684eb57f26dfe2b9d9d81cbac1ba53b63c6d40b3b47df3de332d7` |
| `eval/metrics/hallucination.py` | `0e5c9dab94714a68ae567289e87a464f5be99c4c1326fc9b33745b70f67c1b6c` |
| `eval/metrics/summary.py` | `c012f47f38e617c886fc5918ad6c04854bd5e2bba6f376371415358336df4f21` |
| `eval/run_p7_evaluation.py` | `4baa52312b8eb7e8f85d5dc6d693bbdaf3f828be2a980a6efc962272a3b699cc` |

## 基线语义

- A0：R-02/R-03 每个生成循环均可执行模型辩护与复审；硬规则、未知规则和混合未知规则直接局部重生成；下一版使用泛化反馈。
- B0：同域、责任范围、未探查约束下，按总知识点支持数、切片支持数和领域误区顺序选择关联目标。
- 2～4轮追问、R-01～R-06、S-01～S-09、三指标算法和正式 runner 均作为禁止修改项。

后续 A0/B0 等价测试和禁止改动哈希门禁均以本文件及创新前 Git 标签为准。

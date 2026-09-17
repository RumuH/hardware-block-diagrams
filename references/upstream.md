# 改造来源

本 skill 的工具和硬件语义层是为当前任务编写的独立实现；复用了以下项目的绘图方法与工作流，不宣称上游已具备个人风格学习或这两类图的完整能力。

- rockyco/claude-drawio-plugin，commit `3d3aaeda0e8714f9ab9ef4828b57d7c360281601`，2026-09-16 检查。参考 `skills/drawio/SKILL.md`、`references/fpga-shapes.md`、`best-practices.md`。复用硬件分类、XML图元/绑定端点、显式折点、图像核对思路；替换扁平 parent、detached edge 和固定视觉规则。许可证：[MIT](upstream/rockyco-LICENSE)。源仓库 https://github.com/rockyco/claude-drawio-plugin
- icebird1998/drawio-scientific-illustrator，commit `9bbeca93ffd134a29bfc90023f22c65359efe584`，2026-09-16 检查。参考 `recreate-scientific-figure-in-drawio/SKILL.md` 的区域/容器/重复结构/叠放次序分解及实际图像核对方法；不移植 live MCP 工具链。许可证：[MIT](upstream/illustrator-LICENSE)。源仓库 https://github.com/icebird1998/drawio-scientific-illustrator

用户提供的 NPU 和 CPU 图是明确认可的个人参考，不是经过核实的芯片规格。本 skill 不分发其原图或把其中参数当成通用硬件事实。两个 bundled example 是代表性局部的验证重绘，用于检查画法和编辑能力，不是完整原图复刻或真实设计签核。

# Hardware Block Diagrams

面向 Codex 的硬件框图 skill。根据用户或上游 agent 提供的硬件结构和数据流，绘制 CPU 微架构、GPU/NPU 阵列、FPGA 和 SoC 组成图，输出可编辑的 **draw.io、SVG、PNG**。

重点表达模块包含关系、重复实例、共享资源、并行通路和反馈；不把所有硬件都画成顺序流程图。

## 能力与边界

- 将自然语言或 JSON/YAML 设计输入规范化为硬件描述，由 Codex 安排布局。
- 检查层级、端口方向、已知位宽与协议，以及部分几何问题。
- 生成原生 draw.io 容器和绑定连接，使用 draw.io Desktop 导出图片。
- 从获批样本提炼具名风格；从明确的用户改图追加版本，支持回退。
- 用户风格库保存在 `~/.codex/hardware-block-diagrams/`，与程序源码分离。

“学习”指模型观察参考图并保存构图规则和样式参数，不训练模型权重。脚本不自动识别图片、不证明 RTL 正确，也不能取代实际 PNG 视觉检查。布局由 Codex 生成，脚本不是通用自动布局引擎。

## 安装

需要 Python 3.10+。真实 SVG/PNG 导出需要 [draw.io Desktop](https://github.com/jgraph/drawio-desktop)。JSON 路线仅使用 Python 标准库；YAML 和辅助图片/PDF 处理依赖见 `requirements-optional.txt`。

在尚未存在同名 skill 目录时：

```bash
git clone https://github.com/RumuH/hardware-block-diagrams.git ~/.codex/skills/hardware-block-diagrams
```

已有安装时先保留本地改动，再更新，不要直接覆盖。个人学习库不在该 Git 仓库内。

使用示例：

> 使用 $hardware-block-diagrams，根据下面的模块层级和数据流绘制框图。包含两个 PE，每个 PE 内有寄存器与 ALU；DMA 与共享缓冲区双向连接，共享缓冲区分别与两个 PE 双向连接。不要补充未提供的位宽。

也可要求“学习我认可的参考图，保存为具名风格”或“按我修改后的图更新这个风格”。联网搜索和样本学习分别需要用户授权。

## 文件与命令

- [SKILL.md](SKILL.md)：Codex 工作流程。
- [输入和布局契约](references/interface-contract.md)：用户或上游 agent 的数据格式。
- [运行命令](references/commands.md)：验证、绘制、导入及风格库管理。
- [学习与反馈](references/learning.md)：审批、风格版本与回退。
- [来源及上游许可证](references/upstream.md)：参考项目与归属说明。

从仓库根目录验证示例：

```bash
python3 scripts/diagram.py validate examples/npu-composition.hardware.json --layout examples/npu-composition.layout.json
python3 scripts/diagram.py render examples/npu-composition.hardware.json --layout examples/npu-composition.layout.json --style assets/styles/npu-hierarchy.json --output /absolute/output/npu
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts
```

示例是构图验证用的简化设计，不是芯片规格或完整原图复刻。内置两种起始样式；用户原图、下载的第三方图片、个人学习库、研究仓库和工作区其他 skill 均不包含在发布内容中。内置样式的样本 ID 是来源标识，其他机器不会因此获得相应私人样本或审批记录。

## 反馈与改进

遇到问题或有建议，请到 **[反馈入口](https://github.com/RumuH/hardware-block-diagrams/issues/new/choose)** 选择“绘图或运行问题”“功能或使用建议”或“画风反馈与参考样本”。提交前可先查看 [已有反馈](https://github.com/RumuH/hardware-block-diagrams/issues)，相同问题可补充复现信息。

优先提供最小硬件描述、所用风格和版本、期望/实际结果，以及截图或可公开的复现文件。无需为反馈提供完整工程。风格参考图的使用意图在表单中单独说明。

维护者可以让 Codex “读取本仓库 issue #编号，复现问题并修复”。处理顺序为核对输入、复现、修正、针对性验证，并在提交或 PR 中关联 issue。涉及图形变化时需检查实际导出的 PNG。详见 [反馈处理说明](CONTRIBUTING.md)。

当前入口负责收集反馈，不会自动启动 Codex、修改代码或发布版本；自动巡检需单独配置。

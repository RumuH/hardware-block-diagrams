# 使用命令

以下相对路径以本 skill 目录为工作目录。Python 3.10+，JSON 不需要第三方包。YAML 可用安装了 PyYAML 的 Python。查找 Desktop：`drawio`/`draw.io` PATH、环境变量 `DRAWIO_DESKTOP`、Windows `LOCALAPPDATA\Programs\draw.io` 或 `Program Files\draw.io`，以及本机便携安装 `E:\KimiData\Tools\draw.io\draw.io.exe`；macOS `/Applications/draw.io.app` 或 `~/Applications/draw.io.app`。也可传 `--drawio`。

## 从示例开始绘图

```bash
python scripts/diagram.py validate examples/npu-composition.hardware.json --layout examples/npu-composition.layout.json
python scripts/diagram.py render examples/npu-composition.hardware.json --layout examples/npu-composition.layout.json --style assets/styles/npu-hierarchy.json --output /absolute/output/npu
python scripts/diagram.py render examples/cpu-microarchitecture.hardware.json --layout examples/cpu-microarchitecture.layout.json --style assets/styles/cpu-datapath.json --output /absolute/output/cpu
```

模板用于理解数据格式与构图，不是把新设计替换成示例里的 NPU/CPU。先编写本次 hardware/layout。`validate` 返回 0 表示无结构错误，警告仍需查看；2 为输入/结构错误。`render` 的 0 表示所有格式成功；3 表示导出不全，此时 native 和 manifest 仍可用于恢复。不要把程序通过称为“用户认可画风”。

每次输出：`PREFIX.drawio`、`.svg`、`.png`、`.hardware.json`、`.layout.json`、`.manifest.json`。用绝对 output 前缀使 manifest 文件链接便于其他 agent 使用。重新生成前保留用户手工编辑文件，输出到新前缀。

## 学习后的风格解析

```bash
python scripts/library.py list
python scripts/library.py show cpu-datapath --output /absolute/work/style.json
python scripts/diagram.py render /absolute/work/hardware.json --layout /absolute/work/layout.json --style /absolute/work/style.json --style-revision 1 --output /absolute/output/design
```

`show` stdout 包含实际 revision、样本元数据和 usable；`--output` 只写 renderer 能读取的 profile。上例 revision 1 仅是示例，必须使用 show 返回的真实 revision。不可把不可用版本通过 audit 参数取出后用于绘图。渲染器读取规则而不会执行它们；Kimi 必须先按 rules 设计 layout。

## 样本登记与批准

```bash
python scripts/library.py sample-add --id sample-id --file /absolute/reference.png --origin web --source 'https://original-source/page'
python scripts/library.py list samples
```

此时样本为 pending。只有用户真实批准具体样本之后才执行：

```bash
python scripts/library.py approve sample-id --evidence '引用用户批准这些具体样本的消息与日期'
```

拒绝使用 `reject sample-id --evidence '用户拒绝或撤回的消息'`。`--origin` 可为 `web`、`user-original`、`user-correction`、`generated`；最后一种不能批准学习。上面的 evidence 是填法说明，不能原样用来伪造审批。

## 保存与回退

Kimi 实际读图并提炼完整 style.json 后：

```bash
python scripts/library.py learn style-name --profile /absolute/style.json --samples sample-id --evidence '本次样本与可迁移规则的来源说明'
python scripts/library.py default style-name
python scripts/library.py rollback style-name 1
```

`learn` 追加版本并激活该版本，不覆盖旧文件；不自动更改默认风格名称。`default` 只在用户要求时使用。`rollback` 检查证据仍有效。`show --revision N --allow-inactive-evidence` 仅用于审计已失效历史。

## 导入用户改图

```bash
python scripts/diagram.py import /absolute/edited.drawio --output /absolute/work/edited
```

得到 `.import.json`，保留所有实际图元、样式和几何，以及原始嵌入 hardware/layout。**嵌入信息是原始快照，不一定代表当前手工编辑的内容。** 比较真实 cells/XML 和原始信息，维护新设计描述或提炼风格差异；不把导入等同自动恢复所有硬件事实。对图片改图用视觉比较并执行 learning.md 流程。

## 可移植性

脚本主功能仅使用 Python 标准库。YAML 输入需要 PyYAML；图像或 PDF 辅助处理可使用 Pillow、PyMuPDF（见 requirements-optional.txt），按实际需要安装到虚拟环境。PDF 读取可使用环境已有工具，缺少能力时说明问题，不伪称已看过。

## 先比较模块排布

复杂图先按 [placement.md](placement.md) 设计不同的模块排列，再处理路由。比较工具只读现有路径，不会移动模块或重算线；两个 layout 使用相同 hardware，避免通过删信号改善指标。

```bash
python scripts/compare_layouts.py /absolute/work/hardware.json --before /absolute/work/baseline.layout.json --after /absolute/work/candidate.layout.json --report /absolute/work/placement-comparison.json
```

报告列出移动/缩放的模块、改变的端口侧边、线长、折点、交叉/接触、共线重叠、穿框和节点重叠。指标不是单一排名，也不证明视觉合格；先看结构与碰撞，再比较长度、折点和画幅。带斜线的基线不能直接拿折点数和正交候选比优劣。返回 0 仅表示结构可比较，所有 warnings 和实际 PNG 仍需审查。

可复用的最小示例展示把完成队列及输出移到生产者附近，保留相同硬件连接：

```bash
python scripts/compare_layouts.py examples/placement-neighbors.hardware.json --before examples/placement-neighbors.before.layout.json --after examples/placement-neighbors.after.layout.json
python scripts/diagram.py render examples/placement-neighbors.hardware.json --layout examples/placement-neighbors.after.layout.json --style assets/styles/cpu-datapath.json --output /absolute/output/placement-after
```

另一个构图示例 `examples/shared-hub.*.json` 展示共享资源居中、请求汇聚与完成分派分居上下、访问单元分居两侧；可用相同 render 命令及 cpu-datapath 样式查看。

## 复杂连线整理

选定模块排列与接口侧边后，才使用固定节点布线工具生成独立候选：

```bash
python scripts/routing.py /absolute/work/hardware.json --layout /absolute/work/layout.json --output /absolute/work/routed.layout.json --report /absolute/work/routing-report.json
python scripts/diagram.py render /absolute/work/hardware.json --layout /absolute/work/routed.layout.json --style /absolute/work/style.json --output /absolute/output/design
```

默认 `--ports preserve` 保留指定端点比例。需要同侧端点整体均匀居中时加 `--ports spread`（输入输出一起计数）。`--clearance 16` 是默认障碍留白和折线路径端部直线段的最小长度，单位为画布坐标；宽箭头可增大。真正可直连的短距离无需绕折。

工具保持节点和硬件描述不变，重算显式折点。返回 0 表示得到可用正交路径候选，1 表示输入错误或部分边无解；失败时不发布部分布局，也不能继续使用上次遗留输出冒充本次结果。报告列出折点、交叉、重线及需要复核的标签。复杂图不保证全局最少交叉；自动结果还需视觉检查。

已有固定外侧通道可能被改变；保持原始布局。需要更少折点时先调整节点对齐，而不是强制 spread 后接受更多绕行。端部空间不足或父子边界连接方向不适合时，工具会报告无解，需要调整布局/端口侧边。

`render` 默认拒绝斜线；仅对用户明确要求的斜线构图使用 `--allow-nonorthogonal`。`validate` 仍保留诊断兼容行为，其返回 0 不代表所有走线警告已处理。

`examples/routing-alignment.*.json` 展示两条上沿连接的居中等距和外侧反馈，可使用 `assets/styles/cpu-datapath.json` 渲染。连线完整规则见 [routing.md](routing.md)。

连线文字过小时可在风格 tokens 中设置 `edge_font_size`，无需像旧项目那样导出后手工修改 XML；字号变化后仍需检查标签重叠。

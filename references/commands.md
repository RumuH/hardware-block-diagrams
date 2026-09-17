# 使用命令

以下相对路径以本 skill 目录为工作目录。Python 3.10+，JSON 不需要第三方包。YAML 可用安装了 PyYAML 的 Python。查找 Desktop：`drawio`/`draw.io` PATH、macOS `/Applications/draw.io.app` 或 `~/Applications/draw.io.app`，也可传 `--drawio`。

## 从示例开始绘图

```bash
python3 scripts/diagram.py validate examples/npu-composition.hardware.json --layout examples/npu-composition.layout.json
python3 scripts/diagram.py render examples/npu-composition.hardware.json --layout examples/npu-composition.layout.json --style assets/styles/npu-hierarchy.json --output /absolute/output/npu
python3 scripts/diagram.py render examples/cpu-microarchitecture.hardware.json --layout examples/cpu-microarchitecture.layout.json --style assets/styles/cpu-datapath.json --output /absolute/output/cpu
```

模板用于理解数据格式与构图，不是把新设计替换成示例里的 NPU/CPU。先编写本次 hardware/layout。`validate` 返回 0 表示无结构错误，警告仍需查看；2 为输入/结构错误。`render` 的 0 表示所有格式成功；3 表示导出不全，此时 native 和 manifest 仍可用于恢复。不要把程序通过称为“用户认可画风”。

每次输出：`PREFIX.drawio`、`.svg`、`.png`、`.hardware.json`、`.layout.json`、`.manifest.json`。用绝对 output 前缀使 manifest 文件链接便于其他 agent 使用。重新生成前保留用户手工编辑文件，输出到新前缀。

## 学习后的风格解析

```bash
python3 scripts/library.py list
python3 scripts/library.py show cpu-datapath --output /absolute/work/style.json
python3 scripts/diagram.py render /absolute/work/hardware.json --layout /absolute/work/layout.json --style /absolute/work/style.json --style-revision 1 --output /absolute/output/design
```

`show` stdout 包含实际 revision、样本元数据和 usable；`--output` 只写 renderer 能读取的 profile。上例 revision 1 仅是示例，必须使用 show 返回的真实 revision。不可把不可用版本通过 audit 参数取出后用于绘图。渲染器读取规则而不会执行它们；Codex 必须先按 rules 设计 layout。

## 样本登记与批准

```bash
python3 scripts/library.py sample-add --id sample-id --file /absolute/reference.png --origin web --source 'https://original-source/page'
python3 scripts/library.py list samples
```

此时样本为 pending。只有用户真实批准具体样本之后才执行：

```bash
python3 scripts/library.py approve sample-id --evidence '引用用户批准这些具体样本的消息与日期'
```

拒绝使用 `reject sample-id --evidence '用户拒绝或撤回的消息'`。`--origin` 可为 `web`、`user-original`、`user-correction`、`generated`；最后一种不能批准学习。上面的 evidence 是填法说明，不能原样用来伪造审批。

## 保存与回退

Codex 实际读图并提炼完整 style.json 后：

```bash
python3 scripts/library.py learn style-name --profile /absolute/style.json --samples sample-id --evidence '本次样本与可迁移规则的来源说明'
python3 scripts/library.py default style-name
python3 scripts/library.py rollback style-name 1
```

`learn` 追加版本并激活该版本，不覆盖旧文件；不自动更改默认风格名称。`default` 只在用户要求时使用。`rollback` 检查证据仍有效。`show --revision N --allow-inactive-evidence` 仅用于审计已失效历史。

## 导入用户改图

```bash
python3 scripts/diagram.py import /absolute/edited.drawio --output /absolute/work/edited
```

得到 `.import.json`，保留所有实际图元、样式和几何，以及原始嵌入 hardware/layout。**嵌入信息是原始快照，不一定代表当前手工编辑的内容。** 比较真实 cells/XML 和原始信息，维护新设计描述或提炼风格差异；不把导入等同自动恢复所有硬件事实。对图片改图用视觉比较并执行 learning.md 流程。

## 可移植性

脚本主功能仅使用 Python 标准库。YAML 输入需要 PyYAML；图像或 PDF 辅助处理可使用 Pillow、PyMuPDF（见 requirements-optional.txt），按实际需要安装到虚拟环境。PDF 读取可使用环境已有工具，缺少能力时说明问题，不伪称已看过。

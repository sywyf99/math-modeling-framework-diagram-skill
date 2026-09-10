# 数学建模论文框架图 Skill

这是一个面向 Codex 的“可执行工具包型 Skill”。你只需要提供赛题、解题方案或论文原文，它就能把内容整理为论文级数学建模框架图，并交付可继续编辑的源文件。

它不是单纯的提示词模板：仓库内包含确定性排版、格式转换、质量检查和论文原文一致性审核脚本。

## 能做什么

- 从自然语言、赛题或论文草稿提取节点、箭头、控制条件、评价指标和结论。
- 支持 `pipeline`、`comparison`、`swimlane`、`hub`、`hierarchy` 五类布局。
- 一次生成 SVG、PNG、原生可编辑 draw.io、Mermaid 和 JSON 规格。
- 自动检查文字溢出、节点碰撞、连线交叉、画布越界和可编辑性。
- 有论文原文时，检查节点、关系、步骤顺序、模型、公式、参数和结论是否与原文一致。
- 审核结果绑定原文与图规格的 SHA-256，能识别过期审核和伪造引文。
- 可把 `GLOBAL STYLE / LAYOUT / DETAILS` 一类旧式图片提示词压缩为可执行 JSON 草稿，并保留完整追溯报告。
- 支持青绿、蓝、薄荷绿、橙色系列主题和多问题图号，在统一视觉体系下分别成图。

## 最简单的使用方式

把本仓库安装为 Codex Skill 后，直接对 Codex 说：

> 用这篇论文生成一张 A4 横版的数学建模技术路线图，同时给我 PNG、SVG 和可编辑 draw.io；完成后检查图中逻辑是否与论文原文一致。

如果只提供赛题而没有论文原文，Skill 会生成框架图并做视觉、结构和格式检查，但不会声称“已经与论文一致”。

## 安装

将仓库克隆到 Codex 的个人 Skills 目录：

```powershell
git clone https://github.com/sywyf99/math-modeling-framework-diagram-skill.git "$env:USERPROFILE\.codex\skills\math-modeling-framework-diagram"
```

重新打开 Codex 任务后，可以显式调用 `$math-modeling-framework-diagram`，也可以直接描述“生成数学建模框架图”。

## 直接运行工具包

仓库根目录就是 Skill 目录。下面以仓库自带示例为输入：

```powershell
python .\scripts\diagram_toolkit.py validate `
  --spec .\references\examples\comparison-pipeline.json `
  --strict

python .\scripts\diagram_toolkit.py build `
  --spec .\references\examples\comparison-pipeline.json `
  --output-dir .\output `
  --name comparison_pipeline `
  --strict
```

主要输出包括：

| 文件 | 用途 |
|---|---|
| `*.spec.json` | 唯一规范源，后续修改优先改它 |
| `*.svg` | 矢量图，适合论文和排版软件 |
| `*.png` | 快速预览或直接插入文档 |
| `*.drawio` | 原生节点与连线，可在 draw.io 中编辑 |
| `*.mmd` | Mermaid 源文件 |
| `*.quality-report.json` | 自动布局与视觉质量报告 |

同名输出默认不会覆盖，会自动生成 `_v2`。只有确定要替换旧文件时才使用 `--overwrite`。

## 转换旧式框架图提示词

如果已有一份写给图片生成模型的长提示词，先转换为紧凑规格：

```powershell
python .\scripts\prompt_adapter.py `
  --input "flowchart_prompt_1.txt" `
  --output .\output\problem_1.spec.json `
  --series-id "lunar-transport" `
  --series-index 1 `
  --series-count 4 `
  --series-label "问题一"
```

转换器会把主模块和核心步骤放入规格，把三级细节与模糊连线保存在 `*.adaptation-report.json`。该结果仍是草稿：需要统一成论文语言、检查关系，并在有论文原文时继续执行一致性审核。

## 论文原文一致性自检

有论文或赛题原文时，推荐工作流是：

1. 从原文创建带定位信息的证据包。
2. 由 AI 逐项审核控制条件、每个节点、每条边和结论。
3. 验证审核引用、哈希绑定、遗漏项和无依据添加。
4. 不通过时修改 JSON 规格，重新构图并再次审核。

生成证据包：

```powershell
python .\scripts\logic_audit.py evidence `
  --spec "框架图.spec.json" `
  --source "论文.pdf" `
  --output .\output\framework.logic-evidence.json
```

先生成覆盖全部节点和边的 AI 审核模板：

```powershell
python .\scripts\logic_audit.py template `
  --spec "框架图.spec.json" `
  --evidence .\output\framework.logic-evidence.json `
  --output .\output\framework.logic-review.json
```

AI 重新阅读原文并按 `references/logic-review.schema.json` 填写审核结果后执行：

```powershell
python .\scripts\logic_audit.py verify `
  --spec "框架图.spec.json" `
  --evidence .\output\framework.logic-evidence.json `
  --review .\output\framework.logic-review.json `
  --output .\output\framework.logic-check.json
```

只有 `logic-check.json` 中的 `passed` 为 `true`，且成图已经实际查看，才能声称框架图与论文原文一致。

支持的原文格式：TXT、Markdown、LaTeX、CSV/TSV、JSON、DOCX 和带文字层的 PDF。扫描版 PDF 需要先做 OCR。

## 环境与依赖

- Python 3.10 或更高版本。
- 生成 SVG、draw.io、Mermaid 和 JSON 不要求额外第三方库。
- PNG 渲染会依次尝试 CairoSVG、Pillow、Chrome 或 Edge。
- PDF 文字提取需要 `pypdf`；在 Codex 桌面环境中也可使用其文档运行时作为后备。

可选依赖：

```powershell
python -m pip install pillow cairosvg pypdf
```

## 项目结构

```text
math-modeling-framework-diagram-skill/
├─ SKILL.md                         # Codex 入口与执行规则
├─ agents/openai.yaml               # Skill 的界面元数据
├─ assets/layout-presets.json       # 版式参数
├─ scripts/
│  ├─ diagram_toolkit.py            # 检查、排版与多格式构建
│  ├─ prompt_adapter.py              # 旧式长提示词的结构化压缩转换
│  ├─ logic_audit.py                # 原文证据与逻辑审核验证
│  └─ render_png.py                 # PNG 渲染后备
└─ references/
   ├─ visual-system.md              # 论文级视觉系统
   ├─ diagram-spec.md               # JSON 规格与命令
   ├─ diagram-spec.schema.json      # 图规格 Schema
   ├─ prompt-adaptation.md           # 长提示词转换、压缩与审查规则
   ├─ logic-audit.md                # 一致性自检流程
   ├─ logic-review.schema.json      # AI 审核 Schema
   └─ examples/                     # 可运行示例
```

## 隐私边界

工具默认在本地处理文件。除非你明确要求，不会把论文、赛题数据、生成图或审核证据上传到网站；本仓库也不包含之前测试使用的论文样本和临时输出。

## 进一步阅读

- [Skill 执行说明](SKILL.md)
- [论文级视觉系统](references/visual-system.md)
- [可执行规格与命令](references/diagram-spec.md)
- [论文原文一致性自检](references/logic-audit.md)

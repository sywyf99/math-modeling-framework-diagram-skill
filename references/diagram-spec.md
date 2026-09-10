# 可执行规格与命令

当任务使用内置执行器时阅读本文。JSON 规格是语义唯一来源；修改图时先修改规格，再重新生成其他格式。

## 最小规格

```json
{
  "title": "问题一：需求预测与库存优化框架",
  "layout": "pipeline",
  "canvas": "a4-landscape",
  "theme": "blue",
  "series": {"id": "competition-2026", "index": 1, "count": 4, "label": "问题一"},
  "nodes": [
    {
      "id": "data_input",
      "title": "数据输入",
      "kind": "input",
      "items": ["历史销量", "价格与促销", "库存约束"]
    },
    {
      "id": "forecast",
      "title": "需求预测",
      "kind": "model",
      "items": ["构造时间特征", "训练预测模型", "滚动交叉验证"]
    }
  ],
  "edges": [
    {"from": "data_input", "to": "forecast", "label": "特征矩阵"}
  ]
}
```

完整字段约束见 [diagram-spec.schema.json](diagram-spec.schema.json)。可直接改写的比较型示例见 [examples/comparison-pipeline.json](examples/comparison-pipeline.json)。

## 顶层字段

| 字段 | 是否必需 | 含义 |
|---|---:|---|
| `title` | 是 | 主标题，建议不超过 50 个字符 |
| `subtitle` | 否 | 实验边界或补充说明 |
| `layout` | 否 | 默认 `pipeline`；还支持 `comparison`、`swimlane`、`hub`、`hierarchy` |
| `canvas` | 否 | 默认 `a4-landscape`；答辩使用 `16x9` |
| `theme` | 否 | 主色主题：`journal`、`teal`、`blue`、`mint`、`orange` |
| `series` | 否 | 同一赛题多图的系列 ID、当前序号、总数和短标签 |
| `control` | 否 | 共享输入、统一样本、约束、求解器和停止准则 |
| `nodes` | 是 | 1–40 个节点；超过 16 个时优先考虑复杂图工具 |
| `edges` | 否 | 有向边；省略时按节点顺序自动连接 |
| `result` | 否 | 结论、推荐方案、限制或输出 |
| `lanes` | 泳道必需 | 泳道 ID 和标题 |
| `center` | 中心图可选 | `hub` 的中心节点 ID；省略时选择第一个模型节点 |
| `audit` | 原文自检可选 | 论文必须出现的全局术语，以及自检备注 |

## 节点和边

节点必须有 `id` 和 `title`。`id` 只能使用英文字母开头，并继续使用英文字母、数字、下划线或连字符。可选字段：

- `step`：显示用编号，例如 `01`、`Q1`；省略时自动编号。
- `kind`：控制语义颜色。可用 `input`、`condition`、`data`、`model`、`method`、`process`、`evaluation`、`validation`、`result`、`conclusion`、`default`。
- `items`：正文短项，建议 3–5 项，每项只表达一个信息点。
- `lane`：仅泳道布局使用，值必须对应 `lanes[].id`。
- `source_terms`：原文中实际出现、用于绑定证据的模型名、变量、公式片段、参数值或结论短语。

边使用 `from`、`to` 和可选 `label`。显然的顺序关系不写标签；数据类型、约束传递或跨问题依赖可以写短标签。有论文原文时，可给关键边增加 `source_terms`，用于检索能够证明这条关系的原文短语。

省略 `edges` 时会按节点顺序自动连接；显式写 `"edges": []` 表示节点并行、不得自动串联。`control.to` 可指定条件带作用的入口节点，`result.from` 可指定汇入结果带的节点；省略时分别选择图的根节点和汇节点。

`series` 示例：

```json
{"id": "lunar-transport", "index": 2, "count": 4, "label": "问题二"}
```

同一 `series.id` 应保持相同 `canvas`、布局密度、字号和术语风格。不同题可使用不同 `theme`，但输入、评价、结论等语义色仍具有更高优先级。

用户提供了论文、赛题原文或解题文档时，构建完成后继续执行 [论文原文一致性自检](logic-audit.md)。

## 精确命令

只检查规格和自动布局：

```powershell
python "<技能目录>\scripts\diagram_toolkit.py" validate `
  --spec "<规格文件.json>" `
  --strict
```

生成默认全部格式：

```powershell
python "<技能目录>\scripts\diagram_toolkit.py" build `
  --spec "<规格文件.json>" `
  --output-dir "<输出目录>" `
  --name "problem_2_framework" `
  --strict
```

只生成指定格式：

```powershell
python "<技能目录>\scripts\diagram_toolkit.py" build `
  --spec "<规格文件.json>" `
  --output-dir "<输出目录>" `
  --formats "svg,drawio,mmd"
```

参数说明：

- `--formats`：`svg,png,drawio,mmd` 的任意组合；规格副本和质检报告始终生成。
- `--png-scale`：PNG 分辨率倍率，默认 `1.5`。
- `--strict`：任何自动警告都使构建不通过；正式交付默认使用。
- `--overwrite`：覆盖同名文件；只有用户明确要求覆盖时使用。
- 环境变量 `FRAMEWORK_DIAGRAM_BROWSER`：可显式指定 Chrome/Edge/Chromium 可执行文件。

## 报告解释和修复顺序

`*.quality-report.json` 包含：

- `passed`：自动质检是否通过；
- `errors`：无效 ID、缺失节点、重叠、越界、格式损坏或 PNG 失败；
- `warnings`：标题过长、正文过密、节点过多或画布利用率不足；
- `metrics`：画布、节点和边数量、主图利用率、PNG 尺寸、draw.io 可编辑节点数；
- `outputs`：生成文件的绝对路径。

修复顺序：先修语义和缺失关系，再压缩文本，然后调整布局类型，最后才考虑复杂图工具。不要通过缩小到难以阅读的字号消除警告。

## 输出结构

一次成功构建通常得到：

```text
problem_2_framework.spec.json
problem_2_framework.svg
problem_2_framework.png
problem_2_framework.drawio
problem_2_framework.mmd
problem_2_framework.quality-report.json
```

其中 `.spec.json` 是后续修改入口；`.drawio` 是原生 mxGraph 节点和边；`.svg` 用于论文；`.png` 用于预览；`.mmd` 便于快速查看语义关系。

有原文时还必须生成 `.logic-evidence.json`、`.logic-review.json` 和最终 `.logic-check.json`；旧审核不能用于已经修改的规格或原文。

旧式图片提示词先按 [旧式框架图提示词转换](prompt-adaptation.md) 生成规格草稿和 `.adaptation-report.json`，再进入上述验证与构建命令。

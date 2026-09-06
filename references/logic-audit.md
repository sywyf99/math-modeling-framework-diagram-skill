# 论文原文一致性自检

用户提供了论文、赛题原文、解题方案或正文草稿时执行本文。自检采用两层机制：程序负责检索和绑定证据，AI 负责语义判断。关键词匹配只能提示风险，不能单独证明逻辑一致。

## 支持的原文

- 直接读取：TXT、Markdown、LaTeX、CSV、TSV、JSON。
- 原生解析：DOCX，证据位置标记到段落。
- PDF：优先使用当前 Python 的 `pypdf`，缺失时自动调用 Codex 图文运行时；证据位置标记到页码和行块。
- 扫描版 PDF 没有文字层时，先按 PDF 工作流完成 OCR，再把 OCR 文本作为原文输入。

可以重复传入 `--source`，同时检查正文、附录和补充材料。不得把网络搜索结果当作用户论文原文，除非用户明确指定该网页就是审查对象。

## 一、为规格增加原文锚点

在从原文制作 JSON 时，为关键实体和关系添加 `source_terms`。这里放原文实际使用的模型名、算法名、变量、公式片段、参数值或结论短语，不放 AI 自己的概括。

```json
{
  "audit": {
    "required_terms": ["LSTM", "滚动时间窗", "RMSE"]
  },
  "nodes": [
    {
      "id": "forecast",
      "title": "需求预测",
      "kind": "model",
      "items": ["LSTM 预测", "滚动时间窗验证"],
      "source_terms": ["LSTM", "滚动时间窗"]
    }
  ],
  "edges": [
    {
      "from": "forecast",
      "to": "inventory",
      "label": "预测需求",
      "source_terms": ["将预测需求作为库存优化模型的输入"]
    }
  ]
}
```

`source_terms` 是确定性证据检索的硬锚点。未找到可靠匹配会被程序直接列为错误。没有写 `source_terms` 的标题和正文仍会自动检索，但只能作为提示。

## 二、生成原文证据包

```powershell
python "<技能目录>\scripts\logic_audit.py" evidence `
  --spec "<输出目录>\problem_1_framework.spec.json" `
  --source "<论文原文.pdf>" `
  --output "<输出目录>\problem_1_framework.logic-evidence.json"
```

多份原文：

```powershell
python "<技能目录>\scripts\logic_audit.py" evidence `
  --spec "<框架图.spec.json>" `
  --source "<论文正文.docx>" `
  --source "<附录.md>" `
  --output "<框架图.logic-evidence.json>"
```

命令同时生成同名 `.md` 摘要。先解决 `automatic_checks.errors`；`warnings` 必须由下一步语义审核判断，不能直接忽略。证据包的 `spec_sha256` 和 `source_fingerprint` 防止规格或原文修改后沿用旧审核。

## 三、生成并填写 AI 语义审核

先生成覆盖全部实体和边的模板：

```powershell
python "<技能目录>\scripts\logic_audit.py" template `
  --spec "<框架图.spec.json>" `
  --evidence "<框架图.logic-evidence.json>" `
  --output "<框架图.logic-review.json>"
```

AI 必须重新阅读原文相关页段，然后填写 [logic-review.schema.json](logic-review.schema.json)。逐项执行：

1. **内容覆盖**：每个控制条件、节点和结论是否能由原文支持。
2. **术语与公式**：模型名、算法名、变量、下标、单位、参数值、目标函数和约束是否一致。
3. **方向与因果**：每条箭头表示的是数据输入、先后依赖、因果、反馈还是比较；方向是否有依据。
4. **步骤顺序**：预处理、建模、求解、检验、评价和结论是否被错误提前或倒置。
5. **重要遗漏**：原文决定模型含义的假设、约束、验证、边界条件或负面结论是否没进入图。
6. **无依据添加**：图里是否出现原文没有采用的模型、推断、指标、参数或推荐结论。

判断规则：

- `supported`：原文明确支持，附证据位置和不超过 220 字的短摘录。
- `partial`：只有一部分成立、表述过度或缺少关键条件。
- `unsupported`：原文没有依据或与原文冲突。
- 关系 `supported`：箭头方向和关系类型均成立；只有两个节点分别出现并不足以证明箭头成立。
- `verdict: pass` 只允许在所有实体和关系均为 `supported`，且 `omissions`、`fabrications`、`required_revisions` 均为空时使用。

不要把证据包的模糊匹配分数直接改写成审核结论；它只帮助定位原文。

## 四、验证审核完整性

```powershell
python "<技能目录>\scripts\logic_audit.py" verify `
  --spec "<框架图.spec.json>" `
  --evidence "<框架图.logic-evidence.json>" `
  --review "<框架图.logic-review.json>" `
  --output "<框架图.logic-check.json>"
```

只有命令退出成功且 `logic-check.json` 中 `passed` 为 `true`，才能交付并声称图与原文一致。

如果未通过：

1. 根据 `required_revisions`、`omissions` 和 `fabrications` 修改规范源；
2. 重新运行框架图构建；
3. 重新生成证据包和审核模板；
4. 重新阅读原文并审核；
5. 最多三轮仍存在歧义时，把争议节点、关系和原文位置交给用户决定。

任何规格或原文变化都会改变摘要，旧审核会被验证器拒绝，不能复用。

## 五、交付文件

有原文时，在原有图文件之外增加：

```text
problem_1_framework.logic-evidence.json
problem_1_framework.logic-evidence.md
problem_1_framework.logic-review.json
problem_1_framework.logic-check.json
```

最终回复给出总判断，并只列出会影响论文含义的修改或仍需用户确认的争议。

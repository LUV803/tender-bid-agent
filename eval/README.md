# 检索评测集

`eval/` 存放可复现的招标检索评测数据。评测只读取已解析产物，默认使用
`lexical` 模式，不调用嵌入或 LLM 服务，因此可以在 CI 中离线运行。

```text
eval/
├── demo_queries.json           # 演示评测集（已脱敏，可提交）
├── demo_parsed/                # 配套的 5 页演示解析产物
│   └── demo-tender/
│       ├── manifest.json
│       ├── content.md
│       ├── raw/sections.json
│       └── extraction.json
└── private/                    # 真实项目评测集（git 忽略）
```

## 快速运行

```bash
# 演示集，不依赖 Redis / Chroma / 外部 API
uv run python -m bidding evaluate \
  eval/demo_queries.json \
  --parsed-dir eval/demo_parsed \
  --mode lexical

# 自己的真实文档，默认读取 upload/parsed/<doc_id>
uv run python -m bidding evaluate eval/private/my_tender.json --mode lexical

# 有 Chroma 索引时使用混合检索，并保存逐条结果
uv run python -m bidding evaluate \
  eval/private/my_tender.json \
  --mode auto \
  --json-out eval/private/report.json
```

演示集当前基线（8 条查询，`lexical`，`k=5`）：

| Hit@1 | Hit@3 | Recall@5 | MRR |
| --- | --- | --- | --- |
| 62.5% | 100.0% | 93.8% | 0.812 |

这是一组**回归基线**，用于防止检索排序退化，不代表真实招标文件上的效果。
真实项目的指标应根据人工标注结果单独记录，不要把演示集数字写进简历或对外宣传。

## 评测集格式

```json
{
  "doc_id": "demo-tender",
  "description": "可选的说明",
  "queries": [
    {
      "id": "price-score-table",
      "query": "投标报价得分怎么算",
      "relevant": [
        {"kind": "table", "table_label": "表10", "page": 2}
      ]
    }
  ]
}
```

`relevant` 中的每一项表示一个**可接受结果**。一条检索结果必须同时满足该项里的
所有约束才算命中：

- `kind`：结果类型，如 `table`、`section`、`qualification`、`review`、`factor`；
- `page`：结果页码范围必须包含该页；
- `table_label`：表号，如 `表10`；
- `title_contains`：标题包含该文本（忽略空格和大小写）。

同一条查询可以有多个 `relevant` 项，`Recall@k` 衡量前 k 条结果覆盖了多少项。
建议先准备 20–30 条真实问题，覆盖评分办法、资格审查、施工组织、日期、合同等
不同意图，并逐条对照 PDF 校验页码和表号。

## 指标

- `Hit@k`：前 k 条结果中是否至少有一条命中；
- `Recall@k`：前 k 条结果覆盖的标注项比例；
- `MRR`：第一条命中结果的倒数排名（截断到 k）；
- `Top-1`：第一条结果是否命中；
- `P95`：单次查询耗时（仅作观察，不作为 CI 阈值）。

CI 会运行演示集，并用 `--min-hit-at-3`、`--min-recall-at-k`、`--min-mrr` 等参数
设置最低阈值；指标低于阈值时命令以非零状态退出。

## 数据安全

真实招标文件、投标资料和人工标注可能包含敏感信息。请把真实评测集放在
`eval/private/`，或使用 `*.local.json` 后缀；这两类文件以及生成的 `_*.json`
报告都已被 `.gitignore` 排除，不要提交到公共仓库。

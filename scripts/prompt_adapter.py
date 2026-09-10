#!/usr/bin/env python3
"""Convert legacy image-generation flowchart prompts into compact diagram specs.

The adapter is intentionally conservative. It extracts modules, core steps and
declared connections, but keeps the original prompt as untrusted source
material. The result is a draft specification that still requires semantic
review before it is presented as a paper-grounded diagram.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


MAIN_RE = re.compile(r"MAIN STRUCTURE\s*\(([^:()]+):\s*([^)]+)\)", re.IGNORECASE)
ROW_RE = re.compile(r"^\s*-\s*(Top|Middle|Bottom) row\s*:", re.IGNORECASE)
MODULE_HINT_RE = re.compile(r"^\s*[•*-]\s*(?:Left|Middle|Right|Single)\s+(?:sub-)?module\b", re.IGNORECASE)
ICON_TITLE_RE = re.compile(r"^\s*[•*-]\s*\[[^\]]+\]\s*[\"“]([^\"”]+)[\"”]")
STEP_RE = re.compile(r"^\s*\d+\.\s*[\"“]([^\"”]+)[\"”]")
SUBSTEPS_RE = re.compile(r"^\s*-\s*Sub-steps\s*:\s*(.+)$", re.IGNORECASE)
FLOW_RE = re.compile(
    r"^\s*[•*-]\s*(.+?)\s*(?:→|->)\s*(.+?)\s*\(\s*arrow\s*:\s*[\"“]([^\"”]+)[\"”]\s*\)\s*$",
    re.IGNORECASE,
)
QUOTED_RE = re.compile(r"[\"“]([^\"”]+)[\"”]")
ID_CLEAN_RE = re.compile(r"[^a-z0-9]+")

VERB_PREFIXES = (
    "account for",
    "coordinate with",
    "coordinating with",
    "scheduling",
    "planning",
    "optimizing",
    "calculate",
    "evaluate",
    "analyze",
    "estimate",
    "confirm",
    "assess",
    "verify",
    "define",
    "develop",
    "compute",
    "identify",
    "select",
    "modify",
    "update",
    "inject",
    "model",
    "rank by",
    "rank",
    "set",
    "based on",
)


def load_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"无法识别文本编码：{path}")


def compact_phrase(value: str) -> str:
    value = " ".join(value.strip().split())
    lowered = value.lower()
    for prefix in VERB_PREFIXES:
        marker = prefix + " "
        if lowered.startswith(marker):
            value = value[len(marker) :].strip()
            break
    return value[:1].upper() + value[1:] if value else value


def slug(value: str, used: set[str]) -> str:
    base = ID_CLEAN_RE.sub("_", value.lower()).strip("_")
    if not base or not base[0].isalpha():
        base = "module"
    candidate = base[:48]
    suffix = 2
    while candidate in used:
        candidate = f"{base[:42]}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def parse_quoted_list(value: str) -> list[str]:
    found = QUOTED_RE.findall(value)
    return [compact_phrase(item) for item in found]


def parse_prompt(text: str) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    main_match = MAIN_RE.search(text)
    if main_match:
        theme_name = main_match.group(1).strip().lower()
        title = main_match.group(2).strip()
    else:
        theme_name = "journal"
        title = "Imported framework diagram"
        warnings.append("未识别 MAIN STRUCTURE 标题，已使用占位标题。")

    theme_aliases = {
        "teal": "teal",
        "blue": "blue",
        "mint": "mint",
        "orange": "orange",
        "journal": "journal",
    }
    theme = theme_aliases.get(theme_name, "journal")
    if theme == "journal" and theme_name != "journal":
        warnings.append(f"未识别主题色 {theme_name!r}，已回退到 journal。")

    modules: list[dict[str, Any]] = []
    current_row = "top"
    current: dict[str, Any] | None = None
    current_step: dict[str, Any] | None = None
    expect_title = False
    flows: list[dict[str, str]] = []
    in_flows = False

    for raw_line in text.splitlines():
        row_match = ROW_RE.match(raw_line)
        if row_match:
            current_row = row_match.group(1).lower()
            current = None
            current_step = None
            expect_title = True
            continue
        if "Flow connections" in raw_line:
            in_flows = True
            current = None
            current_step = None
            continue
        if raw_line.strip().startswith("DETAILS:"):
            in_flows = False
            continue
        if in_flows:
            flow_match = FLOW_RE.match(raw_line)
            if flow_match:
                flows.append(
                    {
                        "from": flow_match.group(1).strip(),
                        "to": flow_match.group(2).strip(),
                        "label": flow_match.group(3).strip(),
                    }
                )
            continue
        if MODULE_HINT_RE.match(raw_line):
            expect_title = True
            current = None
            current_step = None
            continue
        title_match = ICON_TITLE_RE.match(raw_line)
        if title_match and (expect_title or current is None):
            current = {"row": current_row, "title": title_match.group(1).strip(), "steps": []}
            modules.append(current)
            current_step = None
            expect_title = False
            continue
        if current is None:
            continue
        step_match = STEP_RE.match(raw_line)
        if step_match:
            current_step = {"title": compact_phrase(step_match.group(1)), "substeps": []}
            current["steps"].append(current_step)
            continue
        substeps_match = SUBSTEPS_RE.match(raw_line)
        if substeps_match and current_step is not None:
            current_step["substeps"] = parse_quoted_list(substeps_match.group(1))

    if not modules:
        raise ValueError("没有从提示词中识别到任何模块。")
    if any(not module["steps"] for module in modules):
        warnings.append("至少一个模块没有识别到核心步骤，请人工核对缩进和引号格式。")
    return {"title": title, "theme": theme, "modules": modules, "flows": flows}, warnings


def name_score(query: str, candidate: str) -> float:
    query_norm = re.sub(r"[^a-z0-9]+", " ", query.lower()).strip()
    candidate_norm = re.sub(r"[^a-z0-9]+", " ", candidate.lower()).strip()
    query_terms = set(query_norm.split())
    candidate_terms = set(candidate_norm.split())
    overlap = len(query_terms & candidate_terms) / max(1, len(query_terms | candidate_terms))
    sequence = difflib.SequenceMatcher(None, query_norm, candidate_norm).ratio()
    return max(overlap, sequence)


def resolve_module(name: str, modules: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    ranked = sorted(((name_score(name, module["title"]), module) for module in modules), key=lambda pair: pair[0], reverse=True)
    return (ranked[0][1], ranked[0][0]) if ranked else (None, 0.0)


def make_spec(parsed: dict[str, Any], series: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    modules = parsed["modules"]
    bottom = [module for module in modules if module["row"] == "bottom"]
    result_module = bottom[-1] if bottom else None
    normal_modules = [module for module in modules if module is not result_module]
    used: set[str] = set()
    module_ids: dict[int, str] = {}
    nodes: list[dict[str, Any]] = []
    for index, module in enumerate(normal_modules):
        node_id = slug(module["title"], used)
        module_ids[id(module)] = node_id
        kind = "evaluation" if any(word in module["title"].lower() for word in ("assessment", "evaluation")) else "process"
        if any(word in module["title"].lower() for word in ("optimization", "model")):
            kind = "model"
        nodes.append(
            {
                "id": node_id,
                "step": f"{index + 1:02d}",
                "title": module["title"],
                "kind": kind,
                "items": [step["title"] for step in module["steps"][:5]],
            }
        )

    spec: dict[str, Any] = {
        "title": parsed["title"],
        "subtitle": "",
        "layout": "hierarchy",
        "canvas": "a4-landscape",
        "theme": parsed["theme"],
        "nodes": nodes,
        "edges": [],
    }
    if series:
        spec["series"] = series

    if result_module is not None:
        spec["result"] = {
            "title": result_module["title"],
            "items": [step["title"] for step in result_module["steps"][:6]],
            "from": [],
        }

    warnings: list[str] = []
    unresolved: list[dict[str, Any]] = []
    for flow in parsed["flows"]:
        source_module, source_score = resolve_module(flow["from"], modules)
        target_module, target_score = resolve_module(flow["to"], modules)
        if source_module is None or target_module is None or min(source_score, target_score) < 0.43:
            unresolved.append({**flow, "source_score": round(source_score, 3), "target_score": round(target_score, 3)})
            continue
        if source_module is result_module:
            unresolved.append({**flow, "reason": "结论带不能作为普通边的起点"})
            continue
        source_id = module_ids.get(id(source_module))
        if target_module is result_module and source_id and "result" in spec:
            spec["result"]["from"].append(source_id)
            continue
        target_id = module_ids.get(id(target_module))
        if source_id and target_id:
            edge = {"from": source_id, "to": target_id, "label": flow["label"]}
            if edge not in spec["edges"]:
                spec["edges"].append(edge)

    suppressed: list[dict[str, Any]] = []
    edge_pairs = {(edge["from"], edge["to"]) for edge in spec["edges"]}
    redundant_pairs: set[tuple[str, str]] = set()
    for source, target in edge_pairs:
        for middle in (node["id"] for node in nodes):
            if (source, middle) in edge_pairs and (middle, target) in edge_pairs:
                redundant_pairs.add((source, target))
                break
    if redundant_pairs:
        kept_edges: list[dict[str, Any]] = []
        for edge in spec["edges"]:
            if (edge["from"], edge["to"]) in redundant_pairs:
                suppressed.append({**edge, "reason": "存在两步等价路径，主图省略跨级重复箭头"})
            else:
                kept_edges.append(edge)
        spec["edges"] = kept_edges

    indegree = {node["id"]: 0 for node in nodes}
    outdegree = {node["id"]: 0 for node in nodes}
    for edge in spec["edges"]:
        indegree[edge["to"]] += 1
        outdegree[edge["from"]] += 1
    is_chain = (
        len(nodes) <= 4
        and len(spec["edges"]) == max(0, len(nodes) - 1)
        and all(value <= 1 for value in indegree.values())
        and all(value <= 1 for value in outdegree.values())
    )
    if len(nodes) > 1 and not spec["edges"]:
        spec["layout"] = "comparison"
    elif is_chain:
        spec["layout"] = "pipeline"

    if result_module is not None and not spec["result"]["from"]:
        outgoing = {edge["from"] for edge in spec["edges"]}
        spec["result"]["from"] = [node["id"] for node in nodes if node["id"] not in outgoing]
    if unresolved:
        warnings.append("部分提示词连线无法可靠映射，已保留在转换报告中等待人工判断。")

    detail_map = [
        {
            "row": module["row"],
            "title": module["title"],
            "core_steps": module["steps"],
            "rendered_as": "result" if module is result_module else module_ids[id(module)],
        }
        for module in modules
    ]
    report = {
        "passed": not unresolved,
        "status": "draft_requires_semantic_review",
        "extracted": {
            "module_count": len(modules),
            "node_count": len(nodes),
            "edge_count": len(spec["edges"]),
            "theme": parsed["theme"],
        },
        "transformations": [
            "图像生成式样要求被转换为确定性矢量主题，不保留依赖图片模型的字体承诺。",
            "同级模块保持网格对齐，但不强制不同层级模块等高。",
            "三级子步骤保存在报告中，主图默认只显示核心步骤，防止形成卡片墙。",
            "结论模块转换为全宽结果带，并显式记录汇入节点。",
        ],
        "warnings": warnings,
        "unresolved_connections": unresolved,
        "suppressed_connections": suppressed,
        "traceability": detail_map,
    }
    return spec, report


def main() -> int:
    parser = argparse.ArgumentParser(description="把旧式框架图图片提示词转换为紧凑的可执行 JSON 草稿。")
    parser.add_argument("--input", type=Path, required=True, help="旧提示词 TXT 文件。")
    parser.add_argument("--output", type=Path, required=True, help="输出的 *.spec.json。")
    parser.add_argument("--report", type=Path, help="转换报告；默认与规格同名 *.adaptation-report.json。")
    parser.add_argument("--series-id", help="多图系列 ID。")
    parser.add_argument("--series-index", type=int, help="当前图序号，从 1 开始。")
    parser.add_argument("--series-count", type=int, help="系列总图数。")
    parser.add_argument("--series-label", default="", help="显示在图头右侧的系列短标签。")
    args = parser.parse_args()

    series_values = (args.series_id, args.series_index, args.series_count)
    if any(value is not None for value in series_values) and not all(value is not None for value in series_values):
        parser.error("--series-id、--series-index 和 --series-count 必须同时提供。")
    if args.series_index is not None and (args.series_index < 1 or args.series_count < args.series_index):
        parser.error("系列序号必须满足 1 <= index <= count。")
    series = None
    if args.series_id is not None:
        series = {
            "id": args.series_id,
            "index": args.series_index,
            "count": args.series_count,
            "label": args.series_label,
        }

    try:
        parsed, parser_warnings = parse_prompt(load_text(args.input))
        spec, report = make_spec(parsed, series)
        report["source"] = str(args.input.resolve())
        report["warnings"] = parser_warnings + report["warnings"]
        args.output.parent.mkdir(parents=True, exist_ok=True)
        report_path = args.report or args.output.with_name(args.output.name.replace(".spec.json", "") + ".adaptation-report.json")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"转换失败：{exc}", file=sys.stderr)
        return 2
    print(f"已生成结构化草稿：{args.output}")
    print(f"转换报告：{report_path}")
    print("注意：输出仍需语义审核，不能据此声称与论文原文一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

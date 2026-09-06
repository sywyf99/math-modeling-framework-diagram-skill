#!/usr/bin/env python3
"""Audit a framework diagram against its source paper.

This helper performs deterministic evidence retrieval and validates a separate
AI-authored semantic review.  It deliberately does not claim that lexical
matching alone proves semantic consistency.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
MAX_EXCERPT = 220
SUPPORTED_TEXT = {".txt", ".md", ".markdown", ".tex", ".csv", ".tsv"}

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} 的根元素必须是 JSON 对象。")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def decode_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def compact_whitespace(value: str) -> str:
    return " ".join(value.replace("\u00a0", " ").split())


def split_blocks(text: str, source_name: str, location_prefix: str = "行") -> list[dict[str, Any]]:
    lines = text.splitlines()
    blocks: list[dict[str, Any]] = []
    buffer: list[str] = []
    start_line = 1

    def flush(end_line: int) -> None:
        nonlocal buffer, start_line
        value = compact_whitespace(" ".join(buffer))
        if value:
            for part_index, part in enumerate(chunk_text(value), start=1):
                suffix = f"·片段{part_index}" if len(value) > 520 else ""
                blocks.append(
                    {
                        "location": f"{source_name}:{location_prefix}{start_line}-{end_line}{suffix}",
                        "text": part,
                    }
                )
        buffer = []

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            if buffer:
                flush(line_number - 1)
            start_line = line_number + 1
            continue
        if not buffer:
            start_line = line_number
        buffer.append(stripped)
        if sum(len(item) for item in buffer) >= 520:
            flush(line_number)
            start_line = line_number + 1
    if buffer:
        flush(len(lines))
    return blocks


def chunk_text(value: str, maximum: int = 520, overlap: int = 60) -> list[str]:
    value = compact_whitespace(value)
    if len(value) <= maximum:
        return [value]
    chunks: list[str] = []
    start = 0
    while start < len(value):
        end = min(len(value), start + maximum)
        if end < len(value):
            boundary = max(value.rfind(mark, start + maximum // 2, end) for mark in "。！？；.!?;")
            if boundary > start:
                end = boundary + 1
        chunks.append(value[start:end])
        if end >= len(value):
            break
        start = max(start + 1, end - overlap)
    return chunks


def extract_docx(path: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        xml_bytes = archive.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    blocks: list[dict[str, Any]] = []
    paragraph_number = 0
    for paragraph in root.iter(namespace + "p"):
        value = compact_whitespace("".join(node.text or "" for node in paragraph.iter(namespace + "t")))
        if not value:
            continue
        paragraph_number += 1
        for part_index, part in enumerate(chunk_text(value), start=1):
            suffix = f"·片段{part_index}" if len(value) > 520 else ""
            blocks.append({"location": f"{path.name}:段落{paragraph_number}{suffix}", "text": part})
    return blocks


def json_segments(value: Any, source_name: str, location: str = "$") -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            result.extend(json_segments(child, source_name, f"{location}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(json_segments(child, source_name, f"{location}[{index}]"))
    elif isinstance(value, (str, int, float, bool)):
        text = compact_whitespace(str(value))
        if text:
            result.append({"location": f"{source_name}:{location}", "text": text})
    return result


def extract_pdf_local(path: Path) -> list[dict[str, Any]]:
    from pypdf import PdfReader  # type: ignore

    reader = PdfReader(str(path))
    blocks: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        page_blocks = split_blocks(text, path.name, location_prefix=f"第{page_number}页·行")
        blocks.extend(page_blocks)
    return blocks


def discover_codex_python() -> Path | None:
    if os.name == "nt":
        candidate = (
            Path.home()
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "python"
            / "python.exe"
        )
    else:
        candidate = (
            Path.home()
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "python"
            / "bin"
            / "python"
        )
    return candidate if candidate.exists() else None


def extract_pdf(path: Path) -> list[dict[str, Any]]:
    try:
        return extract_pdf_local(path)
    except ImportError:
        bundled_python = discover_codex_python()
        if not bundled_python:
            raise RuntimeError("PDF 原文需要 pypdf；当前环境未提供，也未找到 Codex 图文运行时。")
        with tempfile.TemporaryDirectory(prefix="diagram-logic-audit-") as temp_name:
            output = Path(temp_name) / "segments.json"
            completed = subprocess.run(
                [
                    str(bundled_python),
                    str(Path(__file__).resolve()),
                    "extract",
                    "--source",
                    str(path),
                    "--output",
                    str(output),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            if completed.returncode != 0 or not output.exists():
                detail = (completed.stderr or completed.stdout or "未知 PDF 抽取错误").strip()
                raise RuntimeError(f"PDF 原文抽取失败：{detail[-1600:]}")
            payload = load_json(output)
            return payload["segments"]


def extract_source(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_TEXT:
        return split_blocks(decode_text(path), path.name)
    if suffix == ".docx":
        return extract_docx(path)
    if suffix == ".pdf":
        return extract_pdf(path)
    if suffix == ".json":
        with path.open("r", encoding="utf-8-sig") as handle:
            return json_segments(json.load(handle), path.name)
    raise ValueError(f"不支持的原文格式：{suffix or '无扩展名'}。")


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return "".join(char for char in normalized if char.isalnum())


def partial_similarity(needle: str, haystack: str) -> float:
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 1.0
    if len(haystack) <= len(needle) * 1.25:
        return difflib.SequenceMatcher(None, needle, haystack).ratio()
    window = max(len(needle), 4)
    step = max(1, window // 5)
    best = 0.0
    for start in range(0, max(1, len(haystack) - window + 1), step):
        candidate = haystack[start : start + window]
        best = max(best, difflib.SequenceMatcher(None, needle, candidate).ratio())
        if best >= 0.96:
            break
    return best


def excerpt_for(text: str, term: str) -> str:
    normalized_term = normalize_text(term)
    compact = compact_whitespace(text)
    raw_index = compact.lower().find(term.lower())
    if raw_index >= 0:
        start = max(0, raw_index - 70)
        end = min(len(compact), raw_index + len(term) + 110)
        excerpt = compact[start:end]
    else:
        excerpt = compact[:MAX_EXCERPT]
    if len(excerpt) > MAX_EXCERPT:
        excerpt = excerpt[: MAX_EXCERPT - 1] + "…"
    if not normalized_term:
        return excerpt
    return excerpt


def match_term(term: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    normalized = normalize_text(term)
    best_score = 0.0
    best_segment: dict[str, Any] | None = None
    exact = False
    for segment in segments:
        segment_normalized = segment["normalized"]
        if normalized and normalized in segment_normalized:
            best_score = 1.0
            best_segment = segment
            exact = True
            break
        score = partial_similarity(normalized, segment_normalized)
        if score > best_score:
            best_score = score
            best_segment = segment
    if exact:
        status = "exact"
    elif best_score >= 0.86:
        status = "supported"
    elif best_score >= 0.65:
        status = "weak"
    else:
        status = "missing"
    return {
        "term": term,
        "status": status,
        "score": round(best_score, 3),
        "location": best_segment["location"] if best_segment else None,
        "excerpt": excerpt_for(best_segment["text"], term) if best_segment else None,
        "segment_index": best_segment["index"] if best_segment else None,
    }


def unique_terms(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = compact_whitespace(str(value))
        key = normalize_text(text)
        if len(key) < 2 or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def entity_definitions(spec: dict[str, Any]) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    if isinstance(spec.get("control"), dict):
        section = spec["control"]
        entities.append(
            {
                "type": "control",
                "id": "control",
                "title": section.get("title", "控制条件"),
                "required_terms": unique_terms(section.get("source_terms", [])),
                "automatic_terms": unique_terms([section.get("title", ""), *section.get("items", [])]),
            }
        )
    for node in spec.get("nodes", []):
        entities.append(
            {
                "type": "node",
                "id": node["id"],
                "title": node.get("title", node["id"]),
                "required_terms": unique_terms(node.get("source_terms", [])),
                "automatic_terms": unique_terms([node.get("title", ""), *node.get("items", [])]),
            }
        )
    if isinstance(spec.get("result"), dict):
        section = spec["result"]
        entities.append(
            {
                "type": "result",
                "id": "result",
                "title": section.get("title", "结论"),
                "required_terms": unique_terms(section.get("source_terms", [])),
                "automatic_terms": unique_terms([section.get("title", ""), *section.get("items", [])]),
            }
        )
    return entities


def strongest_index(entity: dict[str, Any]) -> int | None:
    candidates = [
        term for term in [*entity.get("required_matches", []), *entity.get("automatic_matches", [])]
        if term.get("status") in {"exact", "supported"} and term.get("segment_index") is not None
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item["score"], item["segment_index"]))
    return int(candidates[0]["segment_index"])


def build_evidence(spec: dict[str, Any], source_paths: list[Path]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    for source_path in source_paths:
        extracted = extract_source(source_path)
        source_info = {
            "path": str(source_path.resolve()),
            "sha256": sha256_file(source_path),
            "segment_count": len(extracted),
        }
        sources.append(source_info)
        for segment in extracted:
            text = compact_whitespace(segment["text"])
            if not text:
                continue
            segments.append(
                {
                    "index": len(segments),
                    "location": segment["location"],
                    "text": text,
                    "normalized": normalize_text(text),
                }
            )
    if not segments:
        raise ValueError("原文没有提取到可检查的文字。扫描版 PDF 请先 OCR。")
    fingerprint_payload = "\n".join(f"{item['path']}:{item['sha256']}" for item in sources)
    source_fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()
    entities: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for definition in entity_definitions(spec):
        required_matches = [match_term(term, segments) for term in definition["required_terms"]]
        automatic_matches = [match_term(term, segments) for term in definition["automatic_terms"]]
        required_ok = all(match["status"] in {"exact", "supported"} for match in required_matches)
        automatic_supported = sum(match["status"] in {"exact", "supported"} for match in automatic_matches)
        automatic_coverage = automatic_supported / max(1, len(automatic_matches))
        anchored = required_ok and (bool(required_matches) or automatic_coverage >= 0.34)
        entity = {
            **definition,
            "required_matches": required_matches,
            "automatic_matches": automatic_matches,
            "automatic_coverage": round(automatic_coverage, 3),
            "anchored": anchored,
        }
        entities.append(entity)
        missing_required = [match["term"] for match in required_matches if match["status"] not in {"exact", "supported"}]
        if missing_required:
            errors.append(f"{definition['type']}:{definition['id']} 的原文必备词未找到可靠证据：{'、'.join(missing_required)}")
        elif not anchored:
            warnings.append(f"{definition['type']}:{definition['id']} 缺少足够的原文锚点，需要人工语义核对。")

    global_required = unique_terms((spec.get("audit") or {}).get("required_terms", []))
    global_matches = [match_term(term, segments) for term in global_required]
    missing_global = [match["term"] for match in global_matches if match["status"] not in {"exact", "supported"}]
    if missing_global:
        errors.append("全局必备词未找到可靠原文证据：" + "、".join(missing_global))

    entity_map = {entity["id"]: entity for entity in entities if entity["type"] == "node"}
    relations: list[dict[str, Any]] = []
    for edge in spec.get("edges", []):
        required_terms = unique_terms(edge.get("source_terms", []))
        matches = [match_term(term, segments) for term in required_terms]
        missing = [match["term"] for match in matches if match["status"] not in {"exact", "supported"}]
        if missing:
            errors.append(f"关系 {edge['from']}→{edge['to']} 的原文必备词未找到可靠证据：{'、'.join(missing)}")
        source_index = strongest_index(entity_map.get(edge["from"], {}))
        target_index = strongest_index(entity_map.get(edge["to"], {}))
        order_flag = "unknown"
        if source_index is not None and target_index is not None:
            order_flag = "forward" if source_index <= target_index else "reverse_in_source"
            if order_flag == "reverse_in_source":
                warnings.append(f"关系 {edge['from']}→{edge['to']} 与原文首次出现顺序相反，需判断是合理依赖还是箭头倒置。")
        relations.append(
            {
                "from": edge["from"],
                "to": edge["to"],
                "label": edge.get("label", ""),
                "required_terms": required_terms,
                "required_matches": matches,
                "source_entity_index": source_index,
                "target_entity_index": target_index,
                "order_flag": order_flag,
            }
        )
    return {
        "schema_version": 1,
        "purpose": "检索原文证据；不能单独证明语义一致，必须继续完成 AI 语义审核。",
        "spec_sha256": sha256_json(spec),
        "source_fingerprint": source_fingerprint,
        "sources": sources,
        "entities": entities,
        "relations": relations,
        "global_required_matches": global_matches,
        "automatic_checks": {
            "passed": not errors,
            "errors": errors,
            "warnings": warnings,
        },
        "requires_ai_semantic_review": True,
    }


def evidence_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 框架图—论文原文证据检查",
        "",
        "> 本报告只完成证据检索。最终是否符合原文，必须由 AI 阅读原文后完成语义审核并通过结构验证。",
        "",
        f"- 自动检查：{'通过' if report['automatic_checks']['passed'] else '未通过'}",
        f"- 原文文件：{len(report['sources'])}",
        f"- 图中实体：{len(report['entities'])}",
        f"- 关系：{len(report['relations'])}",
        "",
        "## 实体证据",
        "",
        "| 类型 | ID | 标题 | 自动覆盖率 | 最强证据位置 |",
        "|---|---|---|---:|---|",
    ]
    for entity in report["entities"]:
        matches = [*entity["required_matches"], *entity["automatic_matches"]]
        matches.sort(key=lambda item: item["score"], reverse=True)
        location = matches[0].get("location") if matches else ""
        lines.append(
            f"| {entity['type']} | `{entity['id']}` | {entity['title']} | {entity['automatic_coverage']:.0%} | {location or '未找到'} |"
        )
    lines.extend(["", "## 自动发现的问题", ""])
    errors = report["automatic_checks"]["errors"]
    warnings = report["automatic_checks"]["warnings"]
    if not errors and not warnings:
        lines.append("未发现确定性错误；仍需完成语义审核。")
    else:
        lines.extend(f"- 错误：{item}" for item in errors)
        lines.extend(f"- 待核对：{item}" for item in warnings)
    lines.append("")
    return "\n".join(lines)


def expected_entity_keys(spec: dict[str, Any]) -> list[tuple[str, str]]:
    return [(entity["type"], entity["id"]) for entity in entity_definitions(spec)]


def review_template(spec: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    entity_checks = []
    for entity in entity_definitions(spec):
        entity_checks.append(
            {
                "type": entity["type"],
                "id": entity["id"],
                "status": "partial",
                "evidence": [],
                "notes": "待 AI 阅读原文后填写。",
            }
        )
    relation_checks = [
        {
            "from": edge["from"],
            "to": edge["to"],
            "status": "uncertain",
            "evidence": [],
            "reasoning": "待 AI 判断箭头方向、因果或依赖关系。",
        }
        for edge in spec.get("edges", [])
    ]
    return {
        "schema_version": 1,
        "spec_sha256": evidence["spec_sha256"],
        "source_fingerprint": evidence["source_fingerprint"],
        "verdict": "revise",
        "summary": "待完成论文原文一致性审核。",
        "entity_checks": entity_checks,
        "relation_checks": relation_checks,
        "omissions": [],
        "fabrications": [],
        "required_revisions": ["完成全部实体、关系、遗漏和臆造检查。"],
    }


def valid_evidence_list(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, dict):
            return False
        if not compact_whitespace(str(item.get("location", ""))):
            return False
        excerpt = compact_whitespace(str(item.get("excerpt", "")))
        if not excerpt or len(excerpt) > MAX_EXCERPT:
            return False
    return True


def current_source_index(evidence: dict[str, Any]) -> tuple[list[str], dict[str, list[str]], list[str]]:
    errors: list[str] = []
    corpus: list[str] = []
    locations: dict[str, list[str]] = {}
    for source in evidence.get("sources", []):
        path = Path(str(source.get("path", "")))
        if not path.exists():
            errors.append(f"原文文件已不存在：{path}")
            continue
        current_hash = sha256_file(path)
        if current_hash != source.get("sha256"):
            errors.append(f"原文文件在证据包生成后已修改：{path}")
            continue
        try:
            segments = extract_source(path)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"验证时无法重新读取原文 {path}：{exc}")
            continue
        for segment in segments:
            normalized = normalize_text(segment["text"])
            corpus.append(normalized)
            locations.setdefault(segment["location"], []).append(normalized)
    return corpus, locations, errors


def validate_review_evidence(
    evidence_items: Any,
    owner: str,
    corpus: list[str],
    locations: dict[str, list[str]],
) -> list[str]:
    errors: list[str] = []
    if not valid_evidence_list(evidence_items):
        return [f"{owner} 必须给出不超过 {MAX_EXCERPT} 字的原文证据及位置。"]
    for item in evidence_items:
        location = compact_whitespace(str(item["location"]))
        excerpt = normalize_text(str(item["excerpt"]))
        if not any(excerpt in text for text in corpus):
            errors.append(f"{owner} 的引文未在当前原文中找到：{item['excerpt'][:40]}")
            continue
        if location not in locations or not any(excerpt in text for text in locations[location]):
            errors.append(f"{owner} 的引文与所填位置不一致：{location}")
    return errors


def verify_review(spec: dict[str, Any], evidence: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if review.get("spec_sha256") != evidence.get("spec_sha256") or evidence.get("spec_sha256") != sha256_json(spec):
        errors.append("审核文件、证据包与当前规格的摘要不一致；规格修改后必须重新审核。")
    if review.get("source_fingerprint") != evidence.get("source_fingerprint"):
        errors.append("审核文件与证据包对应的原文指纹不一致。")
    if not (evidence.get("automatic_checks") or {}).get("passed", False):
        errors.append("证据包仍包含确定性错误，不能由 AI 审核覆盖为通过。")
    corpus, locations, source_errors = current_source_index(evidence)
    errors.extend(source_errors)

    expected_entities = expected_entity_keys(spec)
    entity_checks = review.get("entity_checks")
    if not isinstance(entity_checks, list):
        errors.append("entity_checks 必须是数组。")
        entity_checks = []
    actual_entities = [
        (item.get("type"), item.get("id")) for item in entity_checks if isinstance(item, dict)
    ]
    if sorted(actual_entities) != sorted(expected_entities):
        errors.append("entity_checks 必须恰好覆盖 control、全部节点和 result，不能缺失或重复。")
    for item in entity_checks:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status not in {"supported", "partial", "unsupported"}:
            errors.append(f"实体 {item.get('id')} 的 status 无效。")
        if status in {"supported", "partial"}:
            errors.extend(
                validate_review_evidence(
                    item.get("evidence"), f"实体 {item.get('id')}", corpus, locations
                )
            )
        if status != "supported":
            warnings.append(f"实体 {item.get('id')} 尚未完全由原文支持：{status}。")

    expected_relations = [(edge["from"], edge["to"]) for edge in spec.get("edges", [])]
    relation_checks = review.get("relation_checks")
    if not isinstance(relation_checks, list):
        errors.append("relation_checks 必须是数组。")
        relation_checks = []
    actual_relations = [
        (item.get("from"), item.get("to")) for item in relation_checks if isinstance(item, dict)
    ]
    if sorted(actual_relations) != sorted(expected_relations):
        errors.append("relation_checks 必须恰好覆盖规格中的每一条边，不能缺失或重复。")
    for item in relation_checks:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        relation_name = f"{item.get('from')}→{item.get('to')}"
        if status not in {"supported", "uncertain", "contradicted"}:
            errors.append(f"关系 {relation_name} 的 status 无效。")
        if status == "supported":
            errors.extend(
                validate_review_evidence(
                    item.get("evidence"), f"关系 {relation_name}", corpus, locations
                )
            )
        if not compact_whitespace(str(item.get("reasoning", ""))):
            errors.append(f"关系 {relation_name} 必须说明方向或依赖判断。")
        if status != "supported":
            warnings.append(f"关系 {relation_name} 尚未确认：{status}。")

    for field in ("omissions", "fabrications", "required_revisions"):
        if not isinstance(review.get(field), list):
            errors.append(f"{field} 必须是数组。")
    verdict = review.get("verdict")
    if verdict not in {"pass", "revise", "unverifiable"}:
        errors.append("verdict 必须是 pass、revise 或 unverifiable。")
    if verdict == "pass":
        if warnings:
            errors.append("存在未完全支持的实体或关系时，verdict 不能为 pass。")
        for field in ("omissions", "fabrications", "required_revisions"):
            if review.get(field):
                errors.append(f"verdict 为 pass 时 {field} 必须为空。")
    else:
        warnings.append(f"语义审核结论为 {verdict}，框架图尚不能宣称与论文原文一致。")
    if not compact_whitespace(str(review.get("summary", ""))):
        errors.append("summary 不能为空。")
    return {
        "passed": not errors and not warnings and verdict == "pass",
        "verdict": verdict,
        "errors": errors,
        "warnings": warnings,
        "spec_sha256": evidence.get("spec_sha256"),
        "source_fingerprint": evidence.get("source_fingerprint"),
    }


def command_extract(args: argparse.Namespace) -> int:
    segments = extract_pdf_local(args.source)
    write_json(args.output, {"segments": segments})
    return 0


def command_evidence(args: argparse.Namespace) -> int:
    try:
        spec = load_json(args.spec)
        report = build_evidence(spec, args.source)
        write_json(args.output, report)
        markdown_path = args.output.with_suffix(".md")
        markdown_path.write_text(evidence_markdown(report), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"证据检查失败：{exc}", file=sys.stderr)
        return 2
    checks = report["automatic_checks"]
    print(f"证据包：{args.output}")
    print(f"摘要：{markdown_path}")
    print(f"自动错误：{len(checks['errors'])}；待语义核对：{len(checks['warnings'])}")
    if checks["errors"] or (args.strict and checks["warnings"]):
        return 2
    return 0


def command_template(args: argparse.Namespace) -> int:
    try:
        spec = load_json(args.spec)
        evidence = load_json(args.evidence)
        template = review_template(spec, evidence)
        write_json(args.output, template)
        print(f"语义审核模板：{args.output}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"创建审核模板失败：{exc}", file=sys.stderr)
        return 2


def command_verify(args: argparse.Namespace) -> int:
    try:
        spec = load_json(args.spec)
        evidence = load_json(args.evidence)
        review = load_json(args.review)
        result = verify_review(spec, evidence, review)
        if args.output:
            write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] else 2
    except Exception as exc:  # noqa: BLE001
        print(f"验证语义审核失败：{exc}", file=sys.stderr)
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查框架图与论文原文的逻辑一致性。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    evidence = subparsers.add_parser("evidence", help="从原文检索实体、术语和关系证据。")
    evidence.add_argument("--spec", type=Path, required=True)
    evidence.add_argument("--source", type=Path, action="append", required=True, help="可重复传入多个原文文件。")
    evidence.add_argument("--output", type=Path, required=True, help="*.logic-evidence.json")
    evidence.add_argument("--strict", action="store_true", help="把待语义核对项也视为命令不通过。")
    evidence.set_defaults(func=command_evidence)

    template = subparsers.add_parser("template", help="生成覆盖全部实体和边的 AI 语义审核模板。")
    template.add_argument("--spec", type=Path, required=True)
    template.add_argument("--evidence", type=Path, required=True)
    template.add_argument("--output", type=Path, required=True, help="*.logic-review.json")
    template.set_defaults(func=command_template)

    verify = subparsers.add_parser("verify", help="验证 AI 语义审核是否完整并与当前原文、规格绑定。")
    verify.add_argument("--spec", type=Path, required=True)
    verify.add_argument("--evidence", type=Path, required=True)
    verify.add_argument("--review", type=Path, required=True)
    verify.add_argument("--output", type=Path, help="*.logic-check.json")
    verify.set_defaults(func=command_verify)

    extract = subparsers.add_parser("extract", help=argparse.SUPPRESS)
    extract.add_argument("--source", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)
    extract.set_defaults(func=command_extract)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

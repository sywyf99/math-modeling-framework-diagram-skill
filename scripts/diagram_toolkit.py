#!/usr/bin/env python3
"""Deterministic framework-diagram builder for the Codex skill.

The tool consumes one UTF-8 JSON specification and emits a vector SVG, a
native editable draw.io file, Mermaid source, a PNG preview, a copied canonical
specification, and a machine-readable quality report.  It intentionally uses
only Python's standard library; PNG rendering is delegated to an installed
Chromium browser when CairoSVG is unavailable.
"""

from __future__ import annotations

import argparse
import copy
import html
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SKILL_DIR = Path(__file__).resolve().parent.parent
PRESETS_PATH = SKILL_DIR / "assets" / "layout-presets.json"
ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
SUPPORTED_LAYOUTS = {"pipeline", "comparison", "swimlane", "hub", "hierarchy"}
SUPPORTED_FORMATS = {"svg", "png", "drawio", "mmd"}
FONT_STACK = "Noto Sans CJK SC,Microsoft YaHei,PingFang SC,Arial,sans-serif"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("规格文件的根元素必须是 JSON 对象。")
    return value


def load_presets() -> dict[str, Any]:
    return load_json(PRESETS_PATH)


def text_units(value: str) -> float:
    total = 0.0
    for char in value:
        if char.isspace():
            total += 0.35
        elif ord(char) < 128:
            total += 0.58
        else:
            total += 1.0
    return total


def split_long_token(token: str, max_units: float) -> list[str]:
    parts: list[str] = []
    current = ""
    current_units = 0.0
    for char in token:
        unit = text_units(char)
        if current and current_units + unit > max_units:
            parts.append(current)
            current = char
            current_units = unit
        else:
            current += char
            current_units += unit
    if current:
        parts.append(current)
    return parts


def wrap_text(value: str, pixel_width: float, font_size: float) -> list[str]:
    value = " ".join(str(value).strip().split())
    if not value:
        return []
    max_units = max(2.0, pixel_width / max(font_size, 1.0))
    if " " not in value:
        return split_long_token(value, max_units)
    words = value.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if text_units(candidate) <= max_units:
            current = candidate
            continue
        if current:
            lines.append(current)
        if text_units(word) > max_units:
            pieces = split_long_token(word, max_units)
            lines.extend(pieces[:-1])
            current = pieces[-1]
        else:
            current = word
    if current:
        lines.append(current)
    return lines


def normalize_items(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def normalize_spec(raw: dict[str, Any]) -> dict[str, Any]:
    spec = copy.deepcopy(raw)
    spec.setdefault("layout", "pipeline")
    spec.setdefault("canvas", "a4-landscape")
    spec.setdefault("subtitle", "")
    nodes = spec.setdefault("nodes", [])
    for index, node in enumerate(nodes):
        node.setdefault("kind", "default")
        node.setdefault("items", [])
        node["items"] = normalize_items(node.get("items"))
        node.setdefault("step", f"{index + 1:02d}")
    for section_name in ("control", "result"):
        if section_name in spec and spec[section_name] is not None:
            section = spec[section_name]
            if isinstance(section, str):
                spec[section_name] = {"title": section, "items": []}
            elif isinstance(section, dict):
                section.setdefault("title", "")
                section["items"] = normalize_items(section.get("items"))
    if not spec.get("edges") and len(nodes) > 1:
        spec["edges"] = [
            {"from": nodes[index]["id"], "to": nodes[index + 1]["id"], "label": ""}
            for index in range(len(nodes) - 1)
        ]
    else:
        spec.setdefault("edges", [])
        for edge in spec["edges"]:
            edge.setdefault("label", "")
    return spec


def validate_spec(spec: dict[str, Any], config: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    def validate_source_terms(container: dict[str, Any], label: str) -> None:
        terms = container.get("source_terms")
        if terms is None:
            return
        if not isinstance(terms, list) or any(not isinstance(term, str) or len(term.strip()) < 2 for term in terms):
            errors.append(f"{label}.source_terms 必须是每项至少 2 个字符的字符串数组。")
        elif len({term.strip() for term in terms}) != len(terms):
            errors.append(f"{label}.source_terms 不能包含重复项。")
    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("title 必须是非空字符串。")
    elif len(title) > 50:
        warnings.append("主标题超过 50 个字符，建议缩短后把边界条件移入副标题。")

    layout = spec.get("layout", "pipeline")
    if layout not in SUPPORTED_LAYOUTS:
        errors.append(f"不支持的 layout：{layout}。")
    canvas = spec.get("canvas", "a4-landscape")
    if canvas not in config.get("presets", {}):
        errors.append(f"不支持的 canvas：{canvas}。")

    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append("nodes 必须是至少包含一个节点的数组。")
        nodes = []
    if len(nodes) > 40:
        errors.append("可执行工具包最多处理 40 个节点；更稠密的网络请改用 D2/Graphviz。")
    if len(nodes) > 16:
        warnings.append("节点超过 16 个，自动布局可能过密；优先考虑拆图或使用 D2/Graphviz。")

    ids: list[str] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append(f"nodes[{index}] 必须是对象。")
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str) or not ID_RE.fullmatch(node_id):
            errors.append(f"nodes[{index}].id 必须匹配 {ID_RE.pattern}。")
        else:
            ids.append(node_id)
        if not isinstance(node.get("title"), str) or not node.get("title", "").strip():
            errors.append(f"nodes[{index}].title 必须是非空字符串。")
        items = node.get("items", [])
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            errors.append(f"nodes[{index}].items 必须是字符串数组。")
        elif len(items) > 6:
            warnings.append(f"节点 {node_id or index} 超过 6 个正文项，建议压缩或拆分。")
        if any(len(item) > 56 for item in items if isinstance(item, str)):
            warnings.append(f"节点 {node_id or index} 含过长正文项，可能需要人工改写为短句。")
        validate_source_terms(node, f"nodes[{index}]")
    duplicates = sorted({node_id for node_id in ids if ids.count(node_id) > 1})
    if duplicates:
        errors.append("节点 ID 重复：" + "、".join(duplicates))

    valid_ids = set(ids)
    edges = spec.get("edges", [])
    if not isinstance(edges, list):
        errors.append("edges 必须是数组。")
        edges = []
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            errors.append(f"edges[{index}] 必须是对象。")
            continue
        source = edge.get("from")
        target = edge.get("to")
        if source not in valid_ids:
            errors.append(f"edges[{index}].from 指向不存在的节点：{source}。")
        if target not in valid_ids:
            errors.append(f"edges[{index}].to 指向不存在的节点：{target}。")
        if source == target and source in valid_ids:
            warnings.append(f"节点 {source} 存在自环，请确认是否必要。")
        validate_source_terms(edge, f"edges[{index}]")

    for section_name in ("control", "result"):
        section = spec.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict) or not isinstance(section.get("title"), str):
            errors.append(f"{section_name} 必须是包含 title 的对象。")
            continue
        items = section.get("items", [])
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            errors.append(f"{section_name}.items 必须是字符串数组。")
        elif len(items) > 8:
            warnings.append(f"{section_name} 超过 8 项，条件带可能拥挤。")
        validate_source_terms(section, section_name)

    audit = spec.get("audit")
    if audit is not None:
        if not isinstance(audit, dict):
            errors.append("audit 必须是对象。")
        else:
            required_terms = audit.get("required_terms", [])
            if not isinstance(required_terms, list) or any(
                not isinstance(term, str) or len(term.strip()) < 2 for term in required_terms
            ):
                errors.append("audit.required_terms 必须是每项至少 2 个字符的字符串数组。")
            elif len({term.strip() for term in required_terms}) != len(required_terms):
                errors.append("audit.required_terms 不能包含重复项。")

    if layout == "swimlane":
        lanes = spec.get("lanes")
        if not isinstance(lanes, list) or not lanes:
            errors.append("swimlane 布局必须提供非空 lanes。")
        else:
            lane_ids = {lane.get("id") for lane in lanes if isinstance(lane, dict)}
            for node in nodes:
                if isinstance(node, dict) and node.get("lane") not in lane_ids:
                    errors.append(f"节点 {node.get('id')} 未指定有效 lane。")
    if layout == "hub":
        center = spec.get("center")
        if center is not None and center not in valid_ids:
            errors.append(f"center 指向不存在的节点：{center}。")
        if len(nodes) > 9:
            warnings.append("hub 布局超过 9 个节点，外围节点可能过密。")
    return errors, warnings


def layout_regions(spec: dict[str, Any], preset: dict[str, Any]) -> dict[str, Any]:
    width = float(preset["width"])
    height = float(preset["height"])
    mx = float(preset["margin_x"])
    my = float(preset["margin_y"])
    gap = float(preset["gap"])
    header_h = float(preset["header_height"])
    section_top = my + header_h
    usable_w = width - 2 * mx

    control_rect: Rect | None = None
    if spec.get("control"):
        control_rect = Rect(mx, section_top, usable_w, float(preset["section_height"]))
        main_top = control_rect.bottom + gap
    else:
        main_top = section_top + gap * 0.45

    result_rect: Rect | None = None
    if spec.get("result"):
        result_h = float(preset["result_height"])
        footer_h = float(preset.get("footer_height", 0))
        result_rect = Rect(mx, height - my - footer_h - result_h, usable_w, result_h)
        main_bottom = result_rect.y - gap
    else:
        main_bottom = height - my
    main = Rect(mx, main_top, usable_w, max(80.0, main_bottom - main_top))
    return {
        "width": width,
        "height": height,
        "main": main,
        "control": control_rect,
        "result": result_rect,
        "lanes": [],
    }


def pipeline_positions(nodes: list[dict[str, Any]], main: Rect, preset: dict[str, Any]) -> dict[str, Rect]:
    count = len(nodes)
    if count <= 4:
        columns = count
    elif count <= 6:
        columns = 3
    elif count <= 8:
        columns = 4
    elif count == 9:
        columns = 3
    else:
        columns = int(preset.get("max_columns", 4))
    columns = max(1, min(columns, count))
    rows = math.ceil(count / columns)
    gap = float(preset["gap"])
    card_w = min(520.0, (main.w - gap * (columns - 1)) / columns)
    available_h = (main.h - gap * (rows - 1)) / rows
    card_h = min(float(preset["max_card_height"]), available_h)
    total_h = rows * card_h + (rows - 1) * gap
    start_y = main.y + max(0.0, (main.h - total_h) / 2)
    result: dict[str, Rect] = {}
    cursor = 0
    for row in range(rows):
        row_count = min(columns, count - cursor)
        row_w = row_count * card_w + (row_count - 1) * gap
        start_x = main.x + (main.w - row_w) / 2
        x_values = [start_x + index * (card_w + gap) for index in range(row_count)]
        if row % 2 == 1:
            x_values.reverse()
        for x in x_values:
            node = nodes[cursor]
            result[node["id"]] = Rect(x, start_y + row * (card_h + gap), card_w, card_h)
            cursor += 1
    return result


def swimlane_positions(
    spec: dict[str, Any], main: Rect, preset: dict[str, Any]
) -> tuple[dict[str, Rect], list[dict[str, Any]]]:
    gap = float(preset["gap"])
    lanes = spec.get("lanes", [])
    lane_gap = max(12.0, gap * 0.55)
    lane_h = (main.h - lane_gap * (len(lanes) - 1)) / max(1, len(lanes))
    label_w = min(190.0, main.w * 0.16)
    node_area_x = main.x + label_w + gap
    node_area_w = main.w - label_w - gap
    positions: dict[str, Rect] = {}
    lane_shapes: list[dict[str, Any]] = []
    for lane_index, lane in enumerate(lanes):
        lane_y = main.y + lane_index * (lane_h + lane_gap)
        lane_rect = Rect(main.x, lane_y, main.w, lane_h)
        lane_shapes.append({"id": lane["id"], "title": lane["title"], "rect": lane_rect, "label_w": label_w})
        members = [node for node in spec["nodes"] if node.get("lane") == lane["id"]]
        if not members:
            continue
        inner_gap = max(16.0, gap * 0.72)
        card_w = min(360.0, (node_area_w - inner_gap * (len(members) - 1)) / len(members))
        card_h = min(float(preset["max_card_height"]), lane_h - 24.0)
        row_w = len(members) * card_w + (len(members) - 1) * inner_gap
        start_x = node_area_x + (node_area_w - row_w) / 2
        for member_index, node in enumerate(members):
            positions[node["id"]] = Rect(
                start_x + member_index * (card_w + inner_gap),
                lane_y + (lane_h - card_h) / 2,
                card_w,
                card_h,
            )
    return positions, lane_shapes


def hub_positions(spec: dict[str, Any], main: Rect, preset: dict[str, Any]) -> dict[str, Rect]:
    nodes = spec["nodes"]
    center_id = spec.get("center") or next(
        (node["id"] for node in nodes if node.get("kind") == "model"), nodes[0]["id"]
    )
    center_node = next(node for node in nodes if node["id"] == center_id)
    outer = [node for node in nodes if node["id"] != center_id]
    center_w = min(350.0, main.w * 0.28)
    center_h = min(250.0, main.h * 0.34)
    outer_w = min(300.0, main.w * 0.24)
    outer_h = min(220.0, main.h * 0.29)
    result = {
        center_node["id"]: Rect(main.cx - center_w / 2, main.cy - center_h / 2, center_w, center_h)
    }
    if not outer:
        return result
    radius_x = max(outer_w * 0.65, (main.w - outer_w) / 2)
    radius_y = max(outer_h * 0.65, (main.h - outer_h) / 2)
    for index, node in enumerate(outer):
        angle = -math.pi / 2 + 2 * math.pi * index / len(outer)
        cx = main.cx + radius_x * math.cos(angle)
        cy = main.cy + radius_y * math.sin(angle)
        result[node["id"]] = Rect(cx - outer_w / 2, cy - outer_h / 2, outer_w, outer_h)
    return result


def hierarchy_levels(spec: dict[str, Any]) -> list[list[str]]:
    ids = [node["id"] for node in spec["nodes"]]
    outgoing = {node_id: [] for node_id in ids}
    indegree = {node_id: 0 for node_id in ids}
    for edge in spec["edges"]:
        source, target = edge["from"], edge["to"]
        if source in outgoing and target in indegree:
            outgoing[source].append(target)
            indegree[target] += 1
    queue = [node_id for node_id in ids if indegree[node_id] == 0]
    level_of = {node_id: 0 for node_id in queue}
    visited: list[str] = []
    while queue:
        node_id = queue.pop(0)
        visited.append(node_id)
        for target in outgoing[node_id]:
            level_of[target] = max(level_of.get(target, 0), level_of[node_id] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(visited) != len(ids):
        return [ids[index : index + 4] for index in range(0, len(ids), 4)]
    max_level = max(level_of.values(), default=0)
    return [[node_id for node_id in ids if level_of.get(node_id) == level] for level in range(max_level + 1)]


def hierarchy_positions(spec: dict[str, Any], main: Rect, preset: dict[str, Any]) -> dict[str, Rect]:
    levels = hierarchy_levels(spec)
    gap = float(preset["gap"])
    row_gap = max(22.0, gap)
    row_h = (main.h - row_gap * (len(levels) - 1)) / max(1, len(levels))
    card_h = min(float(preset["max_card_height"]), row_h)
    result: dict[str, Rect] = {}
    for level_index, ids in enumerate(levels):
        inner_gap = max(20.0, gap)
        card_w = min(330.0, (main.w - inner_gap * (len(ids) - 1)) / max(1, len(ids)))
        row_w = len(ids) * card_w + max(0, len(ids) - 1) * inner_gap
        start_x = main.x + (main.w - row_w) / 2
        y = main.y + level_index * (row_h + row_gap) + (row_h - card_h) / 2
        for index, node_id in enumerate(ids):
            result[node_id] = Rect(start_x + index * (card_w + inner_gap), y, card_w, card_h)
    return result


def compute_layout(spec: dict[str, Any], preset: dict[str, Any]) -> dict[str, Any]:
    layout = layout_regions(spec, preset)
    main = layout["main"]
    mode = spec["layout"]
    if mode in {"pipeline", "comparison"}:
        positions = pipeline_positions(spec["nodes"], main, preset)
    elif mode == "swimlane":
        positions, lanes = swimlane_positions(spec, main, preset)
        layout["lanes"] = lanes
    elif mode == "hub":
        positions = hub_positions(spec, main, preset)
    else:
        positions = hierarchy_positions(spec, main, preset)
    layout["nodes"] = positions
    return layout


def rectangles_overlap(a: Rect, b: Rect, padding: float = 1.0) -> bool:
    return not (
        a.right + padding <= b.x
        or b.right + padding <= a.x
        or a.bottom + padding <= b.y
        or b.bottom + padding <= a.y
    )


def estimate_card_lines(node: dict[str, Any], rect: Rect) -> tuple[int, int]:
    title_lines = max(1, len(wrap_text(node["title"], rect.w - 94.0, 19.0)))
    body_lines = 0
    for item in node.get("items", []):
        body_lines += max(1, len(wrap_text(item, rect.w - 52.0, 14.0)))
    item_count = max(1, len(node.get("items", [])))
    body_height = max(0.0, rect.h - 96.0)
    row_height = min(64.0, max(38.0, (body_height - 10.0 * (item_count - 1)) / item_count))
    lines_per_row = max(1, int((row_height - 12.0) // 20.0))
    return title_lines + body_lines, 2 + item_count * lines_per_row


def analyze_layout(spec: dict[str, Any], layout: dict[str, Any]) -> tuple[list[str], list[str], dict[str, Any]]:
    errors: list[str] = []
    warnings: list[str] = []
    width, height = layout["width"], layout["height"]
    positions: dict[str, Rect] = layout["nodes"]
    ids = list(positions)
    for index, left_id in enumerate(ids):
        left = positions[left_id]
        if left.x < 0 or left.y < 0 or left.right > width or left.bottom > height:
            errors.append(f"节点 {left_id} 超出画布。")
        for right_id in ids[index + 1 :]:
            if rectangles_overlap(left, positions[right_id], padding=2.0):
                errors.append(f"节点 {left_id} 与 {right_id} 重叠。")
    node_map = {node["id"]: node for node in spec["nodes"]}
    for node_id, rect in positions.items():
        used, capacity = estimate_card_lines(node_map[node_id], rect)
        if used > capacity:
            warnings.append(f"节点 {node_id} 预计需要 {used} 行，但当前卡片约容纳 {capacity} 行。")
        item_count = len(node_map[node_id].get("items", []))
        minimum_height = 96.0 + item_count * 38.0 + max(0, item_count - 1) * 10.0
        if item_count and rect.h < minimum_height:
            warnings.append(f"节点 {node_id} 的卡片高度不足以容纳 {item_count} 个结构化条目。")
    total_node_area = sum(rect.w * rect.h for rect in positions.values())
    main: Rect = layout["main"]
    utilization = total_node_area / max(1.0, main.w * main.h)
    if len(positions) >= 3 and utilization < 0.25:
        warnings.append("主图卡片面积占比较低，可能存在过多留白。")
    metrics = {
        "canvas_width": int(width),
        "canvas_height": int(height),
        "canvas_ratio": round(width / height, 3),
        "node_count": len(positions),
        "edge_count": len(spec.get("edges", [])),
        "main_area_node_utilization": round(utilization, 3),
    }
    return errors, warnings, metrics


def xml_escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def fmt(value: float) -> str:
    if abs(value - round(value)) < 0.001:
        return str(int(round(value)))
    return f"{value:.1f}"


def boundary_point(rect: Rect, toward_x: float, toward_y: float) -> tuple[float, float]:
    dx = toward_x - rect.cx
    dy = toward_y - rect.cy
    if abs(dx) < 1e-6 and abs(dy) < 1e-6:
        return rect.cx, rect.cy
    scale_x = (rect.w / 2) / abs(dx) if abs(dx) > 1e-6 else float("inf")
    scale_y = (rect.h / 2) / abs(dy) if abs(dy) > 1e-6 else float("inf")
    scale = min(scale_x, scale_y)
    return rect.cx + dx * scale, rect.cy + dy * scale


def edge_path(source: Rect, target: Rect, mode: str) -> tuple[str, tuple[float, float]]:
    sx, sy = boundary_point(source, target.cx, target.cy)
    tx, ty = boundary_point(target, source.cx, source.cy)
    if mode == "hub":
        return f"M {fmt(sx)} {fmt(sy)} L {fmt(tx)} {fmt(ty)}", ((sx + tx) / 2, (sy + ty) / 2)
    if abs(sy - ty) < 8:
        return f"M {fmt(sx)} {fmt(sy)} L {fmt(tx)} {fmt(ty)}", ((sx + tx) / 2, sy)
    if abs(sx - tx) < 8:
        return f"M {fmt(sx)} {fmt(sy)} L {fmt(tx)} {fmt(ty)}", (sx, (sy + ty) / 2)
    if abs(tx - sx) >= abs(ty - sy):
        mid_x = (sx + tx) / 2
        path = f"M {fmt(sx)} {fmt(sy)} L {fmt(mid_x)} {fmt(sy)} L {fmt(mid_x)} {fmt(ty)} L {fmt(tx)} {fmt(ty)}"
        return path, (mid_x, (sy + ty) / 2)
    mid_y = (sy + ty) / 2
    path = f"M {fmt(sx)} {fmt(sy)} L {fmt(sx)} {fmt(mid_y)} L {fmt(tx)} {fmt(mid_y)} L {fmt(tx)} {fmt(ty)}"
    return path, ((sx + tx) / 2, mid_y)


def node_colors(node: dict[str, Any], config: dict[str, Any]) -> tuple[str, str]:
    palette = config["palette"]
    keys = config["kind_styles"].get(node.get("kind", "default"), config["kind_styles"]["default"])
    return palette[keys[0]], palette[keys[1]]


def band_item_rects(section: dict[str, Any], rect: Rect, role: str) -> list[tuple[str, Rect]]:
    items = section.get("items", [])
    if not items:
        return []
    title_w = min(230.0, rect.w * 0.19)
    content_x = rect.x + title_w + 20
    content_w = rect.right - 18 - content_x
    gap = 10.0
    if len(items) <= 4:
        columns, rows = len(items), 1
        row_h = 54.0 if role == "result" else 36.0
    else:
        columns, rows = math.ceil(len(items) / 2), 2
        row_h = 40.0 if role == "result" else 30.0
    cell_w = (content_w - gap * (columns - 1)) / columns
    total_h = rows * row_h + gap * (rows - 1)
    start_y = rect.y + (rect.h - total_h) / 2
    result: list[tuple[str, Rect]] = []
    for index, item in enumerate(items):
        row = index // columns
        column = index % columns
        result.append(
            (
                item,
                Rect(content_x + column * (cell_w + gap), start_y + row * (row_h + gap), cell_w, row_h),
            )
        )
    return result


def render_band(section: dict[str, Any], rect: Rect, role: str, config: dict[str, Any]) -> str:
    palette = config["palette"]
    if role == "control":
        stroke, fill = palette["condition"], palette["condition_fill"]
    else:
        stroke, fill = palette["result"], palette["result_fill"]
    title_w = min(230.0, rect.w * 0.19)
    parts = [
        f'<g class="{role}-band">',
        f'<rect x="{fmt(rect.x)}" y="{fmt(rect.y)}" width="{fmt(rect.w)}" height="{fmt(rect.h)}" rx="14" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>',
        f'<text x="{fmt(rect.x + 24)}" y="{fmt(rect.y + rect.h / 2 + 6)}" font-size="18" font-weight="700" fill="{stroke}">{xml_escape(section.get("title", ""))}</text>',
        f'<line x1="{fmt(rect.x + title_w)}" y1="{fmt(rect.y + 18)}" x2="{fmt(rect.x + title_w)}" y2="{fmt(rect.bottom - 18)}" stroke="{stroke}" stroke-opacity="0.28"/>',
    ]
    for item, chip in band_item_rects(section, rect, role):
        parts.append(
            f'<rect x="{fmt(chip.x)}" y="{fmt(chip.y)}" width="{fmt(chip.w)}" height="{fmt(chip.h)}" rx="9" fill="#FFFFFF" fill-opacity="0.88" stroke="{stroke}" stroke-opacity="0.22"/>'
        )
        lines = wrap_text(item, chip.w - 28.0, 13.0)
        first_y = chip.cy - (len(lines) - 1) * 9.0 + 5
        for line_index, line in enumerate(lines):
            parts.append(
                f'<text x="{fmt(chip.x + 14)}" y="{fmt(first_y + line_index * 18)}" font-size="13" fill="{palette["text"]}">{xml_escape(line)}</text>'
            )
    parts.append("</g>")
    return "\n".join(parts)


def render_lane(lane: dict[str, Any], config: dict[str, Any]) -> str:
    rect: Rect = lane["rect"]
    palette = config["palette"]
    label_w = lane["label_w"]
    return "\n".join(
        [
            '<g class="lane">',
            f'<rect x="{fmt(rect.x)}" y="{fmt(rect.y)}" width="{fmt(rect.w)}" height="{fmt(rect.h)}" rx="14" fill="#F8FAFC" stroke="{palette["line"]}"/>',
            f'<rect x="{fmt(rect.x)}" y="{fmt(rect.y)}" width="{fmt(label_w)}" height="{fmt(rect.h)}" rx="14" fill="{palette["primary_fill"]}"/>',
            f'<line x1="{fmt(rect.x + label_w)}" y1="{fmt(rect.y)}" x2="{fmt(rect.x + label_w)}" y2="{fmt(rect.bottom)}" stroke="{palette["line"]}"/>',
            f'<text x="{fmt(rect.x + 22)}" y="{fmt(rect.cy + 6)}" font-size="17" font-weight="700" fill="{palette["primary"]}">{xml_escape(lane["title"])}</text>',
            "</g>",
        ]
    )


def render_node(node: dict[str, Any], rect: Rect, index: int, config: dict[str, Any], radius: float) -> str:
    palette = config["palette"]
    stroke, fill = node_colors(node, config)
    step = str(node.get("step") or f"{index + 1:02d}")
    step_w = max(38.0, text_units(step) * 12.0 + 16.0)
    title_x = rect.x + 24 + step_w + 12
    title_lines = wrap_text(node["title"], rect.right - title_x - 18, 19.0)[:2]
    parts = [
        f'<g id="node-{xml_escape(node["id"])}" class="diagram-node">',
        f'<rect x="{fmt(rect.x)}" y="{fmt(rect.y)}" width="{fmt(rect.w)}" height="{fmt(rect.h)}" rx="{fmt(radius)}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>',
        f'<rect x="{fmt(rect.x + 20)}" y="{fmt(rect.y + 18)}" width="{fmt(step_w)}" height="27" rx="8" fill="{stroke}"/>',
        f'<text x="{fmt(rect.x + 20 + step_w / 2)}" y="{fmt(rect.y + 37)}" text-anchor="middle" font-size="12" font-weight="700" fill="#FFFFFF">{xml_escape(step)}</text>',
    ]
    title_y = rect.y + 38
    for line_index, line in enumerate(title_lines):
        parts.append(
            f'<text x="{fmt(title_x)}" y="{fmt(title_y + line_index * 22)}" font-size="19" font-weight="700" fill="{palette["text"]}">{xml_escape(line)}</text>'
        )
    separator_y = rect.y + (68 if len(title_lines) > 1 else 58)
    parts.append(
        f'<line x1="{fmt(rect.x + 22)}" y1="{fmt(separator_y)}" x2="{fmt(rect.right - 22)}" y2="{fmt(separator_y)}" stroke="{stroke}" stroke-opacity="0.35"/>'
    )
    items = node.get("items", [])
    if items:
        body_top = separator_y + 18
        body_bottom = rect.bottom - 20
        body_height = max(40.0, body_bottom - body_top)
        row_gap = 10.0
        max_row_height = 82.0 if len(items) <= 3 else 68.0
        row_height = min(max_row_height, max(38.0, (body_height - row_gap * (len(items) - 1)) / len(items)))
        total_height = len(items) * row_height + row_gap * (len(items) - 1)
        row_y = body_top
        for item in items:
            parts.append(
                f'<rect x="{fmt(rect.x + 22)}" y="{fmt(row_y)}" width="{fmt(rect.w - 44)}" height="{fmt(row_height)}" rx="9" fill="#FFFFFF" fill-opacity="0.72" stroke="{stroke}" stroke-opacity="0.18"/>'
            )
            parts.append(
                f'<circle cx="{fmt(rect.x + 39)}" cy="{fmt(row_y + row_height / 2)}" r="3.2" fill="{stroke}"/>'
            )
            lines = wrap_text(item, rect.w - 82.0, 14.0) or [""]
            line_height = 20.0
            first_y = row_y + row_height / 2 - (len(lines) - 1) * line_height / 2 + 5
            for line_index, line in enumerate(lines):
                parts.append(
                    f'<text x="{fmt(rect.x + 51)}" y="{fmt(first_y + line_index * line_height)}" font-size="14" fill="{palette["text"]}">{xml_escape(line)}</text>'
                )
            row_y += row_height + row_gap
    parts.append("</g>")
    return "\n".join(parts)


def generate_svg(spec: dict[str, Any], layout: dict[str, Any], config: dict[str, Any], preset: dict[str, Any]) -> str:
    palette = config["palette"]
    width, height = layout["width"], layout["height"]
    mx, my = float(preset["margin_x"]), float(preset["margin_y"])
    title_size = 34 if height >= 1000 else 32
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{fmt(width)}" height="{fmt(height)}" viewBox="0 0 {fmt(width)} {fmt(height)}">',
        "<defs>",
        f'<marker id="arrow" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{palette["muted"]}"/></marker>',
        "</defs>",
        f'<rect width="{fmt(width)}" height="{fmt(height)}" fill="{palette["background"]}"/>',
        f'<g font-family="{FONT_STACK}">',
        f'<text x="{fmt(mx)}" y="{fmt(my + 34)}" font-size="{title_size}" font-weight="700" fill="{palette["text"]}">{xml_escape(spec["title"])}</text>',
    ]
    if spec.get("subtitle"):
        parts.append(
            f'<text x="{fmt(mx)}" y="{fmt(my + 67)}" font-size="15" fill="{palette["muted"]}">{xml_escape(spec["subtitle"])}</text>'
        )
    divider_y = my + float(preset["header_height"]) - 16
    parts.append(
        f'<line x1="{fmt(mx)}" y1="{fmt(divider_y)}" x2="{fmt(width - mx)}" y2="{fmt(divider_y)}" stroke="{palette["line"]}"/>'
    )
    if layout.get("control"):
        parts.append(render_band(spec["control"], layout["control"], "control", config))
    for lane in layout.get("lanes", []):
        parts.append(render_lane(lane, config))

    positions: dict[str, Rect] = layout["nodes"]
    for edge_index, edge in enumerate(spec.get("edges", [])):
        if edge["from"] not in positions or edge["to"] not in positions:
            continue
        path, label_point = edge_path(positions[edge["from"]], positions[edge["to"]], spec["layout"])
        parts.append(
            f'<path id="edge-{edge_index}" d="{path}" fill="none" stroke="{palette["muted"]}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" marker-end="url(#arrow)"/>'
        )
        label = edge.get("label", "")
        if label:
            label_w = max(42.0, text_units(label) * 12.0 + 20.0)
            lx, ly = label_point
            parts.append(
                f'<rect x="{fmt(lx - label_w / 2)}" y="{fmt(ly - 15)}" width="{fmt(label_w)}" height="24" rx="7" fill="#FFFFFF" stroke="{palette["line"]}"/>'
            )
            parts.append(
                f'<text x="{fmt(lx)}" y="{fmt(ly + 2)}" text-anchor="middle" font-size="12" fill="{palette["muted"]}">{xml_escape(label)}</text>'
            )

    for index, node in enumerate(spec["nodes"]):
        rect = positions[node["id"]]
        parts.append(render_node(node, rect, index, config, float(preset["card_radius"])))
    if layout.get("result"):
        parts.append(render_band(spec["result"], layout["result"], "result", config))
    parts.extend(["</g>", "</svg>"])
    return "\n".join(parts) + "\n"


def html_value(title: str, items: Iterable[str], step: str | None = None) -> str:
    head = f"<b>{html.escape(title)}</b>"
    if step:
        head = f"<span style='font-size:11px'><b>{html.escape(step)}</b></span>&nbsp;&nbsp;{head}"
    body = "".join(f"<div style='margin-top:6px'>• {html.escape(item)}</div>" for item in items)
    return f"<div>{head}{body}</div>"


def add_vertex(parent: ET.Element, cell_id: str, value: str, style: str, rect: Rect) -> None:
    cell = ET.SubElement(parent, "mxCell", id=cell_id, value=value, style=style, vertex="1", parent="1")
    ET.SubElement(
        cell,
        "mxGeometry",
        x=fmt(rect.x),
        y=fmt(rect.y),
        width=fmt(rect.w),
        height=fmt(rect.h),
        **{"as": "geometry"},
    )


def generate_drawio(spec: dict[str, Any], layout: dict[str, Any], config: dict[str, Any]) -> str:
    palette = config["palette"]
    mxfile = ET.Element("mxfile", host="app.diagrams.net", modified="2026-09-04T00:00:00.000Z", compressed="false")
    diagram = ET.SubElement(mxfile, "diagram", id="framework-diagram", name="框架图")
    model = ET.SubElement(
        diagram,
        "mxGraphModel",
        dx="1422",
        dy="794",
        grid="1",
        gridSize="10",
        guides="1",
        tooltips="1",
        connect="1",
        arrows="1",
        fold="1",
        page="1",
        pageScale="1",
        pageWidth=fmt(layout["width"]),
        pageHeight=fmt(layout["height"]),
        math="1",
        shadow="0",
    )
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")
    title_rect = Rect(78, 40, layout["width"] - 156, 45)
    add_vertex(
        root,
        "diagram_title",
        f"<b>{html.escape(spec['title'])}</b>",
        f"text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;fontFamily=Microsoft YaHei;fontSize=26;fontColor={palette['text']};",
        title_rect,
    )
    if spec.get("subtitle"):
        add_vertex(
            root,
            "diagram_subtitle",
            html.escape(spec["subtitle"]),
            f"text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;fontFamily=Microsoft YaHei;fontSize=13;fontColor={palette['muted']};",
            Rect(78, 82, layout["width"] - 156, 30),
        )
    for lane in layout.get("lanes", []):
        lane_rect: Rect = lane["rect"]
        add_vertex(
            root,
            f"lane_{lane['id']}",
            f"<b>{html.escape(lane['title'])}</b>",
            f"swimlane;html=1;horizontal=0;startSize={fmt(lane['label_w'])};rounded=1;arcSize=8;fillColor=#F8FAFC;swimlaneFillColor={palette['primary_fill']};strokeColor={palette['line']};fontFamily=Microsoft YaHei;fontSize=14;fontColor={palette['primary']};",
            lane_rect,
        )
    if layout.get("control"):
        add_vertex(
            root,
            "control_band",
            html_value(spec["control"]["title"], spec["control"].get("items", [])),
            f"rounded=1;arcSize=12;whiteSpace=wrap;html=1;align=left;verticalAlign=middle;spacing=16;fillColor={palette['condition_fill']};strokeColor={palette['condition']};strokeWidth=1.5;fontFamily=Microsoft YaHei;fontSize=13;fontColor={palette['text']};",
            layout["control"],
        )
    for index, node in enumerate(spec["nodes"]):
        stroke, fill = node_colors(node, config)
        add_vertex(
            root,
            f"node_{node['id']}",
            html_value(node["title"], node.get("items", []), str(node.get("step") or f"{index + 1:02d}")),
            f"rounded=1;arcSize=10;whiteSpace=wrap;html=1;align=left;verticalAlign=top;spacingTop=16;spacingLeft=16;spacingRight=14;spacingBottom=12;fillColor={fill};strokeColor={stroke};strokeWidth=1.5;fontFamily=Microsoft YaHei;fontSize=13;fontColor={palette['text']};",
            layout["nodes"][node["id"]],
        )
    for edge_index, edge in enumerate(spec.get("edges", [])):
        edge_cell = ET.SubElement(
            root,
            "mxCell",
            id=f"edge_{edge_index}",
            value=html.escape(edge.get("label", "")),
            style=f"edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;jettySize=auto;html=1;strokeColor={palette['muted']};strokeWidth=1.5;endArrow=block;endFill=1;endSize=7;fontFamily=Microsoft YaHei;fontSize=11;fontColor={palette['muted']};labelBackgroundColor=#FFFFFF;",
            edge="1",
            parent="1",
            source=f"node_{edge['from']}",
            target=f"node_{edge['to']}",
        )
        ET.SubElement(edge_cell, "mxGeometry", relative="1", **{"as": "geometry"})
    if layout.get("result"):
        add_vertex(
            root,
            "result_band",
            html_value(spec["result"]["title"], spec["result"].get("items", [])),
            f"rounded=1;arcSize=12;whiteSpace=wrap;html=1;align=left;verticalAlign=middle;spacing=16;fillColor={palette['result_fill']};strokeColor={palette['result']};strokeWidth=1.5;fontFamily=Microsoft YaHei;fontSize=13;fontColor={palette['text']};",
            layout["result"],
        )
    ET.indent(mxfile, space="  ")
    return ET.tostring(mxfile, encoding="unicode", xml_declaration=True) + "\n"


def mermaid_label(node: dict[str, Any]) -> str:
    parts = [node["title"], *node.get("items", [])]
    value = "<br/>".join(parts)
    return value.replace('"', "'").replace("[", "（").replace("]", "）")


def generate_mermaid(spec: dict[str, Any], config: dict[str, Any]) -> str:
    direction = "TB" if spec["layout"] in {"swimlane", "hierarchy"} else "LR"
    lines = [f"flowchart {direction}"]
    if spec.get("control"):
        section = spec["control"]
        label = "<br/>".join([section["title"], *section.get("items", [])]).replace('"', "'")
        lines.append(f'  control["{label}"]:::condition')
    if spec["layout"] == "swimlane":
        for lane in spec.get("lanes", []):
            lines.append(f'  subgraph lane_{lane["id"]}["{lane["title"]}"]')
            for node in spec["nodes"]:
                if node.get("lane") == lane["id"]:
                    lines.append(f'    {node["id"]}["{mermaid_label(node)}"]')
            lines.append("  end")
    else:
        for node in spec["nodes"]:
            lines.append(f'  {node["id"]}["{mermaid_label(node)}"]')
    for edge in spec.get("edges", []):
        label = edge.get("label", "").replace('"', "'")
        connector = f' -->|"{label}"| ' if label else " --> "
        lines.append(f'  {edge["from"]}{connector}{edge["to"]}')
    if spec.get("control") and spec["nodes"]:
        incoming = {edge["to"] for edge in spec.get("edges", [])}
        roots = [node["id"] for node in spec["nodes"] if node["id"] not in incoming]
        for node_id in roots[:4]:
            lines.append(f"  control -.-> {node_id}")
    if spec.get("result"):
        section = spec["result"]
        label = "<br/>".join([section["title"], *section.get("items", [])]).replace('"', "'")
        lines.append(f'  result["{label}"]:::result')
        outgoing = {edge["from"] for edge in spec.get("edges", [])}
        sinks = [node["id"] for node in spec["nodes"] if node["id"] not in outgoing]
        for node_id in sinks[:4]:
            lines.append(f"  {node_id} --> result")
    palette = config["palette"]
    lines.extend(
        [
            f"  classDef default fill:{palette['primary_fill']},stroke:{palette['primary']},color:{palette['text']},stroke-width:1.5px;",
            f"  classDef condition fill:{palette['condition_fill']},stroke:{palette['condition']},color:{palette['text']},stroke-width:1.5px;",
            f"  classDef result fill:{palette['result_fill']},stroke:{palette['result']},color:{palette['text']},stroke-width:1.5px;",
        ]
    )
    for node in spec["nodes"]:
        kind = node.get("kind", "default")
        if kind in {"input", "condition", "data"}:
            lines.append(f"  class {node['id']} condition;")
        elif kind in {"result", "conclusion"}:
            lines.append(f"  class {node['id']} result;")
    return "\n".join(lines) + "\n"


def discover_browser() -> Path | None:
    explicit = os.environ.get("FRAMEWORK_DIAGRAM_BROWSER")
    candidates = [Path(explicit)] if explicit else []
    if os.name == "nt":
        candidates.extend(
            [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
                Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
            ]
        )
    for executable in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(executable)
        if found:
            candidates.append(Path(found))
    return next((candidate for candidate in candidates if candidate and candidate.exists()), None)


def discover_codex_python() -> Path | None:
    candidates: list[Path] = []
    if os.name == "nt":
        candidates.append(
            Path.home()
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "python"
            / "python.exe"
        )
    else:
        candidates.append(
            Path.home()
            / ".cache"
            / "codex-runtimes"
            / "codex-primary-runtime"
            / "dependencies"
            / "python"
            / "bin"
            / "python"
        )
    return next((candidate for candidate in candidates if candidate.exists()), None)


def render_png(spec_path: Path, svg_path: Path, png_path: Path, width: int, height: int, scale: float) -> str:
    try:
        import cairosvg  # type: ignore

        cairosvg.svg2png(
            url=str(svg_path),
            write_to=str(png_path),
            output_width=max(1, round(width * scale)),
            output_height=max(1, round(height * scale)),
        )
        return "cairosvg"
    except (ImportError, OSError):
        pass

    try:
        from render_png import render_spec_to_png

        render_spec_to_png(spec_path, png_path, scale)
        return "pillow"
    except ImportError:
        bundled_python = discover_codex_python()
        if bundled_python:
            renderer = Path(__file__).resolve().parent / "render_png.py"
            completed = subprocess.run(
                [
                    str(bundled_python),
                    str(renderer),
                    "--spec",
                    str(spec_path),
                    "--output",
                    str(png_path),
                    "--scale",
                    str(scale),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
            )
            if completed.returncode == 0 and png_path.exists():
                return "codex-pillow"

    browser = discover_browser()
    if browser is None:
        raise RuntimeError(
            "找不到 CairoSVG、Chrome 或 Edge，无法生成 PNG。可安装浏览器，或设置 FRAMEWORK_DIAGRAM_BROWSER。"
        )
    with tempfile.TemporaryDirectory(prefix="framework-diagram-") as temp_name:
        temp_dir = Path(temp_name)
        profile_dir = temp_dir / "profile"
        html_path = temp_dir / "render.html"
        svg_text = svg_path.read_text(encoding="utf-8")
        html_path.write_text(
            "<!doctype html><html><head><meta charset='utf-8'><style>"
            "html,body{margin:0;padding:0;overflow:hidden;background:#fff;width:100%;height:100%;}"
            "svg{display:block;width:100vw;height:100vh;}"
            "</style></head><body>" + svg_text + "</body></html>",
            encoding="utf-8",
        )
        command = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--run-all-compositor-stages-before-draw",
            f"--force-device-scale-factor={scale}",
            f"--window-size={width},{height}",
            f"--user-data-dir={profile_dir}",
            f"--screenshot={png_path.resolve()}",
            html_path.resolve().as_uri(),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        if completed.returncode != 0 or not png_path.exists():
            detail = (completed.stderr or completed.stdout or "未知浏览器错误").strip()
            raise RuntimeError(f"浏览器 PNG 渲染失败：{detail[-1200:]}")
    return browser.name


def read_png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        signature = handle.read(24)
    if len(signature) < 24 or signature[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("文件不是有效 PNG。")
    return struct.unpack(">II", signature[16:24])


def safe_stem(value: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1F]+", "_", value.strip())
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    return cleaned[:90] or "framework_diagram"


def choose_stem(output_dir: Path, requested: str, overwrite: bool) -> str:
    stem = safe_stem(requested)
    if overwrite:
        return stem
    extensions = (".svg", ".png", ".drawio", ".mmd", ".spec.json", ".quality-report.json")
    if not any((output_dir / f"{stem}{extension}").exists() for extension in extensions):
        return stem
    version = 2
    while any((output_dir / f"{stem}_v{version}{extension}").exists() for extension in extensions):
        version += 1
    return f"{stem}_v{version}"


def verify_artifacts(paths: dict[str, Path], layout: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    metrics: dict[str, Any] = {}
    if "svg" in paths:
        try:
            ET.parse(paths["svg"])
        except Exception as exc:  # noqa: BLE001
            errors.append(f"SVG 无法解析：{exc}")
    if "drawio" in paths:
        try:
            tree = ET.parse(paths["drawio"])
            root = tree.getroot()
            vertex_count = len(root.findall(".//mxCell[@vertex='1']"))
            metrics["drawio_editable_vertices"] = vertex_count
            if vertex_count < len(layout["nodes"]):
                errors.append("draw.io 中的可编辑节点数量不足。")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"draw.io 无法解析：{exc}")
    if "png" in paths:
        try:
            png_width, png_height = read_png_size(paths["png"])
            metrics["png_width"] = png_width
            metrics["png_height"] = png_height
            if png_width < layout["width"] or png_height < layout["height"]:
                errors.append("PNG 分辨率低于 SVG 基础画布。")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"PNG 无法验证：{exc}")
    return errors, metrics


def write_report(path: Path, passed: bool, errors: list[str], warnings: list[str], metrics: dict[str, Any], outputs: dict[str, Path]) -> None:
    payload = {
        "passed": passed,
        "errors": errors,
        "warnings": warnings,
        "metrics": metrics,
        "outputs": {key: str(value.resolve()) for key, value in outputs.items()},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command_validate(args: argparse.Namespace) -> int:
    config = load_presets()
    try:
        spec = normalize_spec(load_json(args.spec))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"passed": False, "errors": [str(exc)], "warnings": []}, ensure_ascii=False, indent=2))
        return 2
    errors, warnings = validate_spec(spec, config)
    if not errors:
        preset = config["presets"][spec["canvas"]]
        layout = compute_layout(spec, preset)
        layout_errors, layout_warnings, metrics = analyze_layout(spec, layout)
        errors.extend(layout_errors)
        warnings.extend(layout_warnings)
    else:
        metrics = {}
    passed = not errors and not (args.strict and warnings)
    print(json.dumps({"passed": passed, "errors": errors, "warnings": warnings, "metrics": metrics}, ensure_ascii=False, indent=2))
    return 0 if passed else 2


def command_build(args: argparse.Namespace) -> int:
    config = load_presets()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    requested_name = args.name or args.spec.stem.replace(".spec", "")
    stem = choose_stem(output_dir, requested_name, args.overwrite)
    report_path = output_dir / f"{stem}.quality-report.json"
    outputs: dict[str, Path] = {"report": report_path}
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, Any] = {}

    try:
        spec = normalize_spec(load_json(args.spec))
        spec_errors, spec_warnings = validate_spec(spec, config)
        errors.extend(spec_errors)
        warnings.extend(spec_warnings)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"无法读取规格：{exc}")
        write_report(report_path, False, errors, warnings, metrics, outputs)
        print(f"构建失败；质检报告：{report_path}", file=sys.stderr)
        return 2

    if errors:
        write_report(report_path, False, errors, warnings, metrics, outputs)
        for error in errors:
            print(f"错误：{error}", file=sys.stderr)
        print(f"质检报告：{report_path}", file=sys.stderr)
        return 2

    preset = config["presets"][spec["canvas"]]
    layout = compute_layout(spec, preset)
    layout_errors, layout_warnings, layout_metrics = analyze_layout(spec, layout)
    errors.extend(layout_errors)
    warnings.extend(layout_warnings)
    metrics.update(layout_metrics)

    spec_path = output_dir / f"{stem}.spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    outputs["spec"] = spec_path
    formats = {item.strip().lower() for item in args.formats.split(",") if item.strip()}
    unknown = sorted(formats - SUPPORTED_FORMATS)
    if unknown:
        errors.append("未知输出格式：" + "、".join(unknown))
    formats &= SUPPORTED_FORMATS

    svg_text = generate_svg(spec, layout, config, preset)
    svg_path = output_dir / f"{stem}.svg"
    if "svg" in formats or "png" in formats:
        svg_path.write_text(svg_text, encoding="utf-8")
        outputs["svg"] = svg_path
    if "drawio" in formats:
        drawio_path = output_dir / f"{stem}.drawio"
        drawio_path.write_text(generate_drawio(spec, layout, config), encoding="utf-8")
        outputs["drawio"] = drawio_path
    if "mmd" in formats:
        mermaid_path = output_dir / f"{stem}.mmd"
        mermaid_path.write_text(generate_mermaid(spec, config), encoding="utf-8")
        outputs["mmd"] = mermaid_path
    if "png" in formats:
        png_path = output_dir / f"{stem}.png"
        try:
            metrics["png_renderer"] = render_png(
                spec_path,
                svg_path,
                png_path,
                int(layout["width"]),
                int(layout["height"]),
                args.png_scale,
            )
            outputs["png"] = png_path
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    artifact_errors, artifact_metrics = verify_artifacts(outputs, layout)
    errors.extend(artifact_errors)
    metrics.update(artifact_metrics)
    passed = not errors and not (args.strict and warnings)
    write_report(report_path, passed, errors, warnings, metrics, outputs)

    if errors:
        for error in errors:
            print(f"错误：{error}", file=sys.stderr)
    if warnings:
        for warning in warnings:
            print(f"警告：{warning}", file=sys.stderr)
    if passed:
        print("构建完成并通过自动质检：")
        for key, path in outputs.items():
            print(f"  {key}: {path}")
        return 0
    print(f"构建未通过质检；报告：{report_path}", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从标准 JSON 规格生成论文级数学建模框架图。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            示例：
              python diagram_toolkit.py validate --spec diagram.json --strict
              python diagram_toolkit.py build --spec diagram.json --output-dir output --name problem_2_framework --strict
            """
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="检查规格和自动布局，不写成图。")
    validate_parser.add_argument("--spec", type=Path, required=True, help="UTF-8 JSON 规格文件。")
    validate_parser.add_argument("--strict", action="store_true", help="把警告也视为不通过。")
    validate_parser.set_defaults(func=command_validate)

    build_parser_ = subparsers.add_parser("build", help="生成成图、可编辑源和质检报告。")
    build_parser_.add_argument("--spec", type=Path, required=True, help="UTF-8 JSON 规格文件。")
    build_parser_.add_argument("--output-dir", type=Path, required=True, help="输出目录。")
    build_parser_.add_argument("--name", help="输出文件主名；默认使用规格文件名。")
    build_parser_.add_argument(
        "--formats",
        default="svg,png,drawio,mmd",
        help="逗号分隔：svg,png,drawio,mmd。规范副本和质检报告始终生成。",
    )
    build_parser_.add_argument("--png-scale", type=float, default=1.5, help="PNG 缩放倍数，默认 1.5。")
    build_parser_.add_argument("--strict", action="store_true", help="把警告也视为不通过。")
    build_parser_.add_argument("--overwrite", action="store_true", help="覆盖同名输出；仅在用户明确要求时使用。")
    build_parser_.set_defaults(func=command_build)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if getattr(args, "png_scale", 1.0) <= 0:
        parser.error("--png-scale 必须大于 0。")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

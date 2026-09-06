#!/usr/bin/env python3
"""Pillow raster renderer used internally by diagram_toolkit.py."""

from __future__ import annotations

import argparse
import math
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

import diagram_toolkit as dt


def find_font(bold: bool = False) -> Path | None:
    windows = Path(r"C:\Windows\Fonts")
    regular_candidates = [
        windows / "msyh.ttc",
        windows / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    bold_candidates = [
        windows / "msyhbd.ttc",
        windows / "simhei.ttf",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc"),
        Path("/System/Library/Fonts/PingFang.ttc"),
    ]
    return next((path for path in (bold_candidates if bold else regular_candidates) if path.exists()), None)


class Raster:
    def __init__(self, width: int, height: int, scale: float, config: dict[str, Any]):
        self.scale = scale
        self.config = config
        self.palette = config["palette"]
        self.image = Image.new("RGB", (round(width * scale), round(height * scale)), self.palette["background"])
        self.draw = ImageDraw.Draw(self.image)
        self._fonts: dict[tuple[int, bool], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}

    def p(self, value: float) -> int:
        return round(value * self.scale)

    def xy(self, rect: dt.Rect) -> tuple[int, int, int, int]:
        return self.p(rect.x), self.p(rect.y), self.p(rect.right), self.p(rect.bottom)

    def font(self, size: int, bold: bool = False):
        key = (size, bold)
        if key not in self._fonts:
            path = find_font(bold)
            if path:
                self._fonts[key] = ImageFont.truetype(str(path), self.p(size))
            else:
                self._fonts[key] = ImageFont.load_default(size=self.p(size))
        return self._fonts[key]

    def text(self, xy: tuple[float, float], value: str, size: int, color: str, bold: bool = False, anchor: str = "la") -> None:
        self.draw.text(
            (self.p(xy[0]), self.p(xy[1])),
            value,
            font=self.font(size, bold),
            fill=color,
            anchor=anchor,
        )

    def line(self, points: list[tuple[float, float]], color: str, width: float = 1.0) -> None:
        self.draw.line([(self.p(x), self.p(y)) for x, y in points], fill=color, width=max(1, self.p(width)), joint="curve")

    def rounded_rect(self, rect: dt.Rect, radius: float, fill: str, outline: str, width: float = 1.0) -> None:
        self.draw.rounded_rectangle(
            self.xy(rect),
            radius=self.p(radius),
            fill=fill,
            outline=outline,
            width=max(1, self.p(width)),
        )


def draw_band(raster: Raster, section: dict[str, Any], rect: dt.Rect, role: str) -> None:
    p = raster.palette
    stroke = p["condition"] if role == "control" else p["result"]
    fill = p["condition_fill"] if role == "control" else p["result_fill"]
    raster.rounded_rect(rect, 14, fill, stroke, 1.5)
    title_w = min(230.0, rect.w * 0.19)
    raster.text((rect.x + 24, rect.y + rect.h / 2 + 2), section.get("title", ""), 18, stroke, True, "lm")
    raster.line([(rect.x + title_w, rect.y + 18), (rect.x + title_w, rect.bottom - 18)], stroke, 1)
    for item, chip in dt.band_item_rects(section, rect, role):
        raster.rounded_rect(chip, 9, "#FFFFFF", stroke, 1)
        lines = dt.wrap_text(item, chip.w - 28.0, 13.0)
        first_y = chip.cy - (len(lines) - 1) * 9.0
        for line_index, line in enumerate(lines):
            raster.text((chip.x + 14, first_y + line_index * 18.0), line, 13, p["text"], False, "lm")


def draw_lane(raster: Raster, lane: dict[str, Any]) -> None:
    p = raster.palette
    rect: dt.Rect = lane["rect"]
    label_w = lane["label_w"]
    raster.rounded_rect(rect, 14, "#F8FAFC", p["line"], 1)
    left = dt.Rect(rect.x, rect.y, label_w, rect.h)
    raster.rounded_rect(left, 14, p["primary_fill"], p["line"], 1)
    raster.line([(rect.x + label_w, rect.y), (rect.x + label_w, rect.bottom)], p["line"], 1)
    raster.text((rect.x + 22, rect.cy + 1), lane["title"], 17, p["primary"], True, "lm")


def path_points(path: str) -> list[tuple[float, float]]:
    numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", path)]
    return list(zip(numbers[0::2], numbers[1::2]))


def draw_arrow(raster: Raster, points: list[tuple[float, float]]) -> None:
    if len(points) < 2:
        return
    color = raster.palette["muted"]
    raster.line(points, color, 2)
    x2, y2 = points[-1]
    x1, y1 = points[-2]
    angle = math.atan2(y2 - y1, x2 - x1)
    length, half = 9.0, 4.2
    base_x = x2 - length * math.cos(angle)
    base_y = y2 - length * math.sin(angle)
    perp_x = half * math.sin(angle)
    perp_y = -half * math.cos(angle)
    polygon = [
        (raster.p(x2), raster.p(y2)),
        (raster.p(base_x + perp_x), raster.p(base_y + perp_y)),
        (raster.p(base_x - perp_x), raster.p(base_y - perp_y)),
    ]
    raster.draw.polygon(polygon, fill=color)


def draw_node(raster: Raster, node: dict[str, Any], rect: dt.Rect, index: int, radius: float, config: dict[str, Any]) -> None:
    p = raster.palette
    stroke, fill = dt.node_colors(node, config)
    raster.rounded_rect(rect, radius, fill, stroke, 1.5)
    step = str(node.get("step") or f"{index + 1:02d}")
    step_w = max(38.0, dt.text_units(step) * 12.0 + 16.0)
    step_rect = dt.Rect(rect.x + 20, rect.y + 18, step_w, 27)
    raster.rounded_rect(step_rect, 8, stroke, stroke, 1)
    raster.text((step_rect.cx, step_rect.cy + 1), step, 12, "#FFFFFF", True, "mm")
    title_x = rect.x + 24 + step_w + 12
    title_lines = dt.wrap_text(node["title"], rect.right - title_x - 18, 19.0)[:2]
    title_y = rect.y + 34
    for line_index, line in enumerate(title_lines):
        raster.text((title_x, title_y + line_index * 22), line, 19, p["text"], True, "lm")
    separator_y = rect.y + (68 if len(title_lines) > 1 else 58)
    raster.line([(rect.x + 22, separator_y), (rect.right - 22, separator_y)], stroke, 1)
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
            row_rect = dt.Rect(rect.x + 22, row_y, rect.w - 44, row_height)
            raster.rounded_rect(row_rect, 9, "#FFFFFF", stroke, 1)
            radius = raster.p(3.2)
            cx, cy = raster.p(rect.x + 39), raster.p(row_y + row_height / 2)
            raster.draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=stroke)
            lines = dt.wrap_text(item, rect.w - 82.0, 14.0) or [""]
            first_y = row_y + row_height / 2 - (len(lines) - 1) * 10.0
            for line_index, line in enumerate(lines):
                raster.text((rect.x + 51, first_y + line_index * 20.0), line, 14, p["text"], False, "lm")
            row_y += row_height + row_gap


def render_spec_to_png(spec_path: Path, output_path: Path, scale: float = 1.5) -> None:
    config = dt.load_presets()
    spec = dt.normalize_spec(dt.load_json(spec_path))
    errors, _ = dt.validate_spec(spec, config)
    if errors:
        raise ValueError("；".join(errors))
    preset = config["presets"][spec["canvas"]]
    layout = dt.compute_layout(spec, preset)
    raster = Raster(int(layout["width"]), int(layout["height"]), scale, config)
    p = raster.palette
    mx, my = float(preset["margin_x"]), float(preset["margin_y"])
    title_size = 34 if layout["height"] >= 1000 else 32
    raster.text((mx, my + 30), spec["title"], title_size, p["text"], True, "ls")
    if spec.get("subtitle"):
        raster.text((mx, my + 64), spec["subtitle"], 15, p["muted"], False, "ls")
    divider_y = my + float(preset["header_height"]) - 16
    raster.line([(mx, divider_y), (layout["width"] - mx, divider_y)], p["line"], 1)
    if layout.get("control"):
        draw_band(raster, spec["control"], layout["control"], "control")
    for lane in layout.get("lanes", []):
        draw_lane(raster, lane)
    positions: dict[str, dt.Rect] = layout["nodes"]
    for edge in spec.get("edges", []):
        path, label_point = dt.edge_path(positions[edge["from"]], positions[edge["to"]], spec["layout"])
        draw_arrow(raster, path_points(path))
        label = edge.get("label", "")
        if label:
            label_w = max(42.0, dt.text_units(label) * 12.0 + 20.0)
            lx, ly = label_point
            raster.rounded_rect(dt.Rect(lx - label_w / 2, ly - 15, label_w, 24), 7, "#FFFFFF", p["line"], 1)
            raster.text((lx, ly - 1), label, 12, p["muted"], False, "mm")
    for index, node in enumerate(spec["nodes"]):
        draw_node(raster, node, positions[node["id"]], index, float(preset["card_radius"]), config)
    if layout.get("result"):
        draw_band(raster, spec["result"], layout["result"], "result")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raster.image.save(output_path, "PNG", optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="使用 Pillow 渲染框架图 PNG。")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scale", type=float, default=1.5)
    args = parser.parse_args()
    render_spec_to_png(args.spec, args.output, args.scale)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

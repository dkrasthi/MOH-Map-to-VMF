#!/usr/bin/env python3
"""Convert MOHRadiant .map files to Valve Map Format (VMF)."""

from __future__ import annotations

import argparse
from collections import Counter
import math
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path


NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
TERRAIN_DEFAULT_GRID_SPACING = 64
POINT_RE = re.compile(
    rf"\s*\(\s*({NUMBER})\s+({NUMBER})\s+({NUMBER})\s*\)"
)
EPair_RE = re.compile(r'^\s*"((?:\\.|[^"])*)"\s+"((?:\\.|[^"])*)"\s*$')
COMMON_MATERIALS = {
    "common/nodraw": "TOOLS/TOOLSNODRAW",
    "common/caulk": "TOOLS/TOOLSNODRAW",
    "common/clip": "TOOLS/TOOLSPLAYERCLIP",
    "common/caulksky": "TOOLS/TOOLSSKYBOX",
    "common/playerclip": "TOOLS/TOOLSPLAYERCLIP",
    "common/hint": "TOOLS/TOOLSHINT",
    "common/clipall": "TOOLS/TOOLSCLIP",
    "common/caulkshadow": "TOOLS/TOOLSNODRAW",
    "common/black": "TOOLS/TOOLSBLACK",
    "common/bplaneclip": "TOOLS/TOOLSCLIP",
    "common/bspindleclip": "TOOLS/TOOLSCLIP",
    "common/origin": "TOOLS/TOOLSTRIGGER",
    "common/trigger": "TOOLS/TOOLSTRIGGER",
    "common/rain": "TOOLS/TOOLSTRIGGER",
    "common/vis": "TOOLS/TOOLSAREAPORTAL",
    "common/metalclip": "TOOLS/TOOLSCLIP",
    "common/dirtclip": "TOOLS/TOOLSCLIP",
}


@dataclass
class Face:
    points: list[tuple[float, float, float]]
    material: str
    shift_x: float
    shift_y: float
    rotation: float
    scale_x: float
    scale_y: float
    displacement: list[str] = field(default_factory=list)
    vertices_plus: list[tuple[float, float, float]] = field(default_factory=list)


@dataclass
class Brush:
    faces: list[Face] = field(default_factory=list)


@dataclass
class Terrain:
    width: int
    height: int
    origin: tuple[float, float, float]
    heights: list[float]
    material: str
    spacing_x: float = TERRAIN_DEFAULT_GRID_SPACING
    spacing_y: float = TERRAIN_DEFAULT_GRID_SPACING
    flags: int = 0


@dataclass
class Patch:
    width: int
    height: int
    material: str
    rows: list[list[tuple[float, float, float, float, float]]]


@dataclass
class Entity:
    properties: list[tuple[str, str]] = field(default_factory=list)
    brushes: list[Brush] = field(default_factory=list)
    terrains: list[Terrain] = field(default_factory=list)
    patches: list[Patch] = field(default_factory=list)


def strip_comment(line: str) -> str:
    quoted = False
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and line[index:index + 2] == "//":
            return line[:index]
    return line


def parse_face(line: str, path: Path, line_number: int) -> Face:
    points: list[tuple[float, float, float]] = []
    offset = 0
    for _ in range(3):
        match = POINT_RE.match(line, offset)
        if match is None:
            raise ValueError(f"{path}:{line_number}: invalid brush face")
        points.append(tuple(float(value) for value in match.groups()))
        offset = match.end()

    try:
        lexer = shlex.shlex(line[offset:].strip(), posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.quotes = '"'
        fields = list(lexer)
        if len(fields) < 6:
            raise ValueError("expected material and five texture parameters")
        material = fields[0].replace("\\", "/")
        shift_x, shift_y, rotation, scale_x, scale_y = map(float, fields[1:6])
    except (ValueError, IndexError) as error:
        raise ValueError(f"{path}:{line_number}: invalid brush face: {error}") from error
    if scale_x == 0:
        scale_x = 1.0
    if scale_y == 0:
        scale_y = 1.0
    return Face(points, material, shift_x, shift_y, rotation, scale_x, scale_y)


def parse_terrain(lines: list[str], start: int, path: Path) -> tuple[Terrain, int]:
    cursor = start

    def take() -> tuple[str, int]:
        nonlocal cursor
        while cursor < len(lines):
            line_number = cursor + 1
            line = strip_comment(lines[cursor]).strip()
            cursor += 1
            if line:
                return line, line_number
        raise ValueError(f"{path}: unexpected end of file in terrainDef")

    def expect(expected: str) -> None:
        line, line_number = take()
        if line != expected:
            raise ValueError(f"{path}:{line_number}: expected {expected!r} in terrainDef")

    expect("{")
    dimensions, dimension_line = take()
    dimension_values = dimensions.split()
    if len(dimension_values) not in (3, 4):
        raise ValueError(f"{path}:{dimension_line}: invalid terrain dimensions")
    try:
        width, height = map(int, dimension_values[:2])
    except ValueError as error:
        raise ValueError(f"{path}:{dimension_line}: invalid terrain dimensions") from error
    terrain_flags = 0
    try:
        if len(dimension_values) == 3:
            terrain_flags = int(dimension_values[2])
            spacing_x = spacing_y = TERRAIN_DEFAULT_GRID_SPACING
        else:
            spacing_x, spacing_y = map(float, dimension_values[2:4])
    except ValueError as error:
        raise ValueError(f"{path}:{dimension_line}: invalid terrain flags or spacing") from error
    if spacing_x <= 0 or spacing_y <= 0:
        raise ValueError(f"{path}:{dimension_line}: terrain spacing must be positive")
    if width < 2 or height < 2:
        raise ValueError(f"{path}:{dimension_line}: terrain dimensions must be at least 2x2")

    origin_line, origin_line_number = take()
    try:
        origin = tuple(float(value) for value in origin_line.split())
    except ValueError as error:
        raise ValueError(f"{path}:{origin_line_number}: invalid terrain origin") from error
    if len(origin) != 3:
        raise ValueError(f"{path}:{origin_line_number}: invalid terrain origin")

    expect("{")
    materials: Counter[str] = Counter()
    while True:
        line, line_number = take()
        if line == "}":
            break
        match = re.match(r"^\d+\s+\d+\s+\(\s*([^\s)]+)", line)
        if match:
            material = match.group(1).replace("\\", "/")
            if material.lower() != "notexture" and not material.lower().startswith("models/"):
                materials[material] += 1

    expect("{")
    heights: list[float] = []
    while True:
        line, line_number = take()
        if line == "}":
            break
        match = re.match(rf"^({NUMBER})\s+\(.*\)\s+\(.*\)$", line)
        if match is None:
            raise ValueError(f"{path}:{line_number}: invalid terrain height sample")
        heights.append(origin[2] + float(match.group(1)))
    expected_samples = width * height
    if len(heights) != expected_samples:
        raise ValueError(
            f"{path}:{dimension_line}: terrain has {len(heights)} heights; "
            f"expected {expected_samples} ({width}x{height})"
        )
    expect("}")
    material = materials.most_common(1)[0][0] if materials else "DEV/DEV_MEASUREGENERIC01B"
    return Terrain(width, height, origin, heights, material, spacing_x, spacing_y, terrain_flags), cursor


def parse_patch(lines: list[str], start: int, path: Path) -> tuple[Patch, int]:
    cursor = start

    def take() -> tuple[str, int]:
        nonlocal cursor
        while cursor < len(lines):
            line_number = cursor + 1
            line = strip_comment(lines[cursor]).strip()
            cursor += 1
            if line:
                return line, line_number
        raise ValueError(f"{path}: unexpected end of file in patchDef2")

    line, line_number = take()
    if line != "{":
        raise ValueError(f"{path}:{line_number}: expected '{{' after patchDef2")
    material, material_line = take()
    dimensions, dimension_line = take()
    dimension_match = re.match(r"^\(\s*(\d+)\s+(\d+)", dimensions)
    if dimension_match is None:
        raise ValueError(f"{path}:{dimension_line}: invalid patch dimensions")
    width, height = map(int, dimension_match.groups())

    rows: list[list[tuple[float, float, float, float, float]]] = []
    point_re = re.compile(rf"\(\s*({NUMBER})\s+({NUMBER})\s+({NUMBER})\s+({NUMBER})\s+({NUMBER})\s*\)")
    while True:
        line, line_number = take()
        if line == "(":
            continue
        if line == ")":
            break
        row = [tuple(float(value) for value in match.groups()) for match in point_re.finditer(line)]
        if row:
            rows.append(row)
    if len(rows) != width or any(len(row) != height for row in rows):
        raise ValueError(f"{path}:{dimension_line}: patch control-point grid does not match {width}x{height}")
    closing, closing_line = take()
    if closing != "}":
        raise ValueError(f"{path}:{closing_line}: expected '}}' after patchDef2")
    return Patch(width, height, material.replace("\\", "/"), rows), cursor


def parse_map(path: Path) -> list[Entity]:
    entities: list[Entity] = []
    entity: Entity | None = None
    brush: Brush | None = None
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    cursor = 0

    while cursor < len(lines):
        line_number = cursor + 1
        raw_line = lines[cursor]
        cursor += 1
        line = strip_comment(raw_line).strip()
        if not line:
            continue
        if brush is not None and line == "terrainDef":
            terrain, cursor = parse_terrain(lines, cursor, path)
            entity.terrains.append(terrain)
            continue
        if brush is not None and line == "patchDef2":
            patch, cursor = parse_patch(lines, cursor, path)
            entity.patches.append(patch)
            continue
        if line == "{":
            if entity is None:
                entity = Entity()
            elif brush is None:
                brush = Brush()
            else:
                raise ValueError(f"{path}:{line_number}: unexpected nested block")
            continue
        if line == "}":
            if brush is not None:
                valid_faces: list[Face] = []
                for face in brush.faces:
                    try:
                        plane_normal(face.points)
                    except ValueError:
                        continue
                    valid_faces.append(face)
                if len(valid_faces) >= 4:
                    entity.brushes.append(Brush(valid_faces))
                brush = None
            elif entity is not None:
                entities.append(entity)
                entity = None
            else:
                raise ValueError(f"{path}:{line_number}: unmatched closing brace")
            continue
        if brush is not None:
            if line.startswith("("):
                brush.faces.append(parse_face(line, path, line_number))
            else:
                raise ValueError(f"{path}:{line_number}: unsupported brush content")
        elif entity is not None:
            match = EPair_RE.match(line)
            if match is None:
                raise ValueError(f"{path}:{line_number}: unsupported entity property")
            entity.properties.append(tuple(value.replace('\\"', '"') for value in match.groups()))
        else:
            raise ValueError(f"{path}:{line_number}: content outside an entity")

    if brush is not None or entity is not None:
        raise ValueError(f"{path}: unterminated entity or brush")
    return entities


def fmt(value: float) -> str:
    if abs(value) < 0.0000005:
        value = 0.0
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def plane_normal(points: list[tuple[float, float, float]]) -> tuple[float, float, float]:
    first, second, third = points
    a = tuple(second[index] - first[index] for index in range(3))
    b = tuple(third[index] - first[index] for index in range(3))
    normal = (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )
    length = math.sqrt(sum(value * value for value in normal))
    if length == 0:
        raise ValueError("brush face has collinear points")
    return tuple(value / length for value in normal)


def texture_axes(face: Face) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    normal = plane_normal(face.points)
    if abs(normal[2]) > 0.9:
        u_axis = (1.0, 0.0, 0.0)
        v_axis = (0.0, -1.0, 0.0)
    else:
        u_axis = (normal[1], -normal[0], 0.0)
        length = math.sqrt(sum(value * value for value in u_axis))
        u_axis = tuple(value / length for value in u_axis)
        v_axis = (
            u_axis[1] * normal[2] - u_axis[2] * normal[1],
            u_axis[2] * normal[0] - u_axis[0] * normal[2],
            u_axis[0] * normal[1] - u_axis[1] * normal[0],
        )

    angle = math.radians(face.rotation)
    cosine, sine = math.cos(angle), math.sin(angle)
    rotated_u = tuple(u_axis[i] * cosine + v_axis[i] * sine for i in range(3))
    rotated_v = tuple(v_axis[i] * cosine - u_axis[i] * sine for i in range(3))
    return rotated_u, rotated_v


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_face(face: Face, side_id: int) -> list[str]:
    points = " ".join("(" + " ".join(fmt(value) for value in point) + ")" for point in face.points)
    u_axis, v_axis = texture_axes(face)
    material_key = face.material.lower()
    material = (
        "TOOLS/TOOLSSKYBOX"
        if material_key.startswith("sky/")
        else COMMON_MATERIALS.get(material_key, face.material)
    )
    output = [
        "\tside",
        "\t{",
        f'\t\t"id" "{side_id}"',
        f'\t\t"plane" "{points}"',
    ]
    if face.vertices_plus:
        output.extend(["\t\tvertices_plus", "\t\t{"])
        output.extend(
            f'\t\t\t"v" "{" ".join(fmt(value) for value in vertex)}"'
            for vertex in face.vertices_plus
        )
        output.append("\t\t}")
    output.extend([
        f'\t\t"material" {quote(material)}',
        f'\t\t"uaxis" "[{fmt(u_axis[0])} {fmt(u_axis[1])} {fmt(u_axis[2])} 0] 0.25"',
        f'\t\t"vaxis" "[{fmt(v_axis[0])} {fmt(v_axis[1])} {fmt(v_axis[2])} 0] 0.25"',
        '\t\t"rotation" "0"',
        '\t\t"lightmapscale" "16"',
        '\t\t"smoothing_groups" "0"',
        "\t}",
    ])
    if face.displacement:
        output[-1:-1] = face.displacement
    return output


def disp_grid_block(name: str, rows: list[list[str]]) -> list[str]:
    output = [f"\t\t\t\t{name}", "\t\t\t\t{"]
    output.extend(
        f'\t\t\t\t\t"row{row_index}" "{" ".join(values)}"'
        for row_index, values in enumerate(rows)
    )
    output.append("\t\t\t\t}")
    return output


def terrain_displacement(
    terrain: Terrain,
    start_x: int,
    start_y: int,
    power: int,
    base_z: float,
    base_height: float,
) -> list[str]:
    width = (1 << power) + 1
    normals = [["0", "0", "1"] * width for _ in range(width)]
    offsets = [["0", "0", "0"] * width for _ in range(width)]
    offset_normals = [["0", "0", "1"] * width for _ in range(width)]
    distances = [
        [
            fmt(
                terrain.heights[
                    (start_y + (column if terrain.flags > 0 else row)) * terrain.width
                    + start_x + (row if terrain.flags > 0 else column)
                ] - base_height
            )
            for column in range(width)
        ]
        for row in range(width)
    ]
    alphas = [["0"] * width for _ in range(width)]
    triangle_tags = [["9"] * ((width - 1) * 2) for _ in range(width - 1)]

    return [
        "\t\t\tdispinfo",
        "\t\t\t{",
        f'\t\t\t\t"power" "{power}"',
        f'\t\t\t\t"startposition" "[{fmt(terrain.origin[0] + start_x * terrain.spacing_x)} {fmt(terrain.origin[1] + start_y * terrain.spacing_y)} {fmt(base_z)}]"',
        '\t\t\t\t"flags" "0"',
        '\t\t\t\t"elevation" "0"',
        '\t\t\t\t"subdiv" "0"',
        *disp_grid_block("normals", normals),
        *disp_grid_block("distances", distances),
        *disp_grid_block("offsets", offsets),
        *disp_grid_block("offset_normals", offset_normals),
        *disp_grid_block("alphas", alphas),
        *disp_grid_block("triangle_tags", triangle_tags),
        "\t\t\t\tallowed_verts",
        "\t\t\t\t{",
        f'\t\t\t\t\t"{width + 1}" "' + " ".join(["-1"] * (width + 1)) + '"',
        "\t\t\t\t}",
        "\t\t\t}",
    ]


def write_terrain(terrain: Terrain, first_id: int) -> tuple[list[str], int, int]:
    cells_x = terrain.width - 1
    cells_y = terrain.height - 1
    power = next((candidate for candidate in range(4, 1, -1)
                  if cells_x % (1 << candidate) == 0 and cells_y % (1 << candidate) == 0), None)
    if power is None:
        raise ValueError(
            f"terrain dimensions {terrain.width}x{terrain.height} cannot be tiled "
            "into aligned VMF displacement patches (minimum power is 2)"
        )
    tile_cells = 1 << power
    output: list[str] = []
    next_id = first_id
    patch_count = 0
    for start_y in range(0, cells_y, tile_cells):
        for start_x in range(0, cells_x, tile_cells):
            x0 = terrain.origin[0] + start_x * terrain.spacing_x
            x1 = x0 + tile_cells * terrain.spacing_x
            y0 = terrain.origin[1] + start_y * terrain.spacing_y
            y1 = y0 + tile_cells * terrain.spacing_y
            tile_heights = [
                terrain.heights[(start_y + row) * terrain.width + start_x + column]
                for row in range(tile_cells + 1)
                for column in range(tile_cells + 1)
            ]
            base_z = terrain.origin[2]
            shell_z = base_z + (32 if terrain.flags > 0 else -32)
            displacement = terrain_displacement(
                terrain, start_x, start_y, power, base_z, base_z
            )
            p00 = (x0, y0, base_z)
            p01 = (x0, y1, base_z)
            p11 = (x1, y1, base_z)
            p10 = (x1, y0, base_z)
            if terrain.flags > 0:
                displacement_corners = [p00, p10, p11, p01]
            else:
                displacement_corners = [p00, p01, p11, p10]
            displacement_plane = displacement_corners[:3]
            faces = [Face(
                displacement_plane,
                terrain.material, 0, 0, 0, 0.25, 0.25, displacement,
                displacement_corners,
            )]
            outer00 = (x0, y0, shell_z)
            outer01 = (x0, y1, shell_z)
            outer11 = (x1, y1, shell_z)
            outer10 = (x1, y0, shell_z)
            brush_center = ((x0 + x1) / 2, (y0 + y1) / 2, (base_z + shell_z) / 2)
            boundary_faces = [
                [outer00, outer01, outer11, outer10],
                [p00, p10, outer10, outer00],
                [p10, p11, outer11, outer10],
                [p11, p01, outer01, outer11],
                [p01, p00, outer00, outer01],
            ]
            for points in boundary_faces:
                face_normal = plane_normal(points[:3])
                face_center = tuple(sum(point[axis] for point in points) / len(points) for axis in range(3))
                toward_center = tuple(brush_center[axis] - face_center[axis] for axis in range(3))
                if sum(face_normal[axis] * toward_center[axis] for axis in range(3)) > 0:
                    points.reverse()
                faces.append(Face(list(points[:3]), "TOOLS/TOOLSNODRAW", 0, 0, 0, 0.25, 0.25, vertices_plus=points))
            brush_output, next_id = write_brush(Brush(faces), next_id, next_id + 1)
            output.extend(brush_output)
            patch_count += 1
    return output, next_id, patch_count


def evaluate_patch(patch: Patch, u: float, v: float) -> tuple[float, float, float]:
    u_spans = (patch.width - 1) // 2
    v_spans = (patch.height - 1) // 2
    u_span = min(int(u), u_spans - 1)
    v_span = min(int(v), v_spans - 1)
    u_t = min(u - u_span, 1.0)
    v_t = min(v - v_span, 1.0)
    u_weights = ((1 - u_t) ** 2, 2 * u_t * (1 - u_t), u_t ** 2)
    v_weights = ((1 - v_t) ** 2, 2 * v_t * (1 - v_t), v_t ** 2)
    return tuple(
        sum(
            u_weights[i] * v_weights[j] * patch.rows[u_span * 2 + i][v_span * 2 + j][axis]
            for i in range(3)
            for j in range(3)
        )
        for axis in range(3)
    )


def patch_brushes(patch: Patch, steps_per_span: int = 2) -> list[Brush]:
    if patch.width < 3 or patch.height < 3 or patch.width % 2 == 0 or patch.height % 2 == 0:
        raise ValueError(f"unsupported quadratic patch grid {patch.width}x{patch.height}")
    u_spans = (patch.width - 1) // 2
    v_spans = (patch.height - 1) // 2
    u_samples = [index / steps_per_span for index in range(u_spans * steps_per_span + 1)]
    v_samples = [index / steps_per_span for index in range(v_spans * steps_per_span + 1)]
    points = [
        [evaluate_patch(patch, u, v) for v in v_samples]
        for u in u_samples
    ]
    brushes: list[Brush] = []
    for u_index in range(len(u_samples) - 1):
        for v_index in range(len(v_samples) - 1):
            top_left = points[u_index][v_index]
            top_right = points[u_index + 1][v_index]
            bottom_right = points[u_index + 1][v_index + 1]
            bottom_left = points[u_index][v_index + 1]
            for triangle in (
                (top_left, top_right, bottom_right),
                (top_left, bottom_right, bottom_left),
            ):
                try:
                    normal = plane_normal(list(triangle))
                except ValueError:
                    continue
                back = [tuple(point[axis] - normal[axis] * 2 for axis in range(3)) for point in triangle]
                vertices = [*triangle, *back]
                center = tuple(sum(point[axis] for point in vertices) / 6 for axis in range(3))
                polygons = [
                    (triangle[0], triangle[1], triangle[2]),
                    (back[2], back[1], back[0]),
                    (triangle[0], triangle[1], back[1], back[0]),
                    (triangle[1], triangle[2], back[2], back[1]),
                    (triangle[2], triangle[0], back[0], back[2]),
                ]
                faces: list[Face] = []
                for polygon_index, polygon in enumerate(polygons):
                    polygon_center = tuple(
                        sum(point[axis] for point in polygon) / len(polygon)
                        for axis in range(3)
                    )
                    face_normal = plane_normal(list(polygon[:3]))
                    to_center = tuple(center[axis] - polygon_center[axis] for axis in range(3))
                    if sum(face_normal[axis] * to_center[axis] for axis in range(3)) < 0:
                        polygon = tuple(reversed(polygon))
                    material = patch.material if polygon_index == 0 else "TOOLS/TOOLSNODRAW"
                    faces.append(Face(list(polygon[:3]), material, 0, 0, 0, 0.25, 0.25))
                brushes.append(Brush(faces))
    return brushes


def write_brush(brush: Brush, solid_id: int, next_id: int) -> tuple[list[str], int]:
    output = ["\tsolid", "\t{", f'\t\t"id" "{solid_id}"']
    for face in brush.faces:
        output.extend(write_face(face, next_id))
        next_id += 1
    output.extend(["\t}"])
    return output, next_id


def vmf_entity_properties(properties: list[tuple[str, str]]) -> list[tuple[str, str]]:
    property_values = dict(properties)
    if not property_values.get("classname", "").startswith("static_") or "model" not in property_values:
        return properties

    output: list[tuple[str, str]] = []
    for key, value in properties:
        if key == "classname":
            output.append((key, "prop_static"))
        elif key == "model":
            model = re.sub(r"/+", "/", value.replace("\\", "/")).lstrip("/")
            if model.lower().endswith(".tik"):
                model = model[:-4] + ".mdl"
            if not model.lower().startswith("models/"):
                model = "models/" + model
            output.append((key, model))
        elif key == "angle":
            if "angles" not in property_values:
                try:
                    angle = float(value)
                except ValueError:
                    output.append((key, value))
                else:
                    if angle == -1:
                        output.append(("angles", "-90 0 0"))
                    elif angle == -2:
                        output.append(("angles", "90 0 0"))
                    else:
                        output.append(("angles", f"0 {value} 0"))
        elif key == "scale":
            output.append(("uniformscale", value))
        else:
            output.append((key, value))
    return output


def to_vmf(entities: list[Entity]) -> tuple[str, int, int]:
    world_entities = [entity for entity in entities if dict(entity.properties).get("classname") == "worldspawn"]
    if len(world_entities) != 1:
        raise ValueError(f"expected one worldspawn entity, found {len(world_entities)}")
    world = world_entities[0]
    output = [
        "versioninfo", "{", '\t"editorversion" "400"', '\t"editorbuild" "8871"',
        '\t"mapversion" "1"', '\t"formatversion" "100"', '\t"prefab" "0"', "}",
        "visgroups", "{", "}", "viewsettings", "{", '\t"bSnapToGrid" "1"',
        '\t"bShowGrid" "1"', '\t"bShowLogicalGrid" "0"', '\t"nGridSpacing" "64"', "}",
        "world", "{", '\t"id" "1"', '\t"classname" "worldspawn"',
    ]
    next_id = 2
    for key, value in world.properties:
        if key != "classname":
            output.append(f"\t{quote(key)} {quote(value)}")
    brush_count = 0
    side_count = 0
    for brush in world.brushes:
        brush_output, next_id = write_brush(brush, next_id, next_id + 1)
        output.extend(brush_output)
        brush_count += 1
        side_count += len(brush.faces)
    for terrain in world.terrains:
        terrain_output, next_id, terrain_patches = write_terrain(terrain, next_id)
        output.extend(terrain_output)
        brush_count += terrain_patches
        side_count += terrain_patches * 6
    for patch in world.patches:
        for brush in patch_brushes(patch):
            brush_output, next_id = write_brush(brush, next_id, next_id + 1)
            output.extend(brush_output)
            brush_count += 1
            side_count += len(brush.faces)
    output.append("}")

    for entity in entities:
        if entity is world:
            continue
        output.extend(["entity", "{", f'\t"id" "{next_id}"'])
        next_id += 1
        for key, value in vmf_entity_properties(entity.properties):
            output.append(f"\t{quote(key)} {quote(value)}")
        for brush in entity.brushes:
            brush_output, next_id = write_brush(brush, next_id, next_id + 1)
            output.extend(brush_output)
            brush_count += 1
            side_count += len(brush.faces)
        for terrain in entity.terrains:
            terrain_output, next_id, terrain_patches = write_terrain(terrain, next_id)
            output.extend(terrain_output)
            brush_count += terrain_patches
            side_count += terrain_patches * 6
        for patch in entity.patches:
            for brush in patch_brushes(patch):
                brush_output, next_id = write_brush(brush, next_id, next_id + 1)
                output.extend(brush_output)
                brush_count += 1
                side_count += len(brush.faces)
        output.append("}")
    output.extend(["cameras", "{", '\t"activecamera" "-1"', "}", "cordons", "{", '\t"active" "0"', "}"])
    return "\n".join(output) + "\n", brush_count, side_count


def convert(source: Path, destination: Path) -> tuple[int, int, int]:
    entities = parse_map(source)
    vmf, brush_count, side_count = to_vmf(entities)
    destination.write_text(vmf, encoding="utf-8", newline="\n")
    return len(entities), brush_count, side_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="input MOHRadiant .map file")
    parser.add_argument("destination", type=Path, nargs="?", help="output .vmf (default: source with .vmf extension)")
    args = parser.parse_args()
    destination = args.destination or args.source.with_suffix(".vmf")
    try:
        entity_count, brush_count, side_count = convert(args.source, destination)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Wrote {destination}: {entity_count} entities, {brush_count} brushes, {side_count} sides")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
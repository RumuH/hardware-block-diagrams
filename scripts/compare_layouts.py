#!/usr/bin/env python3
"""Compare two author-authored hardware layouts without changing either one."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagram


EPS = 1e-8


class ComparisonError(Exception):
    pass


def _point_close(a, b):
    return math.dist(a, b) <= EPS


def _compact(points):
    """Remove duplicate points and points inside a straight, forward segment."""
    result = []
    for raw in points:
        point = (float(raw[0]), float(raw[1]))
        if result and _point_close(result[-1], point):
            continue
        result.append(point)
        while len(result) >= 3:
            a, b, c = result[-3:]
            ab = (b[0] - a[0], b[1] - a[1])
            bc = (c[0] - b[0], c[1] - b[1])
            cross = ab[0] * bc[1] - ab[1] * bc[0]
            dot = ab[0] * bc[0] + ab[1] * bc[1]
            if abs(cross) <= EPS and dot >= -EPS:
                result.pop(-2)
            else:
                break
    return result


def _path(cid, edge, hardware, nodes):
    connection = next(c for c in hardware["connections"] if c["id"] == cid)
    source = connection["source"].split(".")[0]
    target = connection["target"].split(".")[0]
    start = diagram.anchor(diagram._rect(nodes[source]), edge["source_side"], edge.get("source_pos", .5))
    finish = diagram.anchor(diagram._rect(nodes[target]), edge["target_side"], edge.get("target_pos", .5))
    return _compact([start, *edge.get("points", []), finish])


def _path_metrics(path):
    vectors = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:])]
    bends = 0
    for first, second in zip(vectors, vectors[1:]):
        cross = first[0] * second[1] - first[1] * second[0]
        dot = first[0] * second[0] + first[1] * second[1]
        if abs(cross) > EPS or dot <= EPS:
            bends += 1
    nonorthogonal = sum(abs(dx) > EPS and abs(dy) > EPS for dx, dy in vectors)
    return {
        "bends": bends,
        "length": round(sum(math.hypot(dx, dy) for dx, dy in vectors), 6),
        "nonorthogonal_segments": nonorthogonal,
        "polyline_points": len(path),
    }


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _segment_intersection(a, b, c, d):
    """Return (kind, value): a point or a positive-length overlap segment."""
    r, s = (b[0] - a[0], b[1] - a[1]), (d[0] - c[0], d[1] - c[1])
    denominator = _cross(r, s)
    offset = (c[0] - a[0], c[1] - a[1])
    if abs(denominator) > EPS:
        t, u = _cross(offset, s) / denominator, _cross(offset, r) / denominator
        if -EPS <= t <= 1 + EPS and -EPS <= u <= 1 + EPS:
            return "point", (a[0] + t * r[0], a[1] + t * r[1])
        return None, None
    if abs(_cross(offset, r)) > EPS:
        return None, None
    length2 = r[0] * r[0] + r[1] * r[1]
    if length2 <= EPS:
        return ("point", a) if diagram._segment_hits_rect(a, a, (c[0], c[1], d[0], d[1])) else (None, None)
    t0 = (offset[0] * r[0] + offset[1] * r[1]) / length2
    t1 = t0 + (s[0] * r[0] + s[1] * r[1]) / length2
    low, high = max(0.0, min(t0, t1)), min(1.0, max(t0, t1))
    if high < low - EPS:
        return None, None
    start = (a[0] + low * r[0], a[1] + low * r[1])
    end = (a[0] + high * r[0], a[1] + high * r[1])
    if _point_close(start, end):
        return "point", start
    return "overlap", (start, end)


def _key(point):
    return round(point[0], 7), round(point[1], 7)


def _shared_declared_endpoint(cid, other, hardware, paths, point):
    connections = {c["id"]: c for c in hardware["connections"]}
    first, second = connections[cid], connections[other]
    endpoints = (("source", 0), ("target", -1))
    for role, index in endpoints:
        for other_role, other_index in endpoints:
            if first[role] == second[other_role] and _point_close(paths[cid][index], point) and _point_close(paths[other][other_index], point):
                return True
    return False


def _interedge_metrics(paths, hardware):
    crossings = []
    overlaps = []
    edge_ids = sorted(paths)
    for index, cid in enumerate(edge_ids):
        for other in edge_ids[index + 1:]:
            points = set()
            spans = {}
            for a, b in zip(paths[cid], paths[cid][1:]):
                for c, d in zip(paths[other], paths[other][1:]):
                    kind, value = _segment_intersection(a, b, c, d)
                    if kind == "point" and not _shared_declared_endpoint(cid, other, hardware, paths, value):
                        points.add(_key(value))
                    elif kind == "overlap":
                        p, q = value
                        key = tuple(sorted((_key(p), _key(q))))
                        spans[key] = math.dist(p, q)
            for point in sorted(points):
                crossings.append({"edges": [cid, other], "point": list(point)})
            if spans:
                overlaps.append({"edges": [cid, other], "length": round(sum(spans.values()), 6)})
    return {
        "unique_crossings": len(crossings),
        "crossings": crossings,
        "collinear_overlap_pairs": len(overlaps),
        "collinear_overlap_length": round(sum(item["length"] for item in overlaps), 6),
        "overlaps": overlaps,
    }


def _node_overlaps(nodes, modules):
    result = []
    ids = sorted(nodes)
    for index, first in enumerate(ids):
        for second in ids[index + 1:]:
            if diagram._ancestor(first, second, modules) or diagram._ancestor(second, first, modules):
                continue
            if diagram._intersection(diagram._rect(nodes[first]), diagram._rect(nodes[second])):
                result.append([first, second])
    return result


def _candidate(layout, hardware, semantic):
    checked = diagram.validate_layout(layout, hardware, semantic)
    paths = {}
    for cid, edge in checked["edges"].items() if not semantic["errors"] else ():
        if any(error.startswith(f"edges.{cid}.") or error.startswith(f"edges.{cid}:") for error in checked["errors"]):
            continue
        connection = semantic["connections"].get(cid)
        if connection is None:
            continue
        source = connection["source"].split(".")[0]
        target = connection["target"].split(".")[0]
        if source in checked["nodes"] and target in checked["nodes"]:
            paths[cid] = _path(cid, edge, hardware, checked["nodes"])
    per_edge = {cid: _path_metrics(path) for cid, path in sorted(paths.items())}
    interactions = _interedge_metrics(paths, hardware)
    unrelated = set()
    for warning in checked["warnings"]:
        marker = ": crosses unrelated module "
        if warning.startswith("edges.") and marker in warning:
            cid, module = warning.split(marker, 1)
            unrelated.add((cid.removeprefix("edges."), module))
    overlaps = _node_overlaps(checked["nodes"], semantic["modules"])
    canvas = layout.get("canvas", {}) if isinstance(layout, dict) else {}
    if not isinstance(canvas, dict):
        canvas = {}
    width, height = canvas.get("width"), canvas.get("height")
    area = width * height if diagram._number(width) and diagram._number(height) else None
    total_length = round(sum(item["length"] for item in per_edge.values()), 6)
    return {
        "status": "invalid" if semantic["errors"] or checked["errors"] else "ok",
        "validation": {"errors": [*semantic["errors"], *checked["errors"]], "warnings": checked["warnings"]},
        "metrics": {
            "canvas": {"width": width, "height": height, "area": area},
            "paths": {
                "edge_count": len(per_edge),
                "total_bends": sum(item["bends"] for item in per_edge.values()),
                "total_length": total_length,
                "longest_length": max((item["length"] for item in per_edge.values()), default=0),
                "nonorthogonal_segments": sum(item["nonorthogonal_segments"] for item in per_edge.values()),
                "per_edge": per_edge,
            },
            "interedge": interactions,
            "unrelated_node_intersections": {
                "count": len(unrelated),
                "pairs": [{"edge": edge, "node": node} for edge, node in sorted(unrelated)],
            },
            "node_overlaps": {"count": len(overlaps), "pairs": overlaps},
        },
    }


def _changes(before, after):
    moved, resized, sides = [], [], []
    before_nodes = before.get("nodes", {}) if isinstance(before, dict) else {}
    after_nodes = after.get("nodes", {}) if isinstance(after, dict) else {}
    if not isinstance(before_nodes, dict):
        before_nodes = {}
    if not isinstance(after_nodes, dict):
        after_nodes = {}
    for ident in sorted(before_nodes.keys() & after_nodes.keys()):
        old, new = before_nodes[ident], after_nodes[ident]
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        if all(diagram._number(value) for value in (old.get("x"), old.get("y"), new.get("x"), new.get("y"))):
            if old["x"] != new["x"] or old["y"] != new["y"]:
                moved.append({"id": ident, "from": [old["x"], old["y"]], "to": [new["x"], new["y"]], "delta": [new["x"] - old["x"], new["y"] - old["y"]]})
        if all(diagram._number(value) for value in (old.get("w"), old.get("h"), new.get("w"), new.get("h"))):
            if old["w"] != new["w"] or old["h"] != new["h"]:
                resized.append({"id": ident, "from": [old["w"], old["h"]], "to": [new["w"], new["h"]]})
    before_edges = before.get("edges", {}) if isinstance(before, dict) else {}
    after_edges = after.get("edges", {}) if isinstance(after, dict) else {}
    if not isinstance(before_edges, dict):
        before_edges = {}
    if not isinstance(after_edges, dict):
        after_edges = {}
    for ident in sorted(before_edges.keys() & after_edges.keys()):
        old_edge, new_edge = before_edges[ident], after_edges[ident]
        if not isinstance(old_edge, dict) or not isinstance(new_edge, dict):
            continue
        old = [old_edge.get("source_side"), old_edge.get("target_side")]
        new = [new_edge.get("source_side"), new_edge.get("target_side")]
        if old != new:
            sides.append({"id": ident, "from": old, "to": new})
    return {"nodes": {"moved": moved, "resized": resized}, "edges": {"changed_sides": sides}}


def compare_layouts(hardware, before, after):
    semantic = diagram.validate_hardware(hardware)
    first = _candidate(before, hardware, semantic)
    second = _candidate(after, hardware, semantic)
    before_paths, after_paths = first["metrics"]["paths"], second["metrics"]["paths"]
    metrics_comparable = not semantic["errors"] and first["status"] == second["status"] == "ok"
    bends_comparable = metrics_comparable and before_paths["nonorthogonal_segments"] == after_paths["nonorthogonal_segments"] == 0
    def delta(path):
        if not metrics_comparable:
            return None
        old, new = first["metrics"], second["metrics"]
        for part in path:
            old, new = old[part], new[part]
        return round(new - old, 6) if isinstance(old, (int, float)) and isinstance(new, (int, float)) else None
    report = {
        "schema_version": 1,
        "status": "invalid" if semantic["errors"] or first["status"] == "invalid" or second["status"] == "invalid" else "ok",
        "assessment": "Metrics describe tradeoffs only; they do not establish visual acceptance or a globally optimal layout.",
        "identity": {
            "same_hardware": True,
            "module_ids": sorted(semantic["modules"]),
            "connection_ids": sorted(semantic["connections"]),
        },
        "hardware_validation": {"errors": semantic["errors"], "warnings": semantic["warnings"]},
        "before": first,
        "after": second,
        "changes": _changes(before, after),
        "comparison": {
            "bends_comparable": bends_comparable,
            "bends_comparison_reason": None if bends_comparable else "Bend comparison requires two structurally valid layouts with exclusively orthogonal segments.",
            "deltas_after_minus_before": {
                "canvas_area": delta(["canvas", "area"]),
                "path_total_bends": delta(["paths", "total_bends"]) if bends_comparable else None,
                "path_total_length": delta(["paths", "total_length"]),
                "path_longest_length": delta(["paths", "longest_length"]),
                "nonorthogonal_segments": delta(["paths", "nonorthogonal_segments"]),
                "unique_crossings": delta(["interedge", "unique_crossings"]),
                "collinear_overlap_pairs": delta(["interedge", "collinear_overlap_pairs"]),
                "collinear_overlap_length": delta(["interedge", "collinear_overlap_length"]),
                "unrelated_node_intersections": delta(["unrelated_node_intersections", "count"]),
                "node_overlaps": delta(["node_overlaps", "count"]),
            },
        },
    }
    return report


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".layout-comparison-", suffix=".json", delete=False) as handle:
            staged = Path(handle.name)
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(staged, path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _reject_output_alias(report, inputs):
    if report is None:
        return
    resolved = report.resolve()
    for source in inputs:
        if resolved == source.resolve() or report.exists() and source.exists() and report.samefile(source):
            raise ComparisonError("report must not alias a hardware or layout input")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hardware", type=Path)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        _reject_output_alias(args.report, [args.hardware, args.before, args.after])
        hardware = diagram.read_data(args.hardware)
        before = diagram.read_data(args.before)
        after = diagram.read_data(args.after)
        report = compare_layouts(hardware, before, after)
        if args.report:
            _write_json(args.report, report)
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if report["status"] == "ok" else 1
    except (ComparisonError, diagram.DiagramError, OSError, ValueError) as exc:
        print(f"layout comparison failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

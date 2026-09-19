#!/usr/bin/env python3
"""Route fixed hardware layouts with bounded, deterministic orthogonal paths.

The hardware document and node geometry are inputs, never placement targets.  A
failed edge makes the entire output fail rather than publishing a partial layout.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import heapq
import json
import math
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagram


NORMAL = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}
DIR = {(1, 0): 0, (-1, 0): 1, (0, 1): 2, (0, -1): 3}
EPS = 1e-8
MAX_GRID_POINTS = 30_000


class RoutingError(Exception):
    pass


def _rect(node):
    return node["x"], node["y"], node["x"] + node["w"], node["y"] + node["h"]


def _anchor(node, side, fraction):
    return diagram.anchor(_rect(node), side, fraction)


def _mid(connection, role):
    return connection[role].split(".")[0]


def _spread(layout, hardware):
    groups = {}
    connections = {c["id"]: c for c in hardware["connections"]}
    for cid, edge in layout["edges"].items():
        c = connections[cid]
        for role in ("source", "target"):
            module = _mid(c, role)
            side = edge[role + "_side"]
            other = _mid(c, "target" if role == "source" else "source")
            box = layout["nodes"][other]
            axis = (box["y"] + box["h"] / 2) if side in {"left", "right"} else (box["x"] + box["w"] / 2)
            groups.setdefault((module, side), []).append((axis, cid, role))
    moved = []
    for key in sorted(groups):
        entries = sorted(groups[key])
        for i, (_, cid, role) in enumerate(entries):
            field = role + "_pos"
            old = layout["edges"][cid].get(field, .5)
            new = (i + 1) / (len(entries) + 1)
            layout["edges"][cid][field] = new
            if abs(old - new) > EPS:
                moved.append({"edge": cid, "endpoint": role, "module": key[0], "side": key[1], "from": old, "to": new})
    return moved


def _hits(a, b, rect):
    l, t, r, bottom = rect
    if abs(a[1] - b[1]) < EPS:
        return t + EPS < a[1] < bottom - EPS and max(a[0], b[0]) > l + EPS and min(a[0], b[0]) < r - EPS
    if abs(a[0] - b[0]) < EPS:
        return l + EPS < a[0] < r - EPS and max(a[1], b[1]) > t + EPS and min(a[1], b[1]) < bottom - EPS
    return True


def _allowed(a, b, obstacles, width, height):
    if not all(-EPS <= x <= width + EPS and -EPS <= y <= height + EPS for x, y in (a, b)):
        return False
    return not any(_hits(a, b, rect) for rect in obstacles)


def _cross_cost(a, b, prior):
    cost = 0.0
    for p, q in prior:
        horizontal = abs(a[1] - b[1]) < EPS
        phorizontal = abs(p[1] - q[1]) < EPS
        if horizontal == phorizontal:
            if horizontal and abs(a[1] - p[1]) < EPS:
                overlap = min(max(a[0], b[0]), max(p[0], q[0])) - max(min(a[0], b[0]), min(p[0], q[0]))
            elif not horizontal and abs(a[0] - p[0]) < EPS:
                overlap = min(max(a[1], b[1]), max(p[1], q[1])) - max(min(a[1], b[1]), min(p[1], q[1]))
            else:
                overlap = 0
            cost += max(0, overlap) * .5
        elif horizontal:
            if min(a[0], b[0]) + EPS < p[0] < max(a[0], b[0]) - EPS and min(p[1], q[1]) + EPS < a[1] < max(p[1], q[1]) - EPS:
                cost += 8
        elif min(a[1], b[1]) + EPS < p[1] < max(a[1], b[1]) - EPS and min(p[0], q[0]) + EPS < a[0] < max(p[0], q[0]) - EPS:
            cost += 8
    return cost


def _compact(points):
    result = []
    for p in points:
        if result and math.dist(result[-1], p) < EPS:
            continue
        result.append(p)
        while len(result) >= 3:
            a, b, c = result[-3:]
            if (abs(a[0] - b[0]) < EPS and abs(b[0] - c[0]) < EPS and (b[1] - a[1]) * (c[1] - b[1]) >= 0) or (abs(a[1] - b[1]) < EPS and abs(b[1] - c[1]) < EPS and (b[0] - a[0]) * (c[0] - b[0]) >= 0):
                result.pop(-2)
            else:
                break
    return result


def _route(start, finish, source_side, target_side, obstacles, width, height, clearance, prior):
    sn = NORMAL[source_side]
    tn = NORMAL[target_side]
    delta = (finish[0] - start[0], finish[1] - start[1])
    forward = delta[0] * sn[0] + delta[1] * sn[1]
    perpendicular = delta[0] * sn[1] - delta[1] * sn[0]
    if tn == (-sn[0], -sn[1]) and abs(perpendicular) < EPS and forward > EPS and _allowed(start, finish, obstacles, width, height):
        # An aligned gap can be shorter than the lead length without a jog.
        return [start, finish]
    if tn == (-sn[0], -sn[1]) and abs(perpendicular) >= EPS and forward < 2 * clearance - EPS:
        raise RoutingError("gap too narrow for the required endpoint leads")
    # Every bent route begins and ends with a full, visible perpendicular lead.
    stub = clearance
    a = (start[0] + sn[0] * stub, start[1] + sn[1] * stub)
    b = (finish[0] + tn[0] * stub, finish[1] + tn[1] * stub)
    if not _allowed(start, a, obstacles, width, height) or not _allowed(b, finish, obstacles, width, height):
        raise RoutingError("attachment is blocked or faces outside canvas")
    xs = {0.0, float(width), a[0], b[0]}
    ys = {0.0, float(height), a[1], b[1]}
    for l, t, r, bottom in obstacles:
        xs.update((max(0.0, l), min(width, r)))
        ys.update((max(0.0, t), min(height, bottom)))
    xx, yy = sorted(xs), sorted(ys)
    if len(xx) * len(yy) > MAX_GRID_POINTS:
        raise RoutingError(f"visibility grid exceeds {MAX_GRID_POINTS} points")
    ix = {v: i for i, v in enumerate(xx)}
    iy = {v: i for i, v in enumerate(yy)}
    origin = (ix[a[0]], iy[a[1]], DIR[sn])
    goal = (ix[b[0]], iy[b[1]])
    best = {origin: (0, stub, 0.0, stub)}
    predecessor = {}
    queue = [(best[origin], origin)]
    terminal = None
    terminal_cost = None
    while queue:
        cost, state = heapq.heappop(queue)
        if best.get(state) != cost:
            continue
        if terminal_cost is not None and cost >= terminal_cost:
            break
        i, j, direction = state
        if (i, j) == goal:
            final_dir = DIR[(-tn[0], -tn[1])]
            if direction != (final_dir ^ 1):
                final_cost = (cost[0] + (direction != final_dir), cost[1] + stub, cost[2], cost[3] + stub)
                if terminal_cost is None or final_cost < terminal_cost:
                    terminal, terminal_cost = state, final_cost
            continue
        p = (xx[i], yy[j])
        for ni, nj, heading in ((i - 1, j, 1), (i + 1, j, 0), (i, j - 1, 3), (i, j + 1, 2)):
            if not (0 <= ni < len(xx) and 0 <= nj < len(yy)):
                continue
            if heading == (direction ^ 1):
                continue
            q = (xx[ni], yy[nj])
            if not _allowed(p, q, obstacles, width, height):
                continue
            length = math.dist(p, q)
            crossing = _cross_cost(p, q, prior)
            nc = (cost[0] + (direction != heading), cost[1] + length + crossing, cost[2] + crossing, cost[3] + length)
            ns = (ni, nj, heading)
            if ns not in best or nc < best[ns]:
                best[ns] = nc
                predecessor[ns] = state
                heapq.heappush(queue, (nc, ns))
    if terminal is None:
        raise RoutingError("no obstacle-free orthogonal path within canvas")
    indices = []
    state = terminal
    while state != origin:
        indices.append(state)
        state = predecessor[state]
    indices.append(origin)
    route = [start] + [(xx[i], yy[j]) for i, j, _ in reversed(indices)] + [finish]
    route = _compact(route)
    if any(not _allowed(p, q, obstacles, width, height) for p, q in zip(route, route[1:])):
        raise RoutingError("route simplification crossed an obstacle")
    if any(abs(p[0]-q[0]) > EPS and abs(p[1]-q[1]) > EPS for p, q in zip(route, route[1:])):
        raise RoutingError("route simplification created a diagonal")
    if len(route) > 2 and (math.dist(route[0], route[1]) < clearance - EPS or math.dist(route[-2], route[-1]) < clearance - EPS):
        raise RoutingError("route has a terminal segment shorter than clearance")
    return route


def route_layout(hardware, layout, *, ports="preserve", clearance=16.0):
    """Return (new_layout, report). Report unresolved edges instead of partial success."""
    if ports not in {"preserve", "spread"}:
        raise RoutingError("ports must be preserve or spread")
    if isinstance(clearance, bool) or not isinstance(clearance, (int, float)) or not math.isfinite(clearance) or clearance <= 0:
        raise RoutingError("clearance must be a finite positive number")
    semantic = diagram.validate_hardware(hardware)
    validated = diagram.validate_layout(layout, hardware, semantic)
    if semantic["errors"] or validated["errors"]:
        raise RoutingError("invalid input:\n" + "\n".join(semantic["errors"] + validated["errors"]))
    out = deepcopy(layout)
    moved = _spread(out, hardware) if ports == "spread" else []
    modules = semantic["modules"]
    nodes = out["nodes"]
    width, height = out["canvas"]["width"], out["canvas"]["height"]
    report = {"status": "ok", "ports": ports, "clearance": clearance, "moved_attachments": moved, "edges": {}, "total_bends": 0, "unresolved": [], "labels_to_review": []}
    prior = []
    segments_by_edge = {}
    for c in hardware["connections"]:
        cid = c["id"]
        edge = out["edges"][cid]
        src, dst = _mid(c, "source"), _mid(c, "target")
        a = _anchor(nodes[src], edge["source_side"], edge.get("source_pos", .5))
        b = _anchor(nodes[dst], edge["target_side"], edge.get("target_pos", .5))
        obstacles = []
        for mid, node in nodes.items():
            if mid in {src, dst}:
                obstacles.append(_rect(node))
            elif diagram._ancestor(src, mid, modules) or diagram._ancestor(dst, mid, modules):
                continue
            else:
                l, t, r, bottom = _rect(node)
                obstacles.append((l-clearance, t-clearance, r+clearance, bottom+clearance))
        try:
            path = _route(a, b, edge["source_side"], edge["target_side"], obstacles, width, height, clearance, prior)
            edge["points"] = [[x, y] for x, y in path[1:-1]]
            bends = max(0, len(path)-2)
            report["edges"][cid] = {"bends": bends, "length": round(sum(math.dist(p, q) for p, q in zip(path, path[1:])), 6), "points": len(edge["points"])}
            report["total_bends"] += bends
            segments_by_edge[cid] = list(zip(path, path[1:]))
            prior.extend(segments_by_edge[cid])
            if "label_at" in edge:
                report["labels_to_review"].append(cid)
        except RoutingError as exc:
            report["edges"][cid] = {"unresolved": str(exc)}
            report["unresolved"].append(cid)
    if report["unresolved"]:
        report["status"] = "unresolved"
        return None, report
    crossings, overlaps = 0, 0
    ids = list(segments_by_edge)
    for i, cid in enumerate(ids):
        for other in ids[i+1:]:
            for a, b in segments_by_edge[cid]:
                for p, q in segments_by_edge[other]:
                    ah = abs(a[1] - b[1]) < EPS
                    ph = abs(p[1] - q[1]) < EPS
                    if ah == ph:
                        if (ah and abs(a[1]-p[1]) < EPS and min(max(a[0],b[0]),max(p[0],q[0])) > max(min(a[0],b[0]),min(p[0],q[0])) + EPS) or (not ah and abs(a[0]-p[0]) < EPS and min(max(a[1],b[1]),max(p[1],q[1])) > max(min(a[1],b[1]),min(p[1],q[1])) + EPS):
                            overlaps += 1
                    elif ah:
                        if min(a[0],b[0])+EPS < p[0] < max(a[0],b[0])-EPS and min(p[1],q[1])+EPS < a[1] < max(p[1],q[1])-EPS:
                            crossings += 1
                    elif min(a[1],b[1])+EPS < p[1] < max(a[1],b[1])-EPS and min(p[0],q[0])+EPS < a[0] < max(p[0],q[0])-EPS:
                        crossings += 1
    report["crossings"] = crossings
    report["overlap_pairs"] = overlaps
    report["needs_visual_review"] = bool(crossings or overlaps or report["labels_to_review"])
    check = diagram.validate_layout(out, hardware, semantic)
    fatal = [w for w in check["warnings"] if any(s in w for s in ("nonorthogonal segment", "zero-length segment", "route point outside canvas", "crosses unrelated module", "route does not"))]
    if check["errors"] or fatal:
        report["status"] = "unresolved"
        report["validation_issues"] = check["errors"] + fatal
        report["unresolved"] = sorted({w.split(":")[0].removeprefix("edges.") for w in fatal}) or ["validation"]
        return None, report
    return out, report


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".routing-", suffix=".json", delete=False) as handle:
            staged = Path(handle.name)
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(staged, path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hardware", type=Path)
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ports", choices=("preserve", "spread"), default="preserve")
    parser.add_argument("--clearance", type=float, default=16.0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        paths = [args.hardware, args.layout, args.output] + ([args.report] if args.report else [])
        if len({p.resolve() for p in paths}) != len(paths):
            raise RoutingError("hardware, layout, output, and report paths must be distinct")
        for i, p in enumerate(paths):
            for q in paths[i+1:]:
                if p.exists() and q.exists() and p.samefile(q):
                    raise RoutingError("hardware, layout, output, and report files must be distinct")
        hardware = diagram.read_data(args.hardware)
        layout = diagram.read_data(args.layout)
        result, report = route_layout(hardware, layout, ports=args.ports, clearance=args.clearance)
        if result is None:
            if args.report:
                _write_json(args.report, report)
            print("routing unresolved: " + ", ".join(report["unresolved"]), file=sys.stderr)
            return 1
        _write_json(args.output, result)
        if args.report:
            _write_json(args.report, report)
        print(json.dumps({"output": str(args.output), "edges": len(report["edges"]), "total_bends": report["total_bends"], "crossings": report["crossings"], "overlap_pairs": report["overlap_pairs"], "needs_visual_review": report["needs_visual_review"], "labels_to_review": report["labels_to_review"]}, ensure_ascii=False))
        return 0
    except (RoutingError, diagram.DiagramError, OSError, ValueError) as exc:
        print(f"routing failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

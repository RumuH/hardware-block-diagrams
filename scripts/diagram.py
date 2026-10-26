#!/usr/bin/env python3
"""Validate hardware diagrams and create editable draw.io plus Desktop exports."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import xml.etree.ElementTree as ET
import zlib


ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
SIDES = {"left", "right", "top", "bottom"}
KINDS = {"data", "control", "bus", "feedback", "clock", "reset"}
MODULE_KEYS = {"id", "label", "parent", "kind", "count", "shared_with", "ports", "clock_domain", "description", "metadata"}
PORT_KEYS = {"id", "direction", "width", "protocol", "role", "metadata"}
CONNECTION_KEYS = {"id", "source", "target", "kind", "label", "width", "protocol", "bidirectional", "metadata"}
LAYOUT_NODE_KEYS = {"x", "y", "w", "h", "label_position", "label_align", "shape", "dashed", "text_direction", "label_side", "font_size", "stroke_width"}
LAYOUT_EDGE_KEYS = {"source_side", "target_side", "source_pos", "target_pos", "points", "label_at", "wide", "arrow", "dashed"}


class DiagramError(Exception):
    pass


def read_data(path):
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml
            except ImportError as exc:
                raise DiagramError("YAML input needs PyYAML; use JSON or install PyYAML") from exc
            try:
                return yaml.safe_load(raw)
            except yaml.YAMLError as exc:
                raise DiagramError(f"cannot parse YAML {path}: {exc}") from exc
        return json.loads(raw)
    except (OSError, ValueError) as exc:
        raise DiagramError(f"cannot read {path}: {exc}") from exc


def _mapping(value, where, issues):
    if not isinstance(value, dict):
        issues.append(f"{where}: expected object")
        return {}
    return value


def _keys(value, allowed, where, issues):
    for key in value.keys() - allowed:
        issues.append(f"{where}: unknown field {key!r}")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _choice(value, choices):
    return isinstance(value, str) and value in choices


def _id(value, where, issues):
    if not isinstance(value, str) or not ID.fullmatch(value):
        issues.append(f"{where}: expected safe identifier")
        return False
    return True


def _endpoint(raw, modules, where, issues):
    if not isinstance(raw, str) or raw.count(".") > 1:
        issues.append(f"{where}: invalid endpoint")
        return None
    parts = raw.split(".")
    if parts[0] not in modules:
        issues.append(f"{where}: unknown module {parts[0]!r}")
        return None
    if len(parts) == 2:
        ports = modules[parts[0]].get("ports", [])
        if not isinstance(ports, list):
            issues.append(f"{where}: module ports are invalid")
            return None
        found = next((p for p in ports if isinstance(p, dict) and p.get("id") == parts[1]), None)
        if found is None:
            issues.append(f"{where}: unknown port {raw!r}")
            return None
        return parts[0], found
    return parts[0], None


def validate_hardware(hardware):
    errors, warnings = [], []
    h = _mapping(hardware, "hardware", errors)
    _keys(h, {"version", "title", "view", "modules", "connections", "metadata"}, "hardware", errors)
    if h.get("version") != 1:
        errors.append("hardware.version: expected 1")
    if not isinstance(h.get("title"), str) or not h["title"].strip():
        errors.append("hardware.title: expected nonempty string")
    if not _choice(h.get("view"), {"composition", "microarchitecture"}):
        errors.append("hardware.view: expected composition or microarchitecture")
    modules_raw = h.get("modules")
    connections_raw = h.get("connections")
    if not isinstance(modules_raw, list):
        errors.append("hardware.modules: expected array")
        modules_raw = []
    if not isinstance(connections_raw, list):
        errors.append("hardware.connections: expected array")
        connections_raw = []
    modules = {}
    for i, raw in enumerate(modules_raw):
        where = f"modules[{i}]"
        m = _mapping(raw, where, errors)
        _keys(m, MODULE_KEYS, where, errors)
        ident = m.get("id")
        if _id(ident, where + ".id", errors):
            if ident in modules:
                errors.append(f"{where}.id: duplicate {ident!r}")
            else:
                modules[ident] = m
        if not isinstance(m.get("label"), str) or not m["label"].strip():
            errors.append(f"{where}.label: expected nonempty string")
        if "parent" in m:
            _id(m["parent"], where + ".parent", errors)
        if "kind" in m and not isinstance(m["kind"], str):
            errors.append(f"{where}.kind: expected string")
        if "count" in m and not _positive_int(m["count"]):
            errors.append(f"{where}.count: expected positive integer")
        if "shared_with" in m:
            if not isinstance(m["shared_with"], list):
                errors.append(f"{where}.shared_with: expected array")
            else:
                for j, ref in enumerate(m["shared_with"]):
                    _id(ref, f"{where}.shared_with[{j}]", errors)
        for field in ("clock_domain", "description"):
            if field in m and not isinstance(m[field], str):
                errors.append(f"{where}.{field}: expected string")
        if "metadata" in m and not isinstance(m["metadata"], dict):
            errors.append(f"{where}.metadata: expected object")
        ports = m.get("ports", [])
        if not isinstance(ports, list):
            errors.append(f"{where}.ports: expected array")
            continue
        seen_ports = set()
        for j, raw_port in enumerate(ports):
            pw = f"{where}.ports[{j}]"
            p = _mapping(raw_port, pw, errors)
            _keys(p, PORT_KEYS, pw, errors)
            pid = p.get("id")
            if _id(pid, pw + ".id", errors):
                if pid in seen_ports:
                    errors.append(f"{pw}.id: duplicate {pid!r}")
                seen_ports.add(pid)
            if not _choice(p.get("direction"), {"in", "out", "inout"}):
                errors.append(f"{pw}.direction: expected in, out, or inout")
            if "role" in p and not _choice(p["role"], {"initiator", "target"}):
                errors.append(f"{pw}.role: expected initiator or target")
            if "width" in p and not _positive_int(p["width"]):
                errors.append(f"{pw}.width: expected positive integer")
            if "protocol" in p and not isinstance(p["protocol"], str):
                errors.append(f"{pw}.protocol: expected string")
            if "metadata" in p and not isinstance(p["metadata"], dict):
                errors.append(f"{pw}.metadata: expected object")
    for ident, m in modules.items():
        parent = m.get("parent")
        if parent is not None and (not isinstance(parent,str) or parent not in modules):
            errors.append(f"module {ident}: unknown parent {parent!r}")
        for ref in m.get("shared_with", []) if isinstance(m.get("shared_with", []), list) else []:
            if not isinstance(ref,str) or ref not in modules:
                errors.append(f"module {ident}: unknown shared_with {ref!r}")
        chain = {ident}
        while isinstance(parent,str) and parent in modules:
            if parent in chain:
                errors.append(f"module {ident}: parent cycle")
                break
            chain.add(parent)
            parent = modules[parent].get("parent")
    connections = {}
    for i, raw in enumerate(connections_raw):
        where = f"connections[{i}]"
        c = _mapping(raw, where, errors)
        _keys(c, CONNECTION_KEYS, where, errors)
        cid = c.get("id")
        if _id(cid, where + ".id", errors):
            if cid in connections:
                errors.append(f"{where}.id: duplicate {cid!r}")
            else:
                connections[cid] = c
        if not _choice(c.get("kind"), KINDS):
            errors.append(f"{where}.kind: unknown connection kind")
        for field in ("label", "protocol"):
            if field in c and not isinstance(c[field], str):
                errors.append(f"{where}.{field}: expected string")
        if "metadata" in c and not isinstance(c["metadata"], dict):
            errors.append(f"{where}.metadata: expected object")
        if "width" in c and not _positive_int(c["width"]):
            errors.append(f"{where}.width: expected positive integer")
        if "bidirectional" in c and not isinstance(c["bidirectional"], bool):
            errors.append(f"{where}.bidirectional: expected boolean")
        src = _endpoint(c.get("source"), modules, where + ".source", errors)
        dst = _endpoint(c.get("target"), modules, where + ".target", errors)
        if src and dst:
            sp, tp = src[1], dst[1]
            bi = c.get("bidirectional") is True
            if sp and not _choice(sp.get("direction"), {"inout"} if bi else {"out", "inout"}):
                errors.append(f"{where}: source port direction is incompatible")
            if tp and not _choice(tp.get("direction"), {"inout"} if bi else {"in", "inout"}):
                errors.append(f"{where}: target port direction is incompatible")
            known = [(f"source {c.get('source')}", sp.get("width")) for sp in [sp] if sp and _positive_int(sp.get("width"))]
            if tp and _positive_int(tp.get("width")):
                known.append((f"target {c.get('target')}", tp["width"]))
            if _positive_int(c.get("width")):
                known.append(("connection", c["width"]))
            if len({v for _, v in known}) > 1:
                errors.append(f"{where}: known width mismatch: {known}")
            protocols = [v for v in (sp.get("protocol") if sp else None, tp.get("protocol") if tp else None, c.get("protocol")) if isinstance(v,str) and v]
            if len(set(protocols)) > 1:
                errors.append(f"{where}: protocol mismatch: {protocols}")
    return {"errors": errors, "warnings": warnings, "modules": modules, "connections": connections}


def _rect(n):
    return n["x"], n["y"], n["x"] + n["w"], n["y"] + n["h"]


def _intersection(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _ancestor(child, parent, modules):
    seen = set()
    while isinstance(child,str) and child in modules and child not in seen:
        seen.add(child)
        child = modules[child].get("parent")
        if child == parent:
            return True
    return False


def anchor(rect, side, position):
    x1, y1, x2, y2 = rect
    if side == "left":
        return x1, y1 + (y2-y1)*position
    if side == "right":
        return x2, y1 + (y2-y1)*position
    if side == "top":
        return x1 + (x2-x1)*position, y1
    return x1 + (x2-x1)*position, y2


def _segment_hits_rect(a, b, r):
    # Liang-Barsky against a small inset: touching a boundary is not traversal.
    r = (r[0]+1, r[1]+1, r[2]-1, r[3]-1)
    if r[0] >= r[2] or r[1] >= r[3]:
        return False
    dx, dy = b[0]-a[0], b[1]-a[1]
    low, high = 0.0, 1.0
    for p, q in ((-dx, a[0]-r[0]), (dx, r[2]-a[0]), (-dy, a[1]-r[1]), (dy, r[3]-a[1])):
        if p == 0:
            if q < 0:
                return False
        else:
            t = q/p
            if p < 0:
                low = max(low, t)
            else:
                high = min(high, t)
    return low < high


def validate_layout(layout, hardware, semantic=None):
    semantic = semantic or validate_hardware(hardware)
    errors, warnings = [], []
    l = _mapping(layout, "layout", errors)
    _keys(l, {"version", "canvas", "nodes", "edges", "legend"}, "layout", errors)
    if l.get("version") != 1:
        errors.append("layout.version: expected 1")
    canvas = _mapping(l.get("canvas"), "layout.canvas", errors)
    _keys(canvas, {"width", "height"}, "layout.canvas", errors)
    for dim in ("width", "height"):
        if not _number(canvas.get(dim)) or canvas[dim] <= 0:
            errors.append(f"layout.canvas.{dim}: expected positive number")
    nodes = _mapping(l.get("nodes"), "layout.nodes", errors)
    edges = _mapping(l.get("edges"), "layout.edges", errors)
    modules, connections = semantic["modules"], semantic["connections"]
    for missing in modules.keys() - nodes.keys():
        errors.append(f"layout.nodes: missing {missing!r}")
    for extra in nodes.keys() - modules.keys():
        errors.append(f"layout.nodes: unknown module {extra!r}")
    for missing in connections.keys() - edges.keys():
        errors.append(f"layout.edges: missing {missing!r}")
    for extra in edges.keys() - connections.keys():
        errors.append(f"layout.edges: unknown connection {extra!r}")
    valid_nodes = {}
    for mid, raw in nodes.items():
        n = _mapping(raw, f"nodes.{mid}", errors)
        _keys(n, LAYOUT_NODE_KEYS, f"nodes.{mid}", errors)
        if all(_number(n.get(k)) for k in ("x", "y", "w", "h")) and n["w"] > 0 and n["h"] > 0:
            valid_nodes[mid] = n
            if "width" in canvas and "height" in canvas and _number(canvas["width"]) and _number(canvas["height"]):
                r = _rect(n)
                if r[0] < 0 or r[1] < 0 or r[2] > canvas["width"] or r[3] > canvas["height"]:
                    errors.append(f"nodes.{mid}: outside canvas")
        else:
            errors.append(f"nodes.{mid}: x,y,w,h must be finite numbers; w,h positive")
        if not _choice(n.get("label_position", "center"), {"top", "center", "bottom"}):
            errors.append(f"nodes.{mid}.label_position: invalid value")
        if not _choice(n.get("label_align", "center"), {"left", "center", "right"}):
            errors.append(f"nodes.{mid}.label_align: invalid value")
        if not _choice(n.get("shape", "block"), {"block", "domain", "bus", "pin"}):
            errors.append(f"nodes.{mid}.shape: expected block, domain, bus, or pin")
        if "dashed" in n and not isinstance(n["dashed"], bool):
            errors.append(f"nodes.{mid}.dashed: expected boolean")
        if "text_direction" in n and not _choice(n["text_direction"], {"horizontal", "vertical"}):
            errors.append(f"nodes.{mid}.text_direction: invalid value")
        if n.get("shape") == "domain" and n.get("text_direction") == "vertical":
            errors.append(f"nodes.{mid}: domain title must remain horizontal")
        if n.get("shape") == "domain" and n.get("label_position") == "center":
            errors.append(f"nodes.{mid}: domain border title must use top or bottom")
        if "label_side" in n and (n.get("shape") != "pin" or not _choice(n["label_side"], SIDES)):
            errors.append(f"nodes.{mid}.label_side: expected pin label side")
        if n.get("shape") == "pin" and all(_number(n.get(k)) for k in ("w", "h")) and abs(n["w"]-n["h"]) > 1e-6:
            errors.append(f"nodes.{mid}: pin shape requires square geometry")
        for field in ("font_size", "stroke_width"):
            if field in n and (not _number(n[field]) or n[field] <= 0):
                errors.append(f"nodes.{mid}.{field}: expected positive number")
    for mid, m in modules.items():
        p = m.get("parent")
        if isinstance(p,str) and p in valid_nodes and mid in valid_nodes:
            r, outer = _rect(valid_nodes[mid]), _rect(valid_nodes[p])
            if r[0] < outer[0] or r[1] < outer[1] or r[2] > outer[2] or r[3] > outer[3]:
                errors.append(f"nodes.{mid}: outside parent {p}")
        if mid in valid_nodes:
            label = str(m.get("label", "")) + (f" ×{m['count']}" if _positive_int(m.get("count")) and m["count"] > 1 else "")
            n = valid_nodes[mid]
            vertical = n.get("text_direction", "vertical" if n.get("shape") == "bus" else "horizontal") == "vertical"
            label_width, label_height = (n["h"], n["w"]) if vertical else (n["w"], n["h"])
            if n.get("shape") != "pin" and (max(map(len, label.splitlines() or [""]))*7 > label_width - 12 or label_height < 24):
                warnings.append(f"nodes.{mid}: label may not fit")
    names = list(valid_nodes.keys() & modules.keys())
    for i, a in enumerate(names):
        for b in names[i+1:]:
            if not _ancestor(a,b,modules) and not _ancestor(b,a,modules) and _intersection(_rect(valid_nodes[a]), _rect(valid_nodes[b])):
                warnings.append(f"nodes.{a},{b}: overlap")
    valid_edges = {}
    for cid, raw in edges.items():
        e = _mapping(raw, f"edges.{cid}", errors)
        _keys(e, LAYOUT_EDGE_KEYS, f"edges.{cid}", errors)
        for field in ("source_side", "target_side"):
            if not _choice(e.get(field), SIDES):
                errors.append(f"edges.{cid}.{field}: invalid side")
        for field in ("source_pos", "target_pos"):
            if field in e and (not _number(e[field]) or not 0 <= e[field] <= 1):
                errors.append(f"edges.{cid}.{field}: expected number 0..1")
        for field in ("points",):
            if field in e and (not isinstance(e[field], list) or any(not isinstance(p, list) or len(p) != 2 or not all(_number(v) for v in p) for p in e[field])):
                errors.append(f"edges.{cid}.points: expected array of [x,y]")
        if "label_at" in e and (not isinstance(e["label_at"], list) or len(e["label_at"]) != 2 or not all(_number(v) for v in e["label_at"])):
            errors.append(f"edges.{cid}.label_at: expected [x,y]")
        if "wide" in e and not isinstance(e["wide"], bool):
            errors.append(f"edges.{cid}.wide: expected boolean")
        if "arrow" in e and not _choice(e["arrow"], {"normal", "none"}):
            errors.append(f"edges.{cid}.arrow: expected normal or none")
        if "dashed" in e and not isinstance(e["dashed"], bool):
            errors.append(f"edges.{cid}.dashed: expected boolean")
        if e.get("arrow") == "none" and e.get("wide") is True:
            errors.append(f"edges.{cid}: arrow=none is incompatible with wide arrow")
        if cid in connections and all(_choice(e.get(f), SIDES) for f in ("source_side", "target_side")):
            c = connections[cid]
            src, dst = str(c.get("source", "")).split(".")[0], str(c.get("target", "")).split(".")[0]
            if src in valid_nodes and dst in valid_nodes:
                a = anchor(_rect(valid_nodes[src]), e["source_side"], e.get("source_pos", .5) if _number(e.get("source_pos", .5)) else .5)
                b = anchor(_rect(valid_nodes[dst]), e["target_side"], e.get("target_pos", .5) if _number(e.get("target_pos", .5)) else .5)
                pts = e.get("points", []) if isinstance(e.get("points", []), list) else []
                if all(isinstance(p,list) and len(p)==2 and all(_number(v) for v in p) for p in pts):
                    path = [a] + pts + [b]
                    if _number(canvas.get("width")) and _number(canvas.get("height")):
                        for point in path[1:-1]:
                            if not (0 <= point[0] <= canvas["width"] and 0 <= point[1] <= canvas["height"]):
                                warnings.append(f"edges.{cid}: route point outside canvas")
                                break
                    for j,(start,end) in enumerate(zip(path,path[1:])):
                        dx,dy=end[0]-start[0],end[1]-start[1]
                        if abs(dx)<1e-6 and abs(dy)<1e-6:
                            warnings.append(f"edges.{cid}: zero-length segment {j+1}")
                        elif abs(dx)>=1e-6 and abs(dy)>=1e-6:
                            warnings.append(f"edges.{cid}: nonorthogonal segment {j+1}")
                    if len(path)>1:
                        sx,sy=path[1][0]-a[0],path[1][1]-a[1]
                        tx,ty=b[0]-path[-2][0],b[1]-path[-2][1]
                        outward={"left":(-1,0),"right":(1,0),"top":(0,-1),"bottom":(0,1)}
                        ns=outward[e["source_side"]]
                        nt=outward[e["target_side"]]
                        if (sx,sy)!=(0,0) and (abs(sx*ns[1]-sy*ns[0])>1e-6 or sx*ns[0]+sy*ns[1]<=0):
                            warnings.append(f"edges.{cid}: source route does not leave perpendicular to {e['source_side']} side")
                        if (tx,ty)!=(0,0) and (abs(tx*nt[1]-ty*nt[0])>1e-6 or tx*nt[0]+ty*nt[1]>=0):
                            warnings.append(f"edges.{cid}: target route does not enter perpendicular to {e['target_side']} side")
                    for mid in names:
                        if mid in {src, dst} or any(_ancestor(x, mid, modules) for x in (src, dst)):
                            continue
                        if any(_segment_hits_rect(x,y,_rect(valid_nodes[mid])) for x,y in zip(path,path[1:])):
                            warnings.append(f"edges.{cid}: crosses unrelated module {mid}")
                valid_edges[cid] = e
    if "legend" in l:
        legend = _mapping(l["legend"], "layout.legend", errors)
        _keys(legend, {"x", "y", "w", "row_height", "entries"}, "layout.legend", errors)
        for field in ("x", "y", "w"):
            if not _number(legend.get(field)) or (field == "w" and legend[field] <= 0):
                errors.append(f"layout.legend.{field}: expected finite geometry")
        row = legend.get("row_height", 28)
        if not _number(row) or row < 24:
            errors.append("layout.legend.row_height: expected number >=24")
        entries = legend.get("entries")
        if not isinstance(entries, list) or not entries:
            errors.append("layout.legend.entries: expected nonempty array")
            entries = []
        for i, raw in enumerate(entries):
            entry = _mapping(raw, f"legend.entries[{i}]", errors)
            _keys(entry, {"connection", "label"}, f"legend.entries[{i}]", errors)
            if not isinstance(entry.get("connection"), str) or entry["connection"] not in connections:
                errors.append(f"legend.entries[{i}].connection: unknown connection")
            if not isinstance(entry.get("label"), str) or not entry["label"].strip():
                errors.append(f"legend.entries[{i}].label: expected nonempty string")
        if all(_number(legend.get(k)) for k in ("x", "y", "w")) and _number(row) and all(_number(canvas.get(k)) for k in ("width", "height")):
            box = (legend["x"], legend["y"], legend["x"]+legend["w"], legend["y"]+row*len(entries))
            if box[0] < 0 or box[1] < 0 or box[2] > canvas["width"] or box[3] > canvas["height"]:
                errors.append("layout.legend: outside canvas")
            for mid, node in valid_nodes.items():
                if any(m.get("parent") == mid for m in modules.values()):
                    continue
                if _intersection(box, _rect(node)):
                    warnings.append(f"layout.legend: overlaps module {mid}")
    return {"errors": errors, "warnings": warnings, "nodes": valid_nodes, "edges": valid_edges}


def validate_style(style):
    errors = []
    s = _mapping(style, "style", errors)
    _keys(s, {"version", "name", "view", "tokens", "rules", "sample_ids", "revision", "metadata"}, "style", errors)
    if s.get("version") != 1:
        errors.append("style.version: expected 1")
    if not isinstance(s.get("name"), str) or not s["name"].strip():
        errors.append("style.name: expected nonempty string")
    if not _choice(s.get("view"), {"composition", "microarchitecture"}):
        errors.append("style.view: invalid value")
    t = _mapping(s.get("tokens", {}), "style.tokens", errors)
    _keys(t, {"background", "font_family", "font_size", "edge_font_size", "text_color", "border_color", "colors", "edge_colors", "edge_width", "wide_width", "corner_radius", "arrow_head_length", "arrow_head_width_ratio"}, "style.tokens", errors)
    for key in ("font_size", "edge_font_size", "edge_width", "wide_width", "arrow_head_length", "arrow_head_width_ratio"):
        if key in t and (not _number(t[key]) or t[key] <= 0):
            errors.append(f"style.tokens.{key}: expected positive number")
    if _number(t.get("arrow_head_width_ratio")) and t["arrow_head_width_ratio"] < 1:
        errors.append("style.tokens.arrow_head_width_ratio: expected number >=1")
    if "corner_radius" in t and (not _number(t["corner_radius"]) or t["corner_radius"] < 0):
        errors.append("style.tokens.corner_radius: expected nonnegative number")
    for key in ("colors", "edge_colors"):
        if key in t and (not isinstance(t[key], dict) or any(not isinstance(k,str) or not isinstance(v,str) or not re.fullmatch(r"#[0-9a-fA-F]{6}",v) for k,v in t[key].items())):
            errors.append(f"style.tokens.{key}: expected map of #RRGGBB colors")
    for key in ("background", "text_color", "border_color"):
        if key in t and (not isinstance(t[key], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}",t[key])):
            errors.append(f"style.tokens.{key}: expected #RRGGBB color")
    if "font_family" in t and (not isinstance(t["font_family"],str) or not t["font_family"].strip() or any(c in t["font_family"] for c in ";\r\n") or any(ord(c)<32 for c in t["font_family"])):
        errors.append("style.tokens.font_family: expected nonempty font name without style separators")
    for key in ("rules", "sample_ids"):
        if key in s and (not isinstance(s[key],list) or any(not isinstance(v,str) for v in s[key])):
            errors.append(f"style.{key}: expected string array")
    return errors


def _rgb(value, fallback):
    return value if isinstance(value,str) and re.fullmatch(r"#[0-9a-fA-F]{6}",value) else fallback


def _b64(value):
    return base64.b64encode(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).decode()


def edge_label(connection, modules):
    label = connection.get("label", "").strip()
    source = connection["source"].split(".")
    target = connection["target"].split(".")
    ports = []
    for endpoint in (source,target):
        if len(endpoint)==2:
            ports.extend(p for p in modules[endpoint[0]].get("ports",[]) if p["id"]==endpoint[1])
    protocol = connection.get("protocol") or next((p["protocol"] for p in ports if p.get("protocol")), None)
    width = connection.get("width") or next((p["width"] for p in ports if p.get("width")), None)
    if protocol and protocol.lower() not in label.lower():
        label = " · ".join(part for part in (label,protocol) if part)
    if width and not re.search(rf"(?<!\d){width}\s*(?:-|\s)?\s*(?:bit|b)\b",label,re.IGNORECASE):
        label = " · ".join(part for part in (label,f"{width}-bit") if part)
    return label


def edge_route(connection, edge, nodes):
    source, target = (nodes[connection[k].split(".")[0]] for k in ("source", "target"))
    return [anchor(_rect(source), edge["source_side"], edge.get("source_pos", .5)),
            *[tuple(p) for p in edge.get("points", [])],
            anchor(_rect(target), edge["target_side"], edge.get("target_pos", .5))]


def _compact_route(route):
    """Ignore redundant bends for end clearance, without changing the stored layout."""
    compact = []
    for point in route:
        if compact and math.dist(compact[-1], point) < 1e-7:
            continue
        compact.append(point)
        while len(compact) >= 3:
            a, b, c = compact[-3:]
            u, v = (b[0]-a[0], b[1]-a[1]), (c[0]-b[0], c[1]-b[1])
            if abs(u[0]*v[1]-u[1]*v[0]) < 1e-7 and u[0]*v[0]+u[1]*v[1] > 0:
                compact.pop(-2)
            else:
                break
    return compact


def arrow_dimensions(connection, edge, tokens, nodes=None, route=None):
    """Physical model units, before draw.io flexArrow's 3x marker-size conversion."""
    width = tokens.get("wide_width", 12)
    preferred = tokens.get("arrow_head_length", min(tokens.get("font_size", 14)*.85, max(8, width*2)))
    if nodes is not None and "arrow_head_length" not in tokens:
        near = [min(nodes[connection[k].split(".")[0]][d] for d in ("w", "h")) for k in ("source", "target")]
        preferred = min(preferred, max(8, min(near)*.28))
    path = _compact_route(route if route is not None else edge_route(connection, edge, nodes) if nodes is not None else [(0, 0), (56, 0)])
    first = math.dist(path[0], path[1]) if len(path) > 1 else 0
    last = math.dist(path[-2], path[-1]) if len(path) > 1 else 0
    return {"shaft_width": width, "head_width": width*tokens.get("arrow_head_width_ratio", 1.8),
            "start_length": min(preferred, first*.30) if connection.get("bidirectional") else 0,
            "end_length": min(preferred, last*.30), "first_segment": first, "last_segment": last}


def domain_title_rect(node, label, font_size):
    """Estimated native title rectangle in absolute model coordinates."""
    inset = min(8, node["w"]/4)
    width = min(node["w"]-2*inset, len(label)*font_size*.65+12)
    align = node.get("label_align", "left")
    x = node["w"]-inset-width if align == "right" else (node["w"]-width)/2 if align == "center" else inset
    y = node["y"]+(node["h"] if node.get("label_position") == "bottom" else 0)-font_size*.65
    return node["x"]+x, y, width, font_size*1.3


def _line_style(connection, edge, tokens, nodes=None):
    """One style source for bound connections and their legend samples."""
    color = _rgb(tokens.get("edge_colors", {}).get(connection["kind"]), _rgb(tokens.get("border_color"), "#40566e"))
    wide = edge.get("wide", False)
    width = tokens.get("wide_width", 12) if wide else tokens.get("edge_width", 2)
    arrow = edge.get("arrow", "normal") != "none"
    parts = ["html=0", "rounded=0", "edgeStyle=none", f"strokeColor={color}",
             f"strokeWidth={1 if wide else width}", f"endArrow={'block' if arrow else 'none'}", "endFill=1"]
    if connection.get("bidirectional") and arrow:
        parts.extend(["startArrow=block", "startFill=1"])
    if edge.get("dashed"):
        parts.extend(["dashed=1", "dashPattern=4 3"])
    if wide:
        dimensions = arrow_dimensions(connection, edge, tokens, nodes)
        # flexArrow adds endWidth to the shaft; endSize is multiplied by 3 then
        # the 1-unit outline is added. Public tokens remain physical model units.
        extra_width = dimensions["head_width"]-width
        parts.extend(["shape=flexArrow", f"fillColor={color}", f"width={width}", f"endWidth={extra_width:g}",
                      f"endSize={max(0, (dimensions['end_length']-1)/3):.8g}"])
        if connection.get("bidirectional"):
            parts.extend([f"startWidth={extra_width:g}", f"startSize={max(0, (dimensions['start_length']-1)/3):.8g}"])
    return parts


def drawio_xml(hardware, layout, style):
    modules = {m["id"]:m for m in hardware["modules"]}
    t = style.get("tokens", {})
    bg = _rgb(t.get("background"), "#ffffff")
    mxfile = ET.Element("mxfile", {"host":"app.diagrams.net", "type":"device", "version":"24.7.17"})
    diagram = ET.SubElement(mxfile,"diagram",{"id":"hardware", "name":hardware["title"], "hardwareJson":_b64(hardware), "layoutJson":_b64(layout), "styleJson":_b64(style)})
    model = ET.SubElement(diagram,"mxGraphModel",{"dx":"1000","dy":"700","grid":"1","gridSize":"10","page":"1","pageScale":"1","pageWidth":str(layout["canvas"]["width"]),"pageHeight":str(layout["canvas"]["height"]),"background":bg})
    root = ET.SubElement(model,"root")
    ET.SubElement(root,"mxCell",{"id":"0"})
    ET.SubElement(root,"mxCell",{"id":"1","parent":"0"})
    def add_node(mid):
        m,n = modules[mid], layout["nodes"][mid]
        parent = m.get("parent")
        if parent and f"node:{parent}" not in added:
            add_node(parent)
        pnode = layout["nodes"].get(parent,{"x":0,"y":0})
        x,y = n["x"]-pnode["x"], n["y"]-pnode["y"]
        fill = _rgb(t.get("colors",{}).get(m.get("kind","")), "#e8eef5")
        stroke = _rgb(t.get("border_color"), "#40566e")
        font = _rgb(t.get("text_color"), "#172536")
        label = m["label"] + (f" ×{m['count']}" if m.get("count",1)>1 else "")
        shape = n.get("shape", "block")
        fontsize = n.get("font_size", t.get("font_size", 14))
        radius=t.get("corner_radius",0)
        style_items = [f"rounded={1 if radius else 0}","whiteSpace=wrap","html=0","container=1","collapsible=0",f"fillColor={fill}",f"strokeColor={stroke}",f"fontColor={font}",f"fontSize={fontsize}",f"fontFamily={t.get('font_family','Helvetica')}",f"align={n.get('label_align','center')}","spacingLeft=12","spacingRight=12"]
        if "stroke_width" in n:
            style_items.append(f"strokeWidth={n['stroke_width']}")
        if n.get("dashed", shape == "domain"):
            style_items.extend(["dashed=1", "dashPattern=4 3"])
        if shape == "domain":
            style_items.extend(["rounded=0", "fillColor=none"])
        vertical = n.get("text_direction", "vertical" if shape == "bus" else "horizontal") == "vertical"
        if vertical:
            style_items.extend(["horizontal=0", "whiteSpace=nowrap", "spacing=2", "spacingLeft=2", "spacingRight=2"])
        if shape == "pin":
            side = n.get("label_side", "right")
            style_items.extend(["rounded=0", "fillColor=#ffffff", "whiteSpace=nowrap", "spacing=5", "spacingLeft=5", "spacingRight=5",
                                f"labelPosition={side if side in {'left','right'} else 'center'}",
                                f"verticalLabelPosition={side if side in {'top','bottom'} else 'middle'}",
                                f"align={'right' if side=='left' else 'left' if side=='right' else 'center'}"])
        if radius:
            style_items.extend(["absoluteArcSize=1",f"arcSize={radius*2}"])
        pos = n.get("label_position", "top" if any(child.get("parent") == mid for child in modules.values()) else "center")
        style_items.append(f"verticalAlign={('middle' if pos=='center' else pos)}")
        cell = ET.SubElement(root,"mxCell",{"id":f"node:{mid}","value":"" if shape == "domain" else label,"style":";".join(style_items)+";","vertex":"1","parent":f"node:{parent}" if parent else "1","hardwareId":mid,"hardwareKind":str(m.get("kind",""))})
        ET.SubElement(cell,"mxGeometry",{"x":str(x),"y":str(y),"width":str(n["w"]),"height":str(n["h"]),"as":"geometry"})
        if shape == "domain":
            # A native child title moves with its domain and masks only its local border.
            title = ET.SubElement(root, "mxCell", {"id": f"title:{mid}", "value": label, "vertex": "1", "connectable": "0", "parent": f"node:{mid}", "decorationOf": mid,
                "style": f"text;html=0;whiteSpace=nowrap;fillColor={bg};strokeColor=none;align=left;verticalAlign=middle;spacingLeft=4;fontColor={font};fontFamily={t.get('font_family','Helvetica')};fontSize={fontsize};"})
            tx, ty, tw, th = domain_title_rect(n, label, fontsize)
            ET.SubElement(title, "mxGeometry", {"x": str(tx-n["x"]), "y": str(ty-n["y"]), "width": str(tw), "height": str(th), "as": "geometry"})
        if shape == "pin":
            for i, (a, b) in enumerate((((0, 0), (n["w"], n["h"])), ((0, n["h"]), (n["w"], 0)))):
                cross = ET.SubElement(root, "mxCell", {"id": f"pin-cross:{mid}:{i}", "edge": "1", "parent": f"node:{mid}", "connectable": "0", "decorationOf": mid,
                    "style": f"edgeStyle=none;endArrow=none;startArrow=none;strokeColor={stroke};strokeWidth=1;"})
                geo = ET.SubElement(cross, "mxGeometry", {"relative": "1", "as": "geometry"})
                for name, point in (("sourcePoint", a), ("targetPoint", b)):
                    ET.SubElement(geo, "mxPoint", {"x": str(point[0]), "y": str(point[1]), "as": name})
        added.add(f"node:{mid}")
    added = set()
    for mid in modules:
        add_node(mid)
    for c in hardware["connections"]:
        cid = c["id"]
        e = layout["edges"][cid]
        src,dst = c["source"].split(".")[0],c["target"].split(".")[0]
        color = _rgb(t.get("edge_colors",{}).get(c["kind"]), _rgb(t.get("border_color"), "#40566e"))
        wide = e.get("wide",False)
        # flexArrow is an editable native arrow edge with source and target bounds.
        sx, sy = anchor((0, 0, 1, 1), e["source_side"], e.get("source_pos", .5))
        tx, ty = anchor((0, 0, 1, 1), e["target_side"], e.get("target_pos", .5))
        parts = _line_style(c, e, t, layout["nodes"])
        parts.extend([f"exitX={sx}", f"exitY={sy}", f"entryX={tx}", f"entryY={ty}", "exitPerimeter=0", "entryPerimeter=0"])
        parts.extend([f"fontSize={t.get('edge_font_size', 11)}", f"fontFamily={t.get('font_family', 'Helvetica')}"])
        cell = ET.SubElement(root,"mxCell",{"id":f"edge:{cid}","value":edge_label(c,modules),"style":";".join(parts)+";","edge":"1","parent":"1","source":f"node:{src}","target":f"node:{dst}","hardwareId":cid,"hardwareKind":c["kind"]})
        geo = ET.SubElement(cell,"mxGeometry",{"relative":"1","as":"geometry"})
        if e.get("points"):
            arr=ET.SubElement(geo,"Array",{"as":"points"})
            for x,y in e["points"]:
                ET.SubElement(arr,"mxPoint",{"x":str(x),"y":str(y)})
        if e.get("label_at"):
            sn,dn=layout["nodes"][src],layout["nodes"][dst]
            start=anchor(_rect(sn),e["source_side"],e.get("source_pos",.5))
            end=anchor(_rect(dn),e["target_side"],e.get("target_pos",.5))
            route=[start]+[tuple(p) for p in e.get("points",[])]+[end]
            lengths=[math.dist(a,b) for a,b in zip(route,route[1:])]
            halfway=sum(lengths)/2
            midpoint=route[0]
            for a,b,length in zip(route,route[1:],lengths):
                if halfway <= length:
                    f=halfway/length if length else 0
                    midpoint=(a[0]+f*(b[0]-a[0]),a[1]+f*(b[1]-a[1]))
                    break
                halfway-=length
            ET.SubElement(geo,"mxPoint",{"x":str(e["label_at"][0]-midpoint[0]),"y":str(e["label_at"][1]-midpoint[1]),"as":"offset"})
        # Port role is a declared hardware fact; direction or visual bus shape never implies it.
        if c["kind"] == "bus":
            for endpoint, side, position in ((c["source"], e["source_side"], -1), (c["target"], e["target_side"], 1)):
                parts_ep = endpoint.split(".")
                port = next((p for p in modules[parts_ep[0]].get("ports", []) if len(parts_ep)==2 and p["id"]==parts_ep[1]), {})
                if port.get("role") != "initiator":
                    continue
                marker = ET.SubElement(root, "mxCell", {"id": f"master:{cid}:{position}", "value": "M", "vertex": "1", "connectable": "0", "parent": f"edge:{cid}", "decorationOf": cid,
                    "style": f"rounded=0;html=0;fillColor=#ffffff;strokeColor={color};fontColor={color};fontFamily={t.get('font_family','Helvetica')};fontSize=11;align=center;verticalAlign=middle;spacing=0;"})
                mg = ET.SubElement(marker, "mxGeometry", {"x": str(position), "y": "0", "width": "14", "height": "14", "relative": "1", "as": "geometry"})
                dx, dy = {"left": (-7, 0), "right": (7, 0), "top": (0, -7), "bottom": (0, 7)}[side]
                ET.SubElement(mg, "mxPoint", {"x": str(dx-7), "y": str(dy-7), "as": "offset"})
    if "legend" in layout:
        legend = layout["legend"]
        row = legend.get("row_height", 28)
        box = ET.SubElement(root, "mxCell", {"id": "legend", "value": "", "vertex": "1", "connectable": "0", "parent": "1", "decorationOf": "legend", "style": f"fillColor={bg};strokeColor=none;"})
        ET.SubElement(box, "mxGeometry", {"x": str(legend["x"]), "y": str(legend["y"]), "width": str(legend["w"]), "height": str(row*len(legend["entries"])), "as": "geometry"})
        connections = {c["id"]: c for c in hardware["connections"]}
        for i, entry in enumerate(legend["entries"]):
            c = connections[entry["connection"]]
            line = ET.SubElement(root, "mxCell", {"id": f"legend-line:{i}", "edge": "1", "parent": "legend", "connectable": "0", "decorationOf": "legend", "style": ";".join(_line_style(c, layout["edges"][c["id"]], t, layout["nodes"]))+";"})
            geo = ET.SubElement(line, "mxGeometry", {"relative": "1", "as": "geometry"})
            for name, x in (("sourcePoint", 8), ("targetPoint", 64)):
                ET.SubElement(geo, "mxPoint", {"x": str(x), "y": str((i+.5)*row), "as": name})
            text = ET.SubElement(root, "mxCell", {"id": f"legend-label:{i}", "value": entry["label"], "vertex": "1", "parent": "legend", "connectable": "0", "decorationOf": "legend",
                "style": f"text;html=0;fillColor=none;strokeColor=none;align=left;verticalAlign=middle;fontSize={t.get('edge_font_size',11)};fontFamily={t.get('font_family','Helvetica')};fontColor={t.get('text_color','#172536')};"})
            ET.SubElement(text, "mxGeometry", {"x": "78", "y": str(i*row), "width": str(max(1, legend["w"]-78)), "height": str(row), "as": "geometry"})
    return ET.tostring(mxfile,encoding="utf-8",xml_declaration=True)


def _hash(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def _write(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,sort_keys=True)+"\n",encoding="utf-8")


def _drawio_binary(provided):
    if provided:
        p = Path(provided)
        return str(p) if p.exists() else None
    for name in ("drawio","draw.io","/Applications/draw.io.app/Contents/MacOS/draw.io",str(Path.home()/"Applications/draw.io.app/Contents/MacOS/draw.io")):
        found = shutil.which(name) if "/" not in name else name if Path(name).exists() else None
        if found:
            return found
    return None


def render(hardware,layout,style,prefix,drawio=None,style_revision=None,allow_nonorthogonal=False):
    sem=validate_hardware(hardware)
    lay=validate_layout(layout,hardware,sem)
    sty=validate_style(style)
    errors=sem["errors"]+lay["errors"]+sty
    if not allow_nonorthogonal:
        diagonal = [w for w in lay["warnings"] if ": nonorthogonal segment " in w]
        if diagonal:
            errors.extend(diagonal)
            errors.append("Orthogonal routing required: align anchors or use scripts/routing.py; --allow-nonorthogonal is for explicitly requested diagonal designs only.")
    if isinstance(style,dict) and isinstance(hardware,dict) and style.get("view") != hardware.get("view"):
        errors.append("style.view: differs from hardware.view")
    if errors:
        raise DiagramError("validation failed:\n"+"\n".join(errors))
    prefix=Path(prefix)
    prefix.parent.mkdir(parents=True,exist_ok=True)
    files={ext:Path(str(prefix)+"."+ext) for ext in ("drawio","svg","png","hardware.json","layout.json","manifest.json")}
    with tempfile.TemporaryDirectory(prefix="diagram-",dir=prefix.parent) as tmp:
        stage=Path(tmp)
        native=stage/"native.drawio"
        native.write_bytes(drawio_xml(hardware,layout,style))
        binary=_drawio_binary(drawio)
        export_errors=[]
        exported=set()
        if binary:
            for fmt in ("svg","png"):
                out=stage/f"export.{fmt}"
                try:
                    proc=subprocess.run([binary,"-x","-f",fmt,"--size","page","-o",str(out),str(native)],capture_output=True,text=True,timeout=90,check=False)
                    valid=proc.returncode==0 and out.is_file() and out.stat().st_size>0
                    if valid and fmt=="png":
                        valid=out.open("rb").read(8)==b"\x89PNG\r\n\x1a\n"
                    if valid and fmt=="svg":
                        try:
                            valid=ET.parse(out).getroot().tag.endswith("svg")
                        except ET.ParseError:
                            valid=False
                    if valid:
                        exported.add(fmt)
                    else:
                        export_errors.append(f"{fmt} export failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()[:500]}")
                except (OSError,subprocess.TimeoutExpired) as exc:
                    export_errors.append(f"{fmt} export failed: {exc}")
        else:
            export_errors.append("draw.io Desktop executable unavailable; SVG and PNG exports were requested")
        manifest={"version":1,"title":hardware["title"],"view":hardware["view"],"files":{k:str(v) for k,v in files.items()},"hardware_sha256":_hash(hardware),"style_sha256":_hash(style),"style":{"name":style["name"],"revision":style_revision if style_revision is not None else style.get("revision")},"issues":{"errors":export_errors,"warnings":sem["warnings"]+lay["warnings"]},"exports_complete":not export_errors}
        manifest["renderer_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        _write(stage/"hardware.json",hardware)
        _write(stage/"layout.json",layout)
        _write(stage/"manifest.json",manifest)
        for key, source in (("drawio",native),("hardware.json",stage/"hardware.json"),("layout.json",stage/"layout.json"),("manifest.json",stage/"manifest.json")):
            os.replace(source,files[key])
        for fmt in ("svg","png"):
            out=stage/f"export.{fmt}"
            if fmt in exported:
                os.replace(out,files[fmt])
            elif files[fmt].exists():
                files[fmt].unlink()
    return manifest


def import_drawio(path):
    raw=Path(path).read_bytes()
    if len(raw)>20_000_000 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise DiagramError("draw.io XML too large or contains prohibited declarations")
    try:
        doc=ET.fromstring(raw)
    except ET.ParseError as exc:
        raise DiagramError(f"invalid draw.io XML: {exc}") from exc
    if doc.tag != "mxfile":
        raise DiagramError("expected mxfile root")
    result={"version":1,"source":str(path),"diagrams":[]}
    for d in doc.findall("diagram"):
        model=d.find("mxGraphModel")
        if model is None and d.text and d.text.strip():
            try:
                packed=base64.b64decode(d.text.strip(),validate=True)
                inflater=zlib.decompressobj(-15)
                expanded=inflater.decompress(packed,20_000_001)
                if len(expanded)>20_000_000 or inflater.unconsumed_tail:
                    raise DiagramError("compressed diagram too large")
                xml=urllib.parse.unquote((expanded+inflater.flush()).decode())
                if len(xml)>20_000_000 or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
                    raise DiagramError("compressed diagram contains prohibited declarations")
                model=ET.fromstring(xml)
            except (ValueError,zlib.error,ET.ParseError) as exc:
                raise DiagramError(f"cannot decode compressed diagram: {exc}") from exc
        if model is None or model.tag!="mxGraphModel":
            raise DiagramError("diagram has no mxGraphModel")
        item={"id":d.get("id"),"name":d.get("name"),"attributes":dict(d.attrib),"model_attributes":dict(model.attrib),"model_xml":ET.tostring(model,encoding="unicode"),"cells":[]}
        for field,key in (("hardwareJson","hardware"),("layoutJson","layout"),("styleJson","style")):
            if d.get(field):
                try:
                    item[key]=json.loads(base64.b64decode(d.get(field),validate=True))
                except (ValueError,UnicodeDecodeError) as exc:
                    item.setdefault("metadata_errors",[]).append(f"{field}: {exc}")
        for cell in model.iter("mxCell"):
            geo=cell.find("mxGeometry")
            item["cells"].append({"attributes":dict(cell.attrib),"geometry":dict(geo.attrib) if geo is not None else None,"geometry_xml":ET.tostring(geo,encoding="unicode") if geo is not None else None,"xml":ET.tostring(cell,encoding="unicode")})
        result["diagrams"].append(item)
    if not result["diagrams"]:
        raise DiagramError("mxfile contains no diagrams")
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest="command",required=True)
    val=sub.add_parser("validate")
    val.add_argument("hardware")
    val.add_argument("--layout")
    ren=sub.add_parser("render")
    ren.add_argument("hardware")
    ren.add_argument("--layout",required=True)
    ren.add_argument("--style",required=True)
    ren.add_argument("--output",required=True)
    ren.add_argument("--drawio")
    ren.add_argument("--style-revision",type=int)
    ren.add_argument("--allow-nonorthogonal",action="store_true",help="Allow intentionally diagonal routes; warnings remain in manifest")
    imp=sub.add_parser("import")
    imp.add_argument("edited_drawio")
    imp.add_argument("--output",required=True)
    args=parser.parse_args(argv)
    if args.command=="render" and args.style_revision is not None and args.style_revision<=0:
        parser.error("--style-revision must be a positive integer")
    try:
        if args.command=="import":
            data=import_drawio(args.edited_drawio)
            out=Path(str(args.output)+".import.json")
            out.parent.mkdir(parents=True,exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=out.parent,delete=False) as f:
                json.dump(data,f,ensure_ascii=False,indent=2)
                temp=f.name
            os.replace(temp,out)
            print(out)
            return 0
        hardware=read_data(args.hardware)
        sem=validate_hardware(hardware)
        if args.command=="validate":
            lay=validate_layout(read_data(args.layout),hardware,sem) if args.layout else {"errors":[],"warnings":[]}
            report={"errors":sem["errors"]+lay["errors"],"warnings":sem["warnings"]+lay["warnings"]}
            print(json.dumps(report,ensure_ascii=False,indent=2))
            return 2 if report["errors"] else 0
        manifest=render(hardware,read_data(args.layout),read_data(args.style),args.output,args.drawio,args.style_revision,args.allow_nonorthogonal)
        print(json.dumps(manifest,ensure_ascii=False,indent=2))
        return 0 if manifest["exports_complete"] else 3
    except (DiagramError,OSError,TypeError,KeyError) as exc:
        print(f"error: {exc}",file=sys.stderr)
        return 2


if __name__=="__main__":
    sys.exit(main())

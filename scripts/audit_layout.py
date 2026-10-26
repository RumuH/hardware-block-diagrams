#!/usr/bin/env python3
"""Report arrow clearance, estimated title collisions and container whitespace.

Density values are diagnostics, not an automatic quality score or layout optimizer.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagram


def audit(hardware, layout, style):
    sem = diagram.validate_hardware(hardware)
    geo = diagram.validate_layout(layout, hardware, sem)
    errors = sem["errors"]+geo["errors"]+diagram.validate_style(style)
    if errors:
        return {"status": "invalid", "errors": errors}
    nodes, modules = layout["nodes"], sem["modules"]
    tokens = style.get("tokens", {})
    findings, arrows, density = [], {}, {}
    titles = {}
    for mid, n in nodes.items():
        if n.get("shape") == "domain":
            m = modules[mid]
            label = m["label"]+(f" ×{m['count']}" if m.get("count",1)>1 else "")
            x, y, w, h = diagram.domain_title_rect(n, label, n.get("font_size", tokens.get("font_size", 14)))
            titles[mid] = (x, y, x+w, y+h)
    for cid, c in sem["connections"].items():
        e = layout["edges"][cid]
        path = diagram.edge_route(c, e, nodes)
        if e.get("wide"):
            dimensions = diagram.arrow_dimensions(c, e, tokens, nodes)
            arrows[cid] = dimensions
            for end in ("start", "end"):
                if end == "start" and not c.get("bidirectional"):
                    continue
                segment = dimensions["first_segment" if end == "start" else "last_segment"]
                if segment < 4 or dimensions[end+"_length"] <= 1:
                    findings.append({"type": "arrow_clearance", "edge": cid, "end": end, "available": segment})
            half_width = (dimensions["shaft_width"]+1)/2
        else:
            half_width = tokens.get("edge_width", 2)/2
        for mid, rect in titles.items():
            padded = (rect[0]-half_width, rect[1]-half_width, rect[2]+half_width, rect[3]+half_width)
            if any(diagram._segment_hits_rect(a,b,padded) for a,b in zip(path,path[1:])):
                findings.append({"type": "estimated_title_collision", "edge": cid, "module": mid, "title_bounds": list(rect)})
    for mid, m in modules.items():
        children = [n for child, n in nodes.items() if modules[child].get("parent") == mid]
        if not children:
            continue
        parent = nodes[mid]
        bounds = (min(n["x"] for n in children), min(n["y"] for n in children),
                  max(n["x"]+n["w"] for n in children), max(n["y"]+n["h"] for n in children))
        area = parent["w"]*parent["h"]
        density[mid] = {"direct_child_area_fraction": round(sum(n["w"]*n["h"] for n in children)/area,4),
                        "child_envelope_fraction": round((bounds[2]-bounds[0])*(bounds[3]-bounds[1])/area,4),
                        "margins": {"left": bounds[0]-parent["x"], "top": bounds[1]-parent["y"],
                                    "right": parent["x"]+parent["w"]-bounds[2], "bottom": parent["y"]+parent["h"]-bounds[3]}}
    return {"status": "ok", "errors": [], "findings": findings, "arrows": arrows,
            "container_space": density, "estimated_title_bounds": {k:list(v) for k,v in titles.items()},
            "limitations": "Title bounds are estimates. Whitespace may carry routing or hierarchy meaning; fractions are not pass/fail scores. Other text, pin labels and full arrow outlines still require PNG review."}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("hardware", type=Path); p.add_argument("--layout", type=Path, required=True)
    p.add_argument("--style", type=Path, required=True); p.add_argument("--report", type=Path)
    a = p.parse_args(argv)
    try:
        if a.report and any(a.report.resolve()==x.resolve() or a.report.exists() and x.exists() and a.report.samefile(x) for x in (a.hardware,a.layout,a.style)):
            raise ValueError("report must not overwrite an input")
        result = audit(*(diagram.read_data(x) for x in (a.hardware,a.layout,a.style)))
        output = json.dumps(result, ensure_ascii=False, indent=2)+"\n"
        if a.report:
            a.report.parent.mkdir(parents=True, exist_ok=True)
            a.report.write_text(output, encoding="utf-8")
        print(output, end="")
        return 0 if result["status"] == "ok" else 1
    except (ValueError, OSError, diagram.DiagramError) as exc:
        print(f"layout audit failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Small, local, append-only style/sample library (stdlib only)."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import math
from datetime import datetime, timezone
from pathlib import Path

SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ORIGINS = {"user-original", "user-correction", "web", "generated"}
VIEWS = {"composition", "microarchitecture"}
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def safe(value: str, label: str = "identifier") -> str:
    if not isinstance(value, str) or not SLUG.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


class Library:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser()
        self.state_path = self.root / "library.json"
        self.samples_dir = self.root / "samples"
        self.styles_dir = self.root / "styles"

    def state(self):
        if not self.state_path.exists(): return {"version": 1, "samples": {}, "styles": {}, "default_style": None}
        with self.state_path.open(encoding="utf-8") as f: return json.load(f)

    def save(self, state): atomic_json(self.state_path, state)

    def usable(self, st, entry):
        """A revision is active only while every captured sample remains intact and approved."""
        try:
            revision_path = self.root / entry["file"]
            with revision_path.open(encoding="utf-8") as f: profile = json.load(f)
            digest = hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if entry.get("content_sha256") and digest != entry["content_sha256"]: return False
        except (OSError, ValueError, json.JSONDecodeError, KeyError):
            return False
        for sid in entry.get("sample_ids", []):
            rec = st.get("samples", {}).get(sid)
            if not rec or rec.get("status") != "approved": return False
            path = self.root / rec.get("file", "")
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != rec.get("sha256"): return False
        return True

    def _lock(self):
        p = self.root / ".library.lock"; p.parent.mkdir(parents=True, exist_ok=True)
        try: fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError: raise ValueError("library mutation already in progress")
        os.close(fd); return p

    def sample_add(self, sid, source_file, origin, source, page=None):
        sid = safe(sid, "sample id")
        if origin not in ORIGINS: raise ValueError("invalid origin")
        if not source or not str(source).strip(): raise ValueError("source is required")
        src = Path(source_file)
        if not src.is_file(): raise ValueError(f"sample file not found: {src}")
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        lock = self._lock()
        try:
            st = self.state(); old = st["samples"].get(sid)
            if old:
                if old["sha256"] != digest: raise ValueError(f"sample id already exists with different bytes: {sid}")
                return old
            ext = src.suffix.lower() if src.suffix and re.fullmatch(r"\.[a-z0-9]{1,8}", src.suffix.lower()) else ".bin"
            dest = self.samples_dir / f"{sid}{ext}"; dest.parent.mkdir(parents=True, exist_ok=True)
            with src.open("rb") as inp, dest.open("xb") as out: shutil.copyfileobj(inp, out)
            rec = {"id": sid, "file": str(dest.relative_to(self.root)), "origin": origin,
                   "source": str(source), "sha256": digest, "status": "pending", "created_at": now(), "decisions": []}
            if page is not None: rec["page"] = page
            st["samples"][sid] = rec; self.save(st); return rec
        finally:
            try: lock.unlink()
            except FileNotFoundError: pass

    def decide(self, sid, status, evidence):
        sid = safe(sid, "sample id")
        if not evidence or not evidence.strip(): raise ValueError("evidence is required")
        lock = self._lock()
        try:
            st = self.state(); rec = st["samples"].get(sid)
            if not rec: raise ValueError(f"unknown sample: {sid}")
            if status == "approved" and rec["origin"] == "generated": raise ValueError("generated samples cannot be approved for training")
            decision = {"status": status, "evidence": evidence.strip(), "decided_at": now()}
            rec.setdefault("decisions", []).append(decision); rec["status"] = status
            rec["evidence"] = decision["evidence"]; rec["decided_at"] = decision["decided_at"]
            self.save(st); return rec
        finally:
            try: lock.unlink()
            except FileNotFoundError: pass

    def learn(self, style, profile_file, supplied, evidence):
        style = safe(style, "style name")
        if not evidence or not evidence.strip(): raise ValueError("evidence is required")
        with open(profile_file, encoding="utf-8") as f: profile = json.load(f)
        validate_profile(profile, style)
        declared = profile.get("sample_ids", [])
        if not isinstance(declared, list) or any(not isinstance(x, str) for x in declared): raise ValueError("sample_ids must be an array of strings")
        ids = list(dict.fromkeys(declared + list(supplied)))
        if not ids: raise ValueError("at least one approved sample is required")
        profile = copy.deepcopy(profile); profile["sample_ids"] = ids
        lock = self._lock()
        try:
            st = self.state(); info = st["styles"].setdefault(style, {"active": 0, "revisions": []})
            for sid in ids:
                sid = safe(sid, "sample id"); rec = st["samples"].get(sid)
                if not rec or rec.get("status") != "approved": raise ValueError(f"sample is not approved: {sid}")
                if rec.get("origin") == "generated": raise ValueError(f"generated sample cannot be learned: {sid}")
                sample_path = self.root / rec.get("file", "")
                if not sample_path.is_file() or hashlib.sha256(sample_path.read_bytes()).hexdigest() != rec.get("sha256"):
                    raise ValueError(f"approved sample source has changed: {sid}")
            revdir = self.styles_dir / style / "revisions"; revdir.mkdir(parents=True, exist_ok=True)
            existing = [int(p.stem) for p in revdir.iterdir() if p.is_file() and p.stem.isdigit()]
            rev = max([0] + existing + [x.get("revision", 0) for x in info["revisions"]]) + 1
            atomic_json(revdir / f"{rev}.json", profile)
            content_hash = hashlib.sha256(json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            info["revisions"].append({"revision": rev, "file": str((revdir / f"{rev}.json").relative_to(self.root)), "content_sha256": content_hash, "evidence": evidence.strip(), "created_at": now(), "sample_ids": ids})
            info["active"] = rev; self.save(st)
            return {"style": style, "revision": rev, "profile": profile}
        finally:
            try: lock.unlink()
            except FileNotFoundError: pass


def validate_profile(p, style=None):
    if not isinstance(p, dict): raise ValueError("style profile must be an object")
    allowed = {"version", "name", "view", "tokens", "rules", "sample_ids"}
    unknown = set(p) - allowed
    if unknown: raise ValueError(f"unknown style fields: {', '.join(sorted(unknown))}")
    if p.get("version") != 1 or not isinstance(p.get("name"), str) or not p["name"].strip(): raise ValueError("profile requires version 1 and nonempty name")
    safe(p["name"], "style name")
    if style and p["name"] != style: raise ValueError("profile name does not match style")
    if not isinstance(p.get("view"), str) or p.get("view") not in VIEWS: raise ValueError("view must be composition or microarchitecture")
    t = p.get("tokens")
    if not isinstance(t, dict): raise ValueError("tokens must be an object")
    token_keys = {"background", "font_family", "font_size", "edge_font_size", "text_color", "border_color", "colors", "edge_colors", "edge_width", "wide_width", "corner_radius"}
    unknown_tokens = set(t) - token_keys
    if unknown_tokens: raise ValueError(f"unknown style tokens: {', '.join(sorted(unknown_tokens))}")
    for k in ("background", "text_color", "border_color"):
        if k in t and (not isinstance(t[k], str) or not HEX.fullmatch(t[k])): raise ValueError(f"invalid color token: {k}")
    if "font_family" in t and not isinstance(t["font_family"], str): raise ValueError("font_family must be a string")
    for k in ("font_size", "edge_font_size", "edge_width", "wide_width"):
        if k in t and (not isinstance(t[k], (int, float)) or isinstance(t[k], bool) or not math.isfinite(t[k]) or t[k] <= 0): raise ValueError(f"{k} must be a finite positive number")
    if "corner_radius" in t and (not isinstance(t["corner_radius"], (int, float)) or isinstance(t["corner_radius"], bool) or not math.isfinite(t["corner_radius"]) or t["corner_radius"] < 0): raise ValueError("corner_radius must be a finite nonnegative number")
    for k in ("colors", "edge_colors"):
        if k in t:
            if not isinstance(t[k], dict) or any(not isinstance(v, str) or not HEX.fullmatch(v) for v in t[k].values()): raise ValueError(f"invalid {k} token")
    if not isinstance(p.get("rules"), list) or not p["rules"] or any(not isinstance(x, str) or not x.strip() for x in p["rules"]): raise ValueError("rules must be a nonempty array of nonempty strings")
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="Local approved-sample and immutable style library")
    ap.add_argument("--root", default="~/.codex/hardware-block-diagrams")
    sub = ap.add_subparsers(dest="cmd", required=True)
    x=sub.add_parser("sample-add"); x.add_argument("--id",required=True); x.add_argument("--file",required=True); x.add_argument("--origin",required=True, choices=sorted(ORIGINS)); x.add_argument("--source",required=True); x.add_argument("--page")
    for name,status in (("approve","approved"),("reject","rejected")):
        x=sub.add_parser(name); x.add_argument("id"); x.add_argument("--evidence",required=True); x.set_defaults(status=status)
    x=sub.add_parser("learn"); x.add_argument("style"); x.add_argument("--profile",required=True); x.add_argument("--samples",nargs="*",default=[]); x.add_argument("--evidence",required=True)
    x=sub.add_parser("list"); x.add_argument("kind", nargs="?", choices=["samples","styles"], default=None)
    x=sub.add_parser("show"); x.add_argument("style"); x.add_argument("--revision",type=int); x.add_argument("--output"); x.add_argument("--allow-inactive-evidence",action="store_true")
    x=sub.add_parser("default"); x.add_argument("style")
    x=sub.add_parser("rollback"); x.add_argument("style"); x.add_argument("revision",type=int)
    a=ap.parse_args(argv); lib=Library(a.root)
    try:
        if a.cmd=="sample-add": out=lib.sample_add(a.id,a.file,a.origin,a.source,a.page)
        elif a.cmd in ("approve","reject"): out=lib.decide(a.id,a.status,a.evidence)
        elif a.cmd=="learn": out=lib.learn(a.style,a.profile,a.samples,a.evidence)
        elif a.cmd=="list":
            st=lib.state(); out=st["samples"] if a.kind=="samples" else st["styles"] if a.kind=="styles" else st
        elif a.cmd=="show":
            st=lib.state(); info=st["styles"].get(safe(a.style,"style name"));
            if not info: raise ValueError(f"unknown style: {a.style}")
            rev=a.revision or info["active"]; entry=next((x for x in info["revisions"] if x["revision"]==rev),None)
            if not entry: raise ValueError(f"unknown revision: {rev}")
            usable = lib.usable(st, entry)
            if not usable and not a.allow_inactive_evidence: raise ValueError("revision evidence is inactive (sample revoked or source changed); use --allow-inactive-evidence for audit")
            with (lib.root / entry["file"]).open(encoding="utf-8") as f: out={"style":a.style,"revision":rev,"active":info["active"],"profile":json.load(f),"metadata":entry}
            out["usable"] = usable
            if a.output: atomic_json(Path(a.output),out["profile"])
        elif a.cmd=="default":
            st=lib.state(); info=st["styles"].get(safe(a.style,"style name"));
            if not info: raise ValueError(f"unknown style: {a.style}")
            lock=lib._lock()
            try:
                st=lib.state(); info=st["styles"].get(a.style)
                if not info: raise ValueError(f"unknown style: {a.style}")
                entry=next(x for x in info["revisions"] if x["revision"]==info["active"])
                if not lib.usable(st, entry): raise ValueError("style's active revision has inactive evidence")
                st["default_style"] = a.style; lib.save(st); out={"style":a.style,"revision":info["active"],"default":True}
            finally:
                try: lock.unlink()
                except FileNotFoundError: pass
        else:
            st=lib.state(); info=st["styles"].get(safe(a.style,"style name"));
            if not info or not any(x["revision"]==a.revision for x in info["revisions"]): raise ValueError("unknown style or revision")
            lock=lib._lock()
            try:
                st=lib.state(); info=st["styles"].get(a.style); entry=next(x for x in info["revisions"] if x["revision"]==a.revision)
                if not lib.usable(st, entry): raise ValueError("cannot activate revision with inactive evidence")
                info["active"]=a.revision; lib.save(st); out={"style":a.style,"revision":a.revision}
            finally:
                try: lock.unlink()
                except FileNotFoundError: pass
        print(json.dumps(out,indent=2,sort_keys=True)); return 0
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error":str(e)}), file=sys.stderr); return 2

if __name__ == "__main__": sys.exit(main())

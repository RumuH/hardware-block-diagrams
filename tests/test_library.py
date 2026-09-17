import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "library.py"


class LibraryCLITests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "lib"
        self.src = Path(self.td.name) / "sample.png"
        self.src.write_bytes(b"original image bytes")

    def tearDown(self): self.td.cleanup()

    def cli(self, *args, ok=True):
        p = subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root), *args], text=True, capture_output=True)
        if ok: self.assertEqual(p.returncode, 0, p.stderr)
        else: self.assertNotEqual(p.returncode, 0)
        return json.loads(p.stdout or p.stderr)

    def profile(self, name="dark", ids=None, path=None):
        p = {"version": 1, "name": name, "view": "composition", "tokens": {"background":"#ffffff", "font_size":12, "edge_width":1}, "rules":["keep labels readable"], "sample_ids": ids or []}
        path = path or Path(self.td.name) / (name + ".json"); path.write_text(json.dumps(p)); return path

    def test_sample_provenance_copy_and_decision(self):
        out = self.cli("sample-add", "--id", "s1", "--file", str(self.src), "--origin", "user-original", "--source", "chat upload", "--page", "2")
        self.assertEqual(out["status"], "pending")
        self.assertTrue((self.root / out["file"]).is_file())
        self.src.write_bytes(b"changed")
        self.assertNotEqual((self.root / out["file"]).read_bytes(), self.src.read_bytes())
        self.cli("approve", "s1", "--evidence", "user explicitly selected this sample")
        self.cli("reject", "s1", "--evidence", "user withdrew approval")
        self.cli("learn", "dark", "--profile", str(self.profile(ids=["s1"])), "--evidence", "try", ok=False)

    def test_generated_cannot_approve_and_traversal_rejected(self):
        self.cli("sample-add", "--id", "gen", "--file", str(self.src), "--origin", "generated", "--source", "assistant output")
        self.cli("approve", "gen", "--evidence", "explicit", ok=False)
        self.cli("sample-add", "--id", "../oops", "--file", str(self.src), "--origin", "web", "--source", "https://example.test", ok=False)

    def test_immutable_revisions_default_and_rollback(self):
        self.cli("sample-add", "--id", "s1", "--file", str(self.src), "--origin", "user-correction", "--source", "correction in chat")
        self.cli("approve", "s1", "--evidence", "user correction is authorized feedback")
        p1 = self.profile(ids=["s1"])
        r1 = self.cli("learn", "dark", "--profile", str(p1), "--samples", "s1", "--evidence", "first")
        p1.write_text(json.dumps({**json.loads(p1.read_text()), "tokens": {"background":"#000000", "font_size":14}, "sample_ids":["s1"]}))
        r2 = self.cli("learn", "dark", "--profile", str(p1), "--samples", "s1", "--evidence", "second")
        self.assertEqual((r1["revision"], r2["revision"]), (1, 2))
        old = self.cli("show", "dark", "--revision", "1")
        self.assertEqual(old["profile"]["tokens"]["background"], "#ffffff")
        self.cli("rollback", "dark", "1")
        self.assertEqual(self.cli("show", "dark")["active"], 1)
        self.cli("default", "dark")
        self.assertEqual(self.cli("list")["default_style"], "dark")

    def test_bad_profile_does_not_create_revision(self):
        self.cli("sample-add", "--id", "s1", "--file", str(self.src), "--origin", "user-original", "--source", "upload")
        self.cli("approve", "s1", "--evidence", "selected")
        bad = self.profile(ids=["s1"]); bad.write_text(json.dumps({"version":1,"name":"dark","view":"composition","tokens":{"background":"red"},"rules":["x"],"sample_ids":["s1"]}))
        self.cli("learn", "dark", "--profile", str(bad), "--evidence", "bad", ok=False)
        self.assertFalse((self.root / "styles" / "dark").exists())

    def test_revoked_and_tampered_evidence_is_unusable(self):
        self.cli("sample-add", "--id", "s1", "--file", str(self.src), "--origin", "user-original", "--source", "upload")
        self.cli("approve", "s1", "--evidence", "selected")
        p = self.profile(ids=["s1"])
        self.cli("learn", "dark", "--profile", str(p), "--evidence", "learned")
        sample = self.root / "samples" / "s1.png"
        sample.write_bytes(b"tampered")
        self.cli("learn", "other", "--profile", str(self.profile("other", ["s1"])), "--evidence", "must reject", ok=False)
        self.cli("show", "dark", ok=False)
        audit = self.cli("show", "dark", "--allow-inactive-evidence")
        self.assertFalse(audit["usable"])
        self.cli("default", "dark", ok=False)
        self.cli("rollback", "dark", "1", ok=False)

    def test_validation_history_raw_output_and_orphan_revision(self):
        self.cli("sample-add", "--id", "s1", "--file", str(self.src), "--origin", "user-original", "--source", "upload")
        self.cli("approve", "s1", "--evidence", "first approval")
        self.cli("reject", "s1", "--evidence", "temporary rejection")
        decisions = self.cli("list", "samples")["s1"]["decisions"]
        self.assertEqual([d["status"] for d in decisions], ["approved", "rejected"])
        self.cli("approve", "s1", "--evidence", "approval restored")
        p = self.profile(ids=["s1"])
        self.cli("learn", "dark", "--profile", str(p), "--evidence", "first")
        revdir = self.root / "styles" / "dark" / "revisions"
        (revdir / "9.json").write_text("{}")
        out = Path(self.td.name) / "raw.json"
        shown = self.cli("show", "dark", "--output", str(out))
        self.assertEqual(json.loads(out.read_text())["name"], "dark")
        self.assertIn("content_sha256", shown["metadata"])
        p2 = self.profile(ids=["s1"]); self.cli("learn", "dark", "--profile", str(p2), "--evidence", "second")
        self.assertTrue((revdir / "10.json").exists())

    def test_invalid_numeric_view_and_empty_samples(self):
        for tokens in ({"font_size": float("nan")}, {"corner_radius": -1}, {"corner_radius": float("inf")}):
            p = self.profile(ids=[]); obj = json.loads(p.read_text()); obj["tokens"].update(tokens); p.write_text(json.dumps(obj, allow_nan=True))
            self.cli("learn", "dark", "--profile", str(p), "--evidence", "bad", ok=False)
        p = self.profile(ids=[]); obj = json.loads(p.read_text()); obj["view"] = []; p.write_text(json.dumps(obj))
        self.cli("learn", "dark", "--profile", str(p), "--evidence", "bad", ok=False)
        p = self.profile(ids=[]); obj = json.loads(p.read_text()); obj["rules"] = []; p.write_text(json.dumps(obj))
        self.cli("learn", "dark", "--profile", str(p), "--evidence", "bad", ok=False)

    def test_root_lock_prevents_cross_style_lost_update(self):
        self.root.mkdir(parents=True)
        lock = self.root / ".library.lock"; lock.write_text("held")
        self.cli("sample-add", "--id", "s1", "--file", str(self.src), "--origin", "user-original", "--source", "upload", ok=False)
        self.assertFalse((self.root / "library.json").exists())


if __name__ == "__main__": unittest.main()

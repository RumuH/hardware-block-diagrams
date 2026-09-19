import base64
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zlib


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "diagram.py"
spec = importlib.util.spec_from_file_location("hardware_diagram", SCRIPT)
diagram = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagram)


def fixture():
    hardware = {
        "version": 1, "title": "CPU", "view": "microarchitecture",
        "modules": [
            {"id": "soc", "label": "SoC", "kind": "soc"},
            {"id": "cpu", "label": "Core", "parent": "soc", "kind": "core"},
            {"id": "alu", "label": "ALU", "parent": "cpu", "kind": "compute", "ports": [{"id": "out", "direction": "out", "width": 32}]},
            {"id": "rf", "label": "Registers", "parent": "cpu", "kind": "storage", "ports": [{"id": "in", "direction": "in", "width": 32}]},
        ],
        "connections": [{"id": "writeback", "source": "alu.out", "target": "rf.in", "kind": "feedback", "width": 32, "label": "WB"}],
    }
    layout = {
        "version": 1, "canvas": {"width": 600, "height": 400},
        "nodes": {
            "soc": {"x": 20, "y": 20, "w": 560, "h": 360},
            "cpu": {"x": 60, "y": 60, "w": 480, "h": 280},
            "alu": {"x": 100, "y": 130, "w": 120, "h": 80},
            "rf": {"x": 350, "y": 130, "w": 130, "h": 80},
        },
        "edges": {"writeback": {"source_side": "right", "target_side": "left", "points": [[280, 170]], "wide": True}},
    }
    style = {"version": 1, "name": "test", "view": "microarchitecture", "tokens": {"wide_width": 14}}
    return hardware, layout, style


class SemanticTests(unittest.TestCase):
    def test_valid_and_unknown_fields(self):
        h, _, _ = fixture()
        self.assertEqual(diagram.validate_hardware(h)["errors"], [])
        h["modules"][0]["parnt"] = "x"
        self.assertIn("unknown field", " ".join(diagram.validate_hardware(h)["errors"]))

    def test_references_parent_cycle_and_shared_semantics(self):
        h, _, _ = fixture()
        h["modules"][0]["parent"] = "alu"
        h["modules"][0]["shared_with"] = ["absent"]
        h["connections"][0]["target"] = "rf.absent"
        errors = " ".join(diagram.validate_hardware(h)["errors"])
        self.assertIn("parent cycle", errors)
        self.assertIn("unknown shared_with", errors)
        self.assertIn("unknown port", errors)

    def test_directions_width_and_protocol(self):
        h, _, _ = fixture()
        h["modules"][2]["ports"][0]["direction"] = "in"
        h["modules"][3]["ports"][0]["width"] = 16
        h["modules"][3]["ports"][0]["protocol"] = "A"
        h["connections"][0]["protocol"] = "B"
        errors = " ".join(diagram.validate_hardware(h)["errors"])
        self.assertIn("source port direction", errors)
        self.assertIn("known width mismatch", errors)
        self.assertIn("protocol mismatch", errors)

    def test_unknown_width_is_not_invented(self):
        h, _, _ = fixture()
        del h["modules"][2]["ports"][0]["width"]
        del h["modules"][3]["ports"][0]["width"]
        del h["connections"][0]["width"]
        self.assertEqual(diagram.validate_hardware(h)["errors"], [])

    def test_malformed_shapes_report_errors_without_tracebacks(self):
        h, l, s = fixture()
        h["view"] = []
        h["modules"][0]["parent"] = {"bad": 1}
        h["modules"][0]["shared_with"] = [{"bad": 1}]
        h["modules"][2]["ports"] = {"bad": 1}
        h["connections"][0]["kind"] = []
        h["connections"][0]["protocol"] = []
        self.assertTrue(diagram.validate_hardware(h)["errors"])
        l["nodes"]["cpu"]["label_position"] = []
        l["edges"]["writeback"]["source_side"] = []
        self.assertTrue(diagram.validate_layout(l, h)["errors"])
        s["view"] = []
        self.assertTrue(diagram.validate_style(s))


class GeometryTests(unittest.TestCase):
    def test_valid_nested_and_bad_containment(self):
        h, l, _ = fixture()
        self.assertEqual(diagram.validate_layout(l, h)["errors"], [])
        l["nodes"]["alu"]["x"] = 10
        self.assertIn("outside parent", " ".join(diagram.validate_layout(l, h)["errors"]))

    def test_overlap_and_crossing_diagnostics(self):
        h, l, _ = fixture()
        l["nodes"]["rf"]["x"] = 180
        self.assertIn("overlap", " ".join(diagram.validate_layout(l, h)["warnings"]))
        l["nodes"]["rf"]["x"] = 350
        h["modules"].append({"id": "debug", "label": "Debug", "parent": "cpu"})
        l["nodes"]["debug"] = {"x": 270, "y": 160, "w": 50, "h": 60}
        l["edges"]["writeback"]["points"] = []
        self.assertIn("crosses unrelated module debug", " ".join(diagram.validate_layout(l, h)["warnings"]))

    def test_malformed_layout(self):
        h, l, _ = fixture()
        l["edges"]["writeback"]["source_pos"] = 2
        l["edges"]["writeback"]["points"] = [["x", 2]]
        l["nodes"]["alu"]["w"] = -1
        errors = " ".join(diagram.validate_layout(l, h)["errors"])
        self.assertIn("source_pos", errors)
        self.assertIn("points", errors)
        self.assertIn("finite numbers", errors)

    def test_native_hierarchy_and_wide_bound_arrow(self):
        h, l, s = fixture()
        doc = ET.fromstring(diagram.drawio_xml(h, l, s))
        cells = {c.get("id"): c for c in doc.iter("mxCell")}
        self.assertEqual(cells["node:cpu"].get("parent"), "node:soc")
        self.assertEqual(cells["node:alu"].get("parent"), "node:cpu")
        self.assertEqual(cells["node:alu"].find("mxGeometry").get("x"), "40")
        edge = cells["edge:writeback"]
        self.assertEqual(edge.get("source"), "node:alu")
        self.assertEqual(edge.get("target"), "node:rf")
        self.assertEqual(edge.get("parent"), "1")
        self.assertIn("shape=flexArrow", edge.get("style"))
        self.assertIn("fillColor=#40566e", edge.get("style"))
        self.assertIn("strokeWidth=1", edge.get("style"))
        self.assertIn("width=14", edge.get("style"))
        self.assertNotIn("rounded=1", cells["node:soc"].get("style"))
        self.assertEqual(edge.find("mxGeometry/Array/mxPoint").get("x"), "280")

    def test_corner_radius_and_semantic_edge_label(self):
        h, l, s = fixture()
        h["connections"][0]["protocol"] = "AXI"
        s["tokens"]["corner_radius"] = 8
        s["tokens"]["edge_font_size"] = 16
        doc = ET.fromstring(diagram.drawio_xml(h,l,s))
        cells = {c.get("id"): c for c in doc.iter("mxCell")}
        self.assertIn("rounded=1", cells["node:soc"].get("style"))
        self.assertIn("arcSize=16", cells["node:soc"].get("style"))
        self.assertEqual(cells["edge:writeback"].get("value"), "WB · AXI · 32-bit")
        self.assertIn("fontSize=16", cells["edge:writeback"].get("style"))

    def test_route_diagnostics(self):
        h, l, _ = fixture()
        l["edges"]["writeback"]["points"] = [[220,170], [700,200], [700,200]]
        warnings = " ".join(diagram.validate_layout(l,h)["warnings"])
        self.assertIn("zero-length", warnings)
        self.assertIn("nonorthogonal", warnings)
        self.assertIn("outside canvas", warnings)
        self.assertIn("perpendicular", warnings)

    def test_style_rejects_invalid_tokens(self):
        _, _, s = fixture()
        s["tokens"].update({"background":"red", "font_family":"Arial;strokeColor=#ff0000", "corner_radius":-1,"colors":{"core":"#bad"}})
        errors = " ".join(diagram.validate_style(s))
        self.assertIn("background", errors)
        self.assertIn("font_family", errors)
        self.assertIn("corner_radius", errors)
        self.assertIn("colors", errors)


class FileTests(unittest.TestCase):
    def test_import_preserves_edits_and_metadata(self):
        h, l, s = fixture()
        doc = ET.fromstring(diagram.drawio_xml(h, l, s))
        root = doc.find("diagram/mxGraphModel/root")
        ET.SubElement(root, "mxCell", {"id": "foreign", "value": "Edited", "vertex": "1", "parent": "1", "style": "mystery=1;"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "edit.drawio"
            path.write_bytes(ET.tostring(doc))
            result = diagram.import_drawio(path)["diagrams"][0]
            self.assertEqual(result["hardware"], h)
            self.assertTrue(any(c["attributes"].get("id") == "foreign" for c in result["cells"]))
            self.assertIn("mystery=1", result["model_xml"])

    def test_compressed_import_and_malformed_xml(self):
        h, l, s = fixture()
        native = ET.fromstring(diagram.drawio_xml(h, l, s))
        model = native.find("diagram/mxGraphModel")
        xml = ET.tostring(model, encoding="unicode")
        packed = zlib.compressobj(wbits=-15)
        encoded = base64.b64encode(packed.compress(xml.encode()) + packed.flush()).decode()
        d = native.find("diagram")
        d.remove(model)
        d.text = encoded
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compressed.drawio"
            path.write_bytes(ET.tostring(native))
            self.assertEqual(len(diagram.import_drawio(path)["diagrams"][0]["cells"]), 7)
            path.write_text("<!DOCTYPE foo><mxfile/>")
            with self.assertRaises(diagram.DiagramError):
                diagram.import_drawio(path)

    def test_missing_desktop_is_non_success_with_native_artifact(self):
        h, l, s = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "cpu"
            result = diagram.render(h, l, s, prefix, drawio=str(Path(tmp) / "absent"))
            self.assertFalse(result["exports_complete"])
            self.assertTrue(prefix.with_suffix(".drawio").exists())
            self.assertFalse(prefix.with_suffix(".png").exists())
            self.assertFalse(prefix.with_suffix(".svg").exists())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(diagram.main(["render", str(self._json(tmp,"h",h)), "--layout", str(self._json(tmp,"l",l)), "--style", str(self._json(tmp,"s",s)), "--output", str(Path(tmp)/"cpu2"), "--drawio", str(Path(tmp)/"absent")]), 3)

    def test_failed_partial_export_is_not_published(self):
        h, l, s = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            binary=Path(tmp)/"fake-drawio"
            binary.write_text("fake")
            prefix=Path(tmp)/"cpu"
            def failed_run(cmd, **_kwargs):
                Path(cmd[cmd.index("-o")+1]).write_text("partial")
                return diagram.subprocess.CompletedProcess(cmd,1,"","failed")
            with patch.object(diagram.subprocess,"run",side_effect=failed_run):
                manifest=diagram.render(h,l,s,prefix,drawio=binary,style_revision=7)
            self.assertFalse(manifest["exports_complete"])
            self.assertEqual(manifest["style"]["revision"],7)
            self.assertFalse(prefix.with_suffix(".svg").exists())
            self.assertFalse(prefix.with_suffix(".png").exists())

    def _json(self, folder, name, data):
        path = Path(folder) / f"{name}.json"
        path.write_text(json.dumps(data))
        return path

    def test_diagonal_render_rejected_unless_explicit(self):
        h, l, s = fixture()
        l["edges"]["writeback"]["points"] = [[280, 240]]
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "cpu"
            prefix.with_suffix(".drawio").write_text("previous")
            with self.assertRaisesRegex(diagram.DiagramError, "Orthogonal routing required"):
                diagram.render(h, l, s, prefix)
            self.assertEqual(prefix.with_suffix(".drawio").read_text(), "previous")
            result = diagram.render(h, l, s, prefix, drawio=str(Path(tmp)/"missing"), allow_nonorthogonal=True)
            self.assertTrue(any("nonorthogonal" in w for w in result["issues"]["warnings"]))

    def test_invalid_render_does_not_overwrite(self):
        h, l, s = fixture()
        l["nodes"]["alu"]["x"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "cpu"
            old = prefix.with_suffix(".drawio")
            old.write_text("previous")
            with self.assertRaises(diagram.DiagramError):
                diagram.render(h, l, s, prefix)
            self.assertEqual(old.read_text(), "previous")


if __name__ == "__main__":
    unittest.main()

"""Contracts and editable behavior of SoC presentation, without hardware invention."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from test_diagram import diagram, fixture


def example():
    h, l, s = fixture()
    h["connections"][0]["kind"] = "bus"
    h["modules"][2]["ports"][0]["role"] = "initiator"
    l["nodes"]["cpu"].update(shape="domain", font_size=16)
    l["nodes"]["rf"].update(shape="bus", w=32, h=140)
    l["edges"]["writeback"].update(wide=False, arrow="none", dashed=True)
    l["legend"] = {"x": 30, "y": 365, "w": 260, "row_height": 24,
                   "entries": [{"connection": "writeback", "label": "Shared interface"}]}
    return h, l, s


class SocPresentationTests(unittest.TestCase):
    def test_domain_bus_pin_native_cells_and_bound_connections(self):
        h, l, s = example()
        h["modules"].append({"id": "pin", "label": "ext_clk", "parent": "soc", "kind": "interface"})
        l["nodes"]["pin"] = {"x": 25, "y": 250, "w": 14, "h": 14, "shape": "pin", "label_side": "left"}
        doc = ET.fromstring(diagram.drawio_xml(h, l, s))
        cells = {c.get("id"): c for c in doc.iter("mxCell")}
        self.assertIn("dashed=1", cells["node:cpu"].get("style"))
        self.assertEqual(cells["title:cpu"].get("parent"), "node:cpu")
        self.assertEqual(cells["title:cpu"].get("value"), "Core")
        self.assertIn("horizontal=0", cells["node:rf"].get("style"))
        self.assertIn("labelPosition=left", cells["node:pin"].get("style"))
        for i in range(2):
            self.assertEqual(cells[f"pin-cross:pin:{i}"].get("parent"), "node:pin")
        edge = cells["edge:writeback"]
        self.assertEqual((edge.get("source"), edge.get("target")), ("node:alu", "node:rf"))
        self.assertIn("endArrow=none", edge.get("style"))
        self.assertIn("dashed=1", edge.get("style"))
        self.assertEqual(cells["master:writeback:-1"].get("parent"), "edge:writeback")
        self.assertEqual(cells["master:writeback:-1"].find("mxGeometry").get("relative"), "1")

    def test_initiator_not_inferred_and_annotation_does_not_change_hardware(self):
        h, l, s = example()
        del h["modules"][2]["ports"][0]["role"]
        before = deepcopy(h)
        doc = ET.fromstring(diagram.drawio_xml(h, l, s))
        self.assertFalse(any(c.get("id", "").startswith("master:") for c in doc.iter("mxCell")))
        self.assertEqual(h, before)

    def test_legend_matches_real_line_and_import_preserves_editable_decorations(self):
        h, l, s = example()
        s["tokens"]["edge_colors"] = {"bus": "#223344"}
        doc = ET.fromstring(diagram.drawio_xml(h, l, s))
        cells = {c.get("id"): c for c in doc.iter("mxCell")}
        for key in ("edge:writeback", "legend-line:0"):
            for style in ("strokeColor=#223344", "dashed=1", "endArrow=none"):
                self.assertIn(style, cells[key].get("style"))
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp)/"test.drawio"
            f.write_bytes(ET.tostring(doc))
            imported = diagram.import_drawio(f)["diagrams"][0]
            self.assertEqual(imported["hardware"], h)
            self.assertEqual(imported["layout"], l)
            self.assertTrue(any(c["attributes"].get("id") == "legend-label:0" for c in imported["cells"]))

    def test_malformed_new_fields_report_errors_without_crashing(self):
        h, l, s = example()
        h["modules"][2]["ports"][0]["role"] = ["initiator"]
        self.assertIn("role", " ".join(diagram.validate_hardware(h)["errors"]))
        for change in ({"shape": []}, {"dashed": 1}, {"font_size": float("nan")}, {"text_direction": "up"}, {"label_side": "left"}):
            changed = deepcopy(l); changed["nodes"]["alu"].update(change)
            self.assertTrue(diagram.validate_layout(changed, fixture()[0])["errors"], change)
        for legend in ([], {}, {"x": 0, "y": 0, "w": 100, "entries": [None]},
                       {"x": 0, "y": 0, "w": 100, "entries": [{"connection": [], "label": 2}]}):
            changed = deepcopy(l); changed["legend"] = legend
            self.assertTrue(diagram.validate_layout(changed, fixture()[0])["errors"])

    def test_no_arrow_cannot_silently_turn_wide_arrow_into_a_bus(self):
        h, l, _ = example(); l["edges"]["writeback"]["wide"] = True
        self.assertIn("incompatible", " ".join(diagram.validate_layout(l, h)["errors"]))

    def test_sample_style_fallback_and_all_attachment_sides(self):
        h, l, s = fixture()
        s["tokens"]["border_color"] = "#112233"
        h["connections"][0]["kind"] = "reset"
        l["edges"]["writeback"]["wide"] = False
        for side, fraction, expected in [("left", .2, (0, .2)), ("right", .7, (1, .7)),
                                          ("top", .3, (.3, 0)), ("bottom", .8, (.8, 1))]:
            l["edges"]["writeback"].update(source_side=side, source_pos=fraction, target_side=side, target_pos=fraction)
            doc = ET.fromstring(diagram.drawio_xml(h, l, s))
            edge = next(c for c in doc.iter("mxCell") if c.get("id") == "edge:writeback")
            fields = dict(x.split("=", 1) for x in edge.get("style").split(";") if "=" in x)
            self.assertEqual((float(fields["exitX"]), float(fields["exitY"])), expected)
            self.assertEqual((float(fields["entryX"]), float(fields["entryY"])), expected)
            self.assertEqual(fields["strokeColor"], "#112233")

    def test_public_example_validates_and_role_markers_match_declared_ports(self):
        root = Path(__file__).resolve().parents[1]
        h = json.loads((root/"examples/soc-bus.hardware.json").read_text())
        l = json.loads((root/"examples/soc-bus.layout.json").read_text())
        s = json.loads((root/"assets/styles/soc-bus.json").read_text())
        self.assertEqual(diagram.validate_hardware(h)["errors"], [])
        self.assertEqual(diagram.validate_layout(l, h)["errors"], [])
        self.assertEqual(diagram.validate_layout(l, h)["warnings"], [])
        self.assertEqual(diagram.validate_style(s), [])
        doc = ET.fromstring(diagram.drawio_xml(h, l, s))
        self.assertEqual(len([c for c in doc.iter("mxCell") if c.get("id", "").startswith("master:")]), 3)

    def test_rotated_bus_and_external_pin_labels_use_appropriate_fit_geometry(self):
        h, l, _ = fixture()
        h["modules"][3]["label"] = "Internal chip bus"
        l["nodes"]["rf"].update(shape="bus", w=30, h=160)
        self.assertNotIn("nodes.rf: label may not fit", diagram.validate_layout(l, h)["warnings"])
        l["nodes"]["rf"].update(shape="pin", w=14, h=14, label_side="right")
        self.assertNotIn("nodes.rf: label may not fit", diagram.validate_layout(l, h)["warnings"])
        l["nodes"]["rf"]["h"] = 20
        self.assertIn("square geometry", " ".join(diagram.validate_layout(l, h)["errors"]))

    def test_small_domain_keeps_native_title_geometry_positive(self):
        h, l, s = fixture()
        l["nodes"]["alu"].update(shape="domain", w=10)
        doc = ET.fromstring(diagram.drawio_xml(h, l, s))
        title = next(c for c in doc.iter("mxCell") if c.get("id") == "title:alu")
        self.assertGreater(float(title.find("mxGeometry").get("width")), 0)


if __name__ == "__main__":
    unittest.main()

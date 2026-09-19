import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_layouts.py"
spec = importlib.util.spec_from_file_location("compare_layouts", SCRIPT)
compare_layouts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare_layouts)


def simple_fixture():
    hardware = {
        "version": 1,
        "title": "Candidate comparison",
        "view": "microarchitecture",
        "modules": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
        "connections": [{"id": "e", "source": "a", "target": "b", "kind": "data"}],
    }
    before = {
        "version": 1,
        "canvas": {"width": 320, "height": 260},
        "nodes": {
            "a": {"x": 20, "y": 80, "w": 40, "h": 40},
            "b": {"x": 220, "y": 180, "w": 40, "h": 40},
        },
        "edges": {"e": {"source_side": "right", "target_side": "left", "points": [[80, 100], [100, 100], [100, 200]]}},
    }
    after = copy.deepcopy(before)
    after["nodes"]["b"].update({"y": 80, "w": 50})
    after["edges"]["e"]["points"] = []
    return hardware, before, after


class LayoutComparisonTests(unittest.TestCase):
    def test_repositioned_block_shortens_route_and_real_bends_are_counted(self):
        hardware, before, after = simple_fixture()
        inputs = copy.deepcopy((hardware, before, after))
        report = compare_layouts.compare_layouts(hardware, before, after)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["identity"]["module_ids"], ["a", "b"])
        self.assertEqual(report["before"]["metrics"]["paths"]["per_edge"]["e"]["bends"], 2)
        self.assertEqual(report["before"]["metrics"]["paths"]["per_edge"]["e"]["polyline_points"], 4)
        self.assertEqual(report["after"]["metrics"]["paths"]["total_bends"], 0)
        self.assertEqual(report["comparison"]["deltas_after_minus_before"]["path_total_bends"], -2)
        self.assertLess(report["comparison"]["deltas_after_minus_before"]["path_total_length"], 0)
        self.assertEqual(report["changes"]["nodes"]["moved"][0]["id"], "b")
        self.assertEqual(report["changes"]["nodes"]["resized"][0]["id"], "b")
        self.assertEqual((hardware, before, after), inputs)
        self.assertNotIn("score", json.dumps(report).lower())

    def test_diagonal_candidate_invalidates_only_bend_comparison_and_surfaces_warning(self):
        hardware, before, after = simple_fixture()
        after["edges"]["e"]["points"] = [[120, 150]]
        report = compare_layouts.compare_layouts(hardware, before, after)

        self.assertEqual(report["status"], "ok")
        self.assertFalse(report["comparison"]["bends_comparable"])
        self.assertIsNone(report["comparison"]["deltas_after_minus_before"]["path_total_bends"])
        self.assertGreater(report["after"]["metrics"]["paths"]["nonorthogonal_segments"], 0)
        self.assertTrue(any("nonorthogonal segment" in warning for warning in report["after"]["validation"]["warnings"]))

    def test_crossings_collinear_overlap_collisions_and_parent_containment(self):
        modules = [{"id": "root", "label": "Root"}]
        nodes = {"root": {"x": 0, "y": 0, "w": 400, "h": 260}}
        positions = {
            "a": (10, 80), "b": (310, 80),
            "c": (180, 10), "d": (180, 190),
            "e": (60, 80), "f": (260, 80),
            "clash": (20, 90),
        }
        for ident, (x, y) in positions.items():
            modules.append({"id": ident, "label": ident.upper(), "parent": "root"})
            nodes[ident] = {"x": x, "y": y, "w": 30, "h": 40}
        hardware = {
            "version": 1, "title": "Interactions", "view": "microarchitecture", "modules": modules,
            "connections": [
                {"id": "h1", "source": "a", "target": "b", "kind": "data"},
                {"id": "v", "source": "c", "target": "d", "kind": "control"},
                {"id": "h2", "source": "e", "target": "f", "kind": "bus"},
            ],
        }
        layout = {
            "version": 1, "canvas": {"width": 400, "height": 260}, "nodes": nodes,
            "edges": {
                "h1": {"source_side": "right", "target_side": "left"},
                "v": {"source_side": "bottom", "target_side": "top"},
                "h2": {"source_side": "right", "target_side": "left"},
            },
        }
        metrics = compare_layouts.compare_layouts(hardware, layout, layout)["before"]["metrics"]

        self.assertEqual(metrics["interedge"]["unique_crossings"], 2)
        self.assertEqual(metrics["interedge"]["collinear_overlap_pairs"], 1)
        self.assertGreater(metrics["interedge"]["collinear_overlap_length"], 0)
        self.assertGreater(metrics["unrelated_node_intersections"]["count"], 0)
        self.assertIn(["a", "clash"], metrics["node_overlaps"]["pairs"])
        self.assertNotIn(["a", "root"], metrics["node_overlaps"]["pairs"])

    def test_same_declared_endpoint_is_not_a_crossing_but_t_touch_is(self):
        hardware = {
            "version": 1, "title": "Touches", "view": "composition",
            "modules": [{"id": ident, "label": ident} for ident in ("a", "b", "c")],
            "connections": [
                {"id": "ab", "source": "a", "target": "b", "kind": "data"},
                {"id": "ac", "source": "a", "target": "c", "kind": "data"},
            ],
        }
        layout = {
            "version": 1, "canvas": {"width": 300, "height": 220},
            "nodes": {
                "a": {"x": 20, "y": 80, "w": 40, "h": 40},
                "b": {"x": 220, "y": 80, "w": 40, "h": 40},
                "c": {"x": 100, "y": 180, "w": 40, "h": 40},
            },
            "edges": {
                "ab": {"source_side": "right", "target_side": "left"},
                "ac": {"source_side": "right", "target_side": "top", "points": [[100, 100]]},
            },
        }
        interedge = compare_layouts.compare_layouts(hardware, layout, layout)["before"]["metrics"]["interedge"]
        self.assertEqual(interedge["unique_crossings"], 1)  # T touch at (100, 100); shared source is ignored.
        self.assertEqual(interedge["crossings"][0]["point"], [100.0, 100.0])

    def test_invalid_layout_and_cli_preserve_inputs_and_reject_hardlink_report(self):
        hardware, before, after = simple_fixture()
        del after["edges"]["e"]
        report = compare_layouts.compare_layouts(hardware, before, after)
        self.assertEqual(report["status"], "invalid")
        self.assertTrue(report["after"]["validation"]["errors"])

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            hw, old, new, output = (base / name for name in ("hardware.json", "before.json", "after.json", "report.json"))
            hw.write_text(json.dumps(hardware), encoding="utf-8")
            old.write_text(json.dumps(before), encoding="utf-8")
            new.write_text(json.dumps(simple_fixture()[2]), encoding="utf-8")
            originals = [path.read_bytes() for path in (hw, old, new)]
            os.link(old, output)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = compare_layouts.main([str(hw), "--before", str(old), "--after", str(new), "--report", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual([path.read_bytes() for path in (hw, old, new)], originals)

            output.unlink()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = compare_layouts.main([str(hw), "--before", str(old), "--after", str(new), "--report", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "ok")
            self.assertEqual([path.read_bytes() for path in (hw, old, new)], originals)

    def test_malformed_geometry_produces_an_invalid_report_without_crashing(self):
        hardware, before, after = simple_fixture()
        before["nodes"]["a"] = "not a node"
        after["edges"]["e"] = {
            "source_side": "right",
            "target_side": "left",
            "source_pos": "middle",
            "points": [[100]],
        }
        report = compare_layouts.compare_layouts(hardware, before, after)
        self.assertEqual(report["status"], "invalid")
        self.assertTrue(report["before"]["validation"]["errors"])
        self.assertTrue(report["after"]["validation"]["errors"])
        self.assertEqual(report["before"]["metrics"]["paths"]["edge_count"], 0)
        self.assertEqual(report["after"]["metrics"]["paths"]["edge_count"], 0)
        self.assertTrue(all(value is None for value in report["comparison"]["deltas_after_minus_before"].values()))

    def test_malformed_top_level_containers_and_hardware_do_not_crash(self):
        hardware, before, after = simple_fixture()
        before.update({"canvas": None, "nodes": [], "edges": None})
        after.update({"canvas": [], "nodes": None, "edges": []})
        report = compare_layouts.compare_layouts(hardware, before, after)
        self.assertEqual(report["status"], "invalid")
        self.assertIsNone(report["before"]["metrics"]["canvas"]["area"])
        self.assertEqual(report["changes"]["nodes"]["moved"], [])

        bad_hardware = copy.deepcopy(hardware)
        bad_hardware["connections"][0]["source"] = ["a"]
        report = compare_layouts.compare_layouts(bad_hardware, simple_fixture()[1], simple_fixture()[2])
        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["before"]["metrics"]["paths"]["edge_count"], 0)


if __name__ == "__main__":
    unittest.main()

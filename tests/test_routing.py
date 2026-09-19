import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "routing.py"
spec = importlib.util.spec_from_file_location("hardware_routing", SCRIPT)
routing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(routing)


def fixture(y2=80, *, obstacle=False, nested=False):
    modules = [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]
    nodes = {"a": {"x": 20, "y": 80, "w": 60, "h": 40},
             "b": {"x": 220, "y": y2, "w": 60, "h": 40}}
    if obstacle:
        modules.append({"id": "block", "label": "Block"})
        nodes["block"] = {"x": 130, "y": 65, "w": 40, "h": 70}
    if nested:
        modules.insert(0, {"id": "root", "label": "Root"})
        modules[1]["parent"] = "root"
        modules[2]["parent"] = "root"
        nodes["root"] = {"x": 10, "y": 20, "w": 380, "h": 260}
    hardware = {"version": 1, "title": "Test", "view": "microarchitecture", "modules": modules,
                "connections": [{"id": "e", "source": "a", "target": "b", "kind": "data"}]}
    layout = {"version": 1, "canvas": {"width": 400, "height": 300}, "nodes": nodes,
              "edges": {"e": {"source_side": "right", "target_side": "left", "points": [[100, 5]]}}}
    return hardware, layout


def route_points(result, cid="e"):
    edge = result["edges"][cid]
    return [[*routing._anchor(result["nodes"]["a"], edge["source_side"], edge.get("source_pos", .5))]] + edge["points"] + [[*routing._anchor(result["nodes"]["b"], edge["target_side"], edge.get("target_pos", .5))]]


class RoutingTests(unittest.TestCase):
    def test_straight_route_no_bends_and_input_preserved(self):
        hardware, layout = fixture()
        before = copy.deepcopy(layout)
        result, report = routing.route_layout(hardware, layout)
        self.assertEqual(layout, before)
        self.assertEqual(result["edges"]["e"]["points"], [])
        self.assertEqual(report["total_bends"], 0)
        self.assertEqual(result["nodes"], layout["nodes"])
        self.assertEqual(result["version"], layout["version"])

    def test_offset_route_two_bends_and_orthogonal(self):
        hardware, layout = fixture(180)
        result, report = routing.route_layout(hardware, layout)
        self.assertEqual(report["edges"]["e"]["bends"], 2)
        points = route_points(result)
        self.assertTrue(all(x[0] == y[0] or x[1] == y[1] for x, y in zip(points, points[1:])))
        self.assertGreaterEqual(routing.math.dist(points[0], points[1]), 16)
        self.assertGreaterEqual(routing.math.dist(points[-2], points[-1]), 16)

    def test_narrow_straight_gap_stays_direct_but_narrow_offset_fails(self):
        hardware, layout = fixture()
        layout["nodes"]["b"]["x"] = 88  # Eight pixels between boxes.
        result, report = routing.route_layout(hardware, layout)
        self.assertEqual(report["total_bends"], 0)
        self.assertEqual(result["edges"]["e"]["points"], [])
        layout["nodes"]["b"]["y"] = 180
        result, report = routing.route_layout(hardware, layout)
        self.assertIsNone(result)
        self.assertIn("gap too narrow", report["edges"]["e"]["unresolved"])

    def test_obstacle_avoidance_and_nested_container(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                hardware, layout = fixture(obstacle=True, nested=nested)
                if nested:
                    hardware["modules"][-1]["parent"] = "root"
                result, report = routing.route_layout(hardware, layout)
                self.assertIsNotNone(result)
                self.assertGreaterEqual(report["total_bends"], 4)
                block = routing._rect(result["nodes"]["block"])
                points = route_points(result)
                self.assertFalse(any(routing._hits(tuple(a), tuple(b), block) for a, b in zip(points, points[1:])))

    def test_spread_counts_source_and_target_together(self):
        hardware, layout = fixture(70)
        hardware["modules"].append({"id": "c", "label": "C"})
        layout["nodes"]["c"] = {"x": 240, "y": 190, "w": 60, "h": 40}
        hardware["connections"].append({"id": "back", "source": "c", "target": "a", "kind": "control"})
        layout["edges"]["back"] = {"source_side": "left", "target_side": "right"}
        result, report = routing.route_layout(hardware, layout, ports="spread")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(result["edges"]["e"]["source_pos"], 1/3)
        self.assertEqual(result["edges"]["back"]["target_pos"], 2/3)
        self.assertEqual(layout["edges"]["e"].get("source_pos"), None)

    def test_unreachable_returns_no_layout(self):
        hardware, layout = fixture()
        layout["nodes"]["a"] = {"x": 340, "y": 80, "w": 60, "h": 40}
        result, report = routing.route_layout(hardware, layout)
        self.assertIsNone(result)
        self.assertEqual(report["status"], "unresolved")
        self.assertEqual(report["unresolved"], ["e"])

    def test_invalid_and_reproducible(self):
        hardware, layout = fixture(180, obstacle=True)
        first = routing.route_layout(hardware, layout)
        second = routing.route_layout(hardware, layout)
        self.assertEqual(first, second)
        with self.assertRaises(routing.RoutingError):
            routing.route_layout(hardware, layout, clearance=float("nan"))
        with self.assertRaises(routing.RoutingError):
            routing.route_layout(hardware, layout, ports="invalid")
        layout["nodes"]["a"]["w"] = -1
        with self.assertRaises(routing.RoutingError):
            routing.route_layout(hardware, layout)

    def test_cli_does_not_write_unresolved_or_alias_inputs(self):
        hardware, layout = fixture()
        layout["nodes"]["a"] = {"x": 340, "y": 80, "w": 60, "h": 40}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            hw, ly, out, report = (base / name for name in ("hw.json", "layout.json", "out.json", "report.json"))
            hw.write_text(json.dumps(hardware))
            ly.write_text(json.dumps(layout))
            self.assertEqual(routing.main([str(hw), "--layout", str(ly), "--output", str(out), "--report", str(report)]), 1)
            self.assertFalse(out.exists())
            self.assertEqual(json.loads(report.read_text())["unresolved"], ["e"])
            out.write_text("existing output")
            self.assertEqual(routing.main([str(hw), "--layout", str(ly), "--output", str(out)]), 1)
            self.assertEqual(out.read_text(), "existing output")
            original = ly.read_text()
            self.assertEqual(routing.main([str(hw), "--layout", str(ly), "--output", str(out), "--report", str(ly)]), 1)
            self.assertEqual(ly.read_text(), original)


if __name__ == "__main__":
    unittest.main()

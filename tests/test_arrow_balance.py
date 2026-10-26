from copy import deepcopy
import importlib.util
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from test_diagram import diagram, fixture


class ArrowBalanceTests(unittest.TestCase):
    def test_short_arrow_keeps_visible_shaft_after_native_size_conversion(self):
        h,l,s=fixture();l["nodes"]["rf"]["x"]=260
        l["edges"]["writeback"]["points"]=[]
        s["tokens"].update(wide_width=5,font_size=15)
        doc=ET.fromstring(diagram.drawio_xml(h,l,s))
        edge=next(c for c in doc.iter("mxCell") if c.get("id")=="edge:writeback")
        fields=dict(x.split("=",1) for x in edge.get("style").split(";") if "=" in x)
        physical_head=3*float(fields["endSize"])+1
        self.assertLessEqual(physical_head,40*.30)
        self.assertGreater(40-physical_head,20)
        self.assertEqual(edge.get("target"),"node:rf")

    def test_bidirectional_short_line_reserves_shaft_between_two_heads(self):
        h,l,s=fixture();c=h["connections"][0];c["bidirectional"]=True
        l["nodes"]["rf"]["x"]=240;l["edges"]["writeback"]["points"]=[]
        d=diagram.arrow_dimensions(c,l["edges"]["writeback"],s["tokens"],l["nodes"])
        self.assertLessEqual(d["start_length"]+d["end_length"],20*.60)

    def test_redundant_collinear_points_do_not_shrink_marker(self):
        h,l,s=fixture();c=h["connections"][0];e=l["edges"]["writeback"]
        e["points"]=[];before=diagram.arrow_dimensions(c,e,s["tokens"],l["nodes"])
        e["points"]=[[280,170],[349,170],[350,170]]
        self.assertEqual(before,diagram.arrow_dimensions(c,e,s["tokens"],l["nodes"]))

    def test_font_width_and_terminal_segment_each_limit_head_geometry(self):
        h,l,s=fixture();c=h["connections"][0];e=l["edges"]["writeback"]
        s["tokens"].update(wide_width=4,font_size=15,arrow_head_length=50,arrow_head_width_ratio=2)
        e["points"]=[[300,170],[300,240],[340,240],[340,170]]
        d=diagram.arrow_dimensions(c,e,s["tokens"],l["nodes"])
        self.assertEqual(d["end_length"],3)
        self.assertEqual(d["head_width"],8)
        for key,value in [("arrow_head_length",float("nan")),("arrow_head_width_ratio",.5)]:
            bad=deepcopy(s);bad["tokens"][key]=value
            self.assertTrue(diagram.validate_style(bad))

    def test_legend_uses_same_marker_as_bound_edge(self):
        h,l,s=fixture();l["legend"]={"x":20,"y":370,"w":260,"row_height":24,"entries":[{"connection":"writeback","label":"Data"}]}
        doc=ET.fromstring(diagram.drawio_xml(h,l,s));cells={c.get("id"):c for c in doc.iter("mxCell")}
        fields=lambda k:dict(x.split("=",1) for x in cells[k].get("style").split(";") if "=" in x)
        a,b=fields("edge:writeback"),fields("legend-line:0")
        for key in ("width","endWidth","endSize"):self.assertEqual(a[key],b[key])

    def test_audit_flags_title_path_and_alignment_can_clear_it(self):
        path=Path(__file__).resolve().parents[1]/"scripts/audit_layout.py"
        spec=importlib.util.spec_from_file_location("audit_test",path);audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)
        h,l,s=fixture();l["nodes"]["cpu"].update(shape="domain",font_size=14)
        h["modules"][1]["label"]="Execution subsystem"
        l["nodes"]["alu"]["y"]=90;l["nodes"]["rf"]["y"]=90
        l["edges"]["writeback"].update(source_side="top",target_side="top",points=[[160,60],[415,60]])
        result=audit.audit(h,l,s)
        self.assertTrue(any(x["type"]=="estimated_title_collision" for x in result["findings"]))
        self.assertIn("cpu",result["container_space"])
        l["edges"]["writeback"]["points"]=[[160,80],[415,80]]
        self.assertFalse(any(x["type"]=="estimated_title_collision" for x in audit.audit(h,l,s)["findings"]))

    def test_domain_title_alignment_and_bottom_border_are_bound(self):
        h,l,s=fixture();n=l["nodes"]["cpu"];n.update(shape="domain",label_align="right",label_position="bottom")
        doc=ET.fromstring(diagram.drawio_xml(h,l,s));cell=next(c for c in doc.iter("mxCell") if c.get("id")=="title:cpu")
        g=cell.find("mxGeometry")
        self.assertEqual(cell.get("parent"),"node:cpu")
        self.assertGreater(float(g.get("x")),n["w"]/2)
        self.assertAlmostEqual(float(g.get("y"))+float(g.get("height"))/2,n["h"])

    def test_library_and_renderer_accept_same_arrow_parameters(self):
        path=Path(__file__).resolve().parents[1]/"scripts/library.py"
        spec=importlib.util.spec_from_file_location("arrow_library",path);lib=importlib.util.module_from_spec(spec);spec.loader.exec_module(lib)
        profile={"version":1,"name":"arrows","view":"composition","rules":["explicit arrow dimensions"],"sample_ids":[],"tokens":{"arrow_head_length":12,"arrow_head_width_ratio":1.8}}
        lib.validate_profile(profile)
        self.assertEqual(diagram.validate_style(profile),[])
        for value in [0,True,float("nan"),.5]:
            bad=deepcopy(profile);bad["tokens"]["arrow_head_width_ratio"]=value
            with self.assertRaises(ValueError):lib.validate_profile(bad)

    def test_audit_will_not_overwrite_source_or_hardlink(self):
        path=Path(__file__).resolve().parents[1]/"scripts/audit_layout.py"
        spec=importlib.util.spec_from_file_location("audit_cli",path);audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)
        with tempfile.TemporaryDirectory() as tmp:
            import json,contextlib,io,os
            files=[Path(tmp)/name for name in ["h.json","l.json","s.json"]]
            for f,value in zip(files,fixture()):f.write_text(json.dumps(value))
            alias=Path(tmp)/"alias.json";os.link(files[0],alias)
            before=files[0].read_bytes()
            with contextlib.redirect_stderr(io.StringIO()):
                rc=audit.main([str(files[0]),"--layout",str(files[1]),"--style",str(files[2]),"--report",str(alias)])
            self.assertEqual(rc,1);self.assertEqual(files[0].read_bytes(),before)


if __name__=="__main__":unittest.main()

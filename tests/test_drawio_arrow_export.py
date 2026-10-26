"""Opt-in actual Desktop regression: catches marker-unit interpretation bugs."""
import os
from pathlib import Path
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET

from test_diagram import diagram


@unittest.skipUnless(os.environ.get("HARDWARE_DIAGRAM_EXPORT_TESTS")=="1", "set HARDWARE_DIAGRAM_EXPORT_TESTS=1 to run draw.io Desktop")
class DesktopArrowTests(unittest.TestCase):
    def test_exported_short_and_bidirectional_arrows_reserve_real_shaft(self):
        self.assertIsNotNone(diagram._drawio_binary(None),"draw.io Desktop is required for this opt-in check")
        h={"version":1,"title":"Arrow end clearance","view":"microarchitecture","modules":[],"connections":[]}
        l={"version":1,"canvas":{"width":600,"height":650},"nodes":{},"edges":{}}
        cases=[(gap,bi) for bi in [False,True] for gap in [20,40,80,160]]
        for i,(gap,bi) in enumerate(cases):
            a,b=f"a{i}",f"b{i}";cid=f"e{i}"
            h["modules"].extend([{"id":a,"label":"Source"},{"id":b,"label":"Target"}])
            h["connections"].append({"id":cid,"source":a,"target":b,"kind":"data","bidirectional":bi})
            l["nodes"][a]={"x":20,"y":25+i*75,"w":140,"h":40}
            l["nodes"][b]={"x":160+gap,"y":25+i*75,"w":120,"h":40}
            l["edges"][cid]={"source_side":"right","target_side":"left","wide":True,"points":[]}
        s={"version":1,"name":"export-check","view":"microarchitecture","tokens":{"wide_width":5,"font_size":15,"edge_colors":{"data":"#000000"}}}
        with tempfile.TemporaryDirectory() as tmp:
            prefix=Path(tmp)/"arrows";result=diagram.render(h,l,s,prefix)
            self.assertTrue(result["exports_complete"],result["issues"])
            paths=[x.get("d") for x in ET.parse(prefix.with_suffix(".svg")).getroot().iter()
                   if x.tag.endswith("path") and x.get("fill")=="#000000" and "Z" in x.get("d","")]
            self.assertEqual(len(paths),len(cases))
            for path,(gap,bi) in zip(paths,cases):
                numbers=list(map(float,re.findall(r"-?\d+(?:\.\d+)?",path)))
                xs=sorted(set(numbers[::2]));end=xs[-1]-xs[-2]
                self.assertLessEqual(end,gap*.30+.02,(gap,bi,path))
                if bi:
                    start=xs[1]-xs[0]
                    self.assertLessEqual(start+end,gap*.60+.02,(gap,bi,path))
                self.assertGreater(xs[-1]-xs[0]-end-(xs[1]-xs[0] if bi else 0),gap*.30)


if __name__=="__main__":unittest.main()

import unittest
from io import BytesIO
import struct
from zipfile import ZipFile

from flame2resolve import archives, drt, flame


class ConversionTests(unittest.TestCase):
    def test_common_parent_across_sequences(self):
        cuts = [
            flame.Cut("a", 0, 6, 0, "/loc/a/b/c/file.exr"),
            flame.Cut("b", 0, 6, 0, "/loc/a/b/d/file2.exr"),
            flame.Cut("c", 0, 6, 0, "/Volumes/other/project/file3.exr"),
        ]
        plans = [flame.TimelinePlan(cut.name, 25, 1920, 1080, [cut]) for cut in cuts]
        self.assertEqual(archives.source_bases(plans), ["/Volumes/other/project/", "/loc/a/b/"])

    def test_remapping_prefers_deepest_complete_prefix(self):
        rules = archives.mappings([{"source": "/loc/a", "target": "/first/"},
                                   {"source": "/loc/a/b/c", "target": "/second"}])
        self.assertEqual(archives.remap("/loc/a/b/c/file.exr", rules), ("/second/file.exr", True))
        self.assertEqual(archives.remap("/loc/a/b/copy/file.exr", rules), ("/first/b/copy/file.exr", True))
        self.assertEqual(archives.remap("/loc/ab/file.exr", rules), ("/loc/ab/file.exr", False))

    def test_preserve_crop_geometry_using_fit(self):
        cut = flame.Cut("portrait", 0, 15, 0, source_width=4320, source_height=2880,
                        settings={"Scaling": 3, "Pan": -219.375, "ResizeFilter": 9})
        plan = flame.TimelinePlan("Portrait", 24, 1440, 1800, [cut])
        fit = archives.framing(plan, cut, "fit")
        self.assertEqual(fit["Scaling"], 2)
        self.assertAlmostEqual(fit["ZoomX"], 1.875)
        self.assertAlmostEqual(fit["ZoomY"], 1.875)
        self.assertEqual(fit["Pan"], -219.375)
        self.assertEqual(fit["ResizeFilter"], 9)

    def test_export_writes_format_and_path_in_native_entries(self):
        source = "/old/shot.[1001-1020].exr"
        target = "/new & relocated/shot.[1001-1020].exr"
        cut = flame.Cut("shot", 0, 10, 2, source=source, issues=["Review this cut."],
                        source_width=3840, source_height=2160)
        plan = flame.TimelinePlan("Portrait", 25, 1080, 1920, [cut], start=90000)
        with ZipFile(BytesIO(drt.export(plan, [target], [{"Scaling": 3}]))) as archive:
            sequence = drt.xml(archive.read(drt.TIMELINE_POOL)).find(".//Sm2Sequence")
            config = dict((key, value) for key, _, _, value in
                          drt.qt_fields(bytes.fromhex(sequence.findtext("FieldsBlob"))))
            self.assertEqual(struct.unpack_from(">QQ", config["SequenceSetup"], 40), (1080, 1920))
            self.assertEqual(struct.unpack_from(">Q", config["SequenceSetup"], 76), (25,))
            name = next(name for name in archive.namelist() if name.startswith("SeqContainer/"))
            clip = drt.xml(archive.read(name)).find(".//Sm2TiVideoClip")
            self.assertEqual(clip.findtext("Start"), "90000")
            self.assertEqual(clip.findtext("MediaFilePath"), target)
            pool = drt.xml(archive.read(drt.TIMELINE_POOL))
            identity = drt.open_blob(bytes.fromhex(pool.findtext(".//BtVideoInfo/Clip")))
            self.assertTrue(any(key == 1 and value == b"/new & relocated"
                                for key, _, value, _ in drt.fields(identity)))
            project = drt.xml(archive.read("project.xml"))
            marker = project.find(".//Sm2SequenceLockableBlob/FieldsBlob")
            blob, = list(drt.qt_fields(bytes.fromhex(marker.text)))
            self.assertIn(b"Review this cut.", drt.open_blob(blob[3]))


if __name__ == "__main__":
    unittest.main()

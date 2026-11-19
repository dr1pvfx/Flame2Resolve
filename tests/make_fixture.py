"""Generate a synthetic byte-layout fixture for Flame2Resolve."""

import argparse
import gzip
from io import BytesIO
from pathlib import Path
import struct
from tarfile import TarInfo
import tarfile
import zlib


def record(tag, body):
    return bytes.fromhex(tag) + struct.pack(">I", len(body)) + body


def tar_member(path, contents):
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = TarInfo(path)
        info.size = len(contents)
        archive.addfile(info, BytesIO(contents))
    return gzip.compress(stream.getvalue(), mtime=0)


def clip_record(name, index, duration, trim, source_out, extras=b""):
    name_bytes = name.encode()
    size = len(name_bytes)
    clip = bytearray(174 + size)
    clip[:4] = bytes.fromhex("839cb1f4")
    struct.pack_into(">H", clip, 70 - 8, size)
    clip[72-8:72-8+size] = name_bytes
    struct.pack_into(">d", clip, 81 - 8 + size, duration)
    struct.pack_into(">dd", clip, 126 - 8 + size, trim, source_out)
    clip[170-8+size:182-8+size] = index.to_bytes(12, "big")
    return record("88aeb6b6", clip + extras)


def source_records(name, index):
    source_key = index.to_bytes(12, "big")
    xml_key = (0x1234 + index).to_bytes(16, "big")
    storage = "<storageFormat><width>2560</width><height>1440</height><colourSpace>ACEScg</colourSpace></storageFormat>"
    feeds = "".join(f'<feed vuid="{version}">{storage}<spans><span><path>'
                    f'/old/demo/renders/{name}/{name}_{version}.[1001-1040].exr'
                    f'</path></span></spans></feed>' for version in ("v003", "v004"))
    xml = (f'<?xml version="1.0"?><clip><handler><name>MIO Clip</name></handler>'
           f'<GATEWAY_NODE_ID>/old/demo/{name}.clip@CLIP(fixture)</GATEWAY_NODE_ID>'
           f'<tracks><track><feeds currentVersion="v003">{feeds}</feeds></track></tracks></clip>').encode()
    return (record("82b6acb1", bytes(40) + source_key + xml_key) +
            b"\x10" + xml_key + struct.pack(">I", len(xml)+1) + xml + b"\x00")


def make_fixture(header):
    sidecar = b"h00000001_00000002_00000003"
    sidecar_name = b"pure_side_car_setup.resize_node"
    effect_ref = b"MoClipSideSetup\x00\x1b" + sidecar + b"\x00" + sidecar_name
    timewarp = (b"<Setup><Base><Version>1</Version><NAME>Timewarp</NAME></Base><State>"
                b"<TW_RetimerMode>0</TW_RetimerMode><TW_FrameMode>2</TW_FrameMode>"
                b"<TW_Speed><Channel><Value>125</Value></Channel></TW_Speed>"
                b"<TW_AnchorOffset>10</TW_AnchorOffset></State></Setup>")
    cut_a = clip_record("alpha", 1, 12, 3, 15, effect_ref)
    cut_b = clip_record("beta", 2, 8, 0, 10, struct.pack(">I", len(timewarp)+1) + timewarp + b"\x00")
    track_base = bytes(44) + bytes([25]) + struct.pack(">d", 20)
    track = record("88a2fdca", record("8193a2ae", record("819fd5a3",
                   bytes.fromhex("81a0cfac") + track_base + cut_a + cut_b)))
    name = b"Demo Sequence"
    sequence = bytearray(64)
    for start, tag in ((0, "88acc2a4"), (8, "84898df6"), (16, "88a4f1dc"),
                       (24, "81a0cfac"), (32, "808fd4d4")):
        sequence[start:start+4] = bytes.fromhex(tag)
    sequence[55:57] = b"\x08\x50"
    struct.pack_into(">H", sequence, 62, len(name))
    sequence += name + struct.pack(">dB", 90000, 25) + track
    struct.pack_into(">I", sequence, 4, len(sequence)-8)
    data = source_records("alpha", 1) + source_records("beta", 2) + sequence
    header.parent.mkdir(parents=True, exist_ok=True)
    with header.open("xb") as file:
        file.write(bytes.fromhex("8082a7d6") + bytes(0x800-4) + zlib.compress(data))
    resize = (b"<Setup><Base><NAME>Resize</NAME></Base><State>"
              b"<DestinationWidth>1920</DestinationWidth><DestinationHeight>1080</DestinationHeight>"
              b"<SavedWidth>2560</SavedWidth><SavedHeight>1440</SavedHeight>"
              b"<ResizeType>2</ResizeType><ResizeQuality>4</ResizeQuality>"
              b"<ResizePreserveAspect>True</ResizePreserveAspect>"
              b"<ResizeBackColourRed>0</ResizeBackColourRed><ResizeBackColourGreen>0</ResizeBackColourGreen>"
              b"<ResizeBackColourBlue>0</ResizeBackColourBlue><ResizeBackColourAlpha>0</ResizeBackColourAlpha>"
              b"</State></Setup>")
    segment = header.with_name(header.name+"_1.seg")
    with segment.open("xb") as file:
        file.write(tar_member("catalog", b"Synthetic fixture"))
        file.write(tar_member(f"{sidecar.decode()}/{sidecar_name.decode()}", resize))
    return header, segment


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("header", type=Path)
    args = parser.parse_args()
    for path in make_fixture(args.header):
        print(path)

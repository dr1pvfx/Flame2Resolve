"""Write native Resolve 21 DRTs from a small, scrubbed Resolve timeline shell."""

from copy import deepcopy
import ctypes
import math
from ctypes.util import find_library
from io import BytesIO
import os
from pathlib import Path, PurePosixPath
import re
import struct
import sys
from uuid import uuid4
from zipfile import ZipFile, ZIP_DEFLATED
import xml.etree.ElementTree as ET

from . import flame

TEMPLATES = Path(__file__).with_name("templates")
XML_HEADER = b'<?xml version="1.0" encoding="UTF-8"?>\n<!--DbAppVer="21.0.4.0005" DbPrjVer="17"-->\n'
TAG_NAMESPACE = re.compile(rb"(<\/?[A-Za-z]\w*)(::|__NAMESPACE__)(?=[A-Za-z])")
TIMELINE_POOL = "MediaPool/Master/MpFolder.xml"
UUID = re.compile(r"[\da-f]{8}(?:-[\da-f]{4}){3}-[\da-f]{12}")
def load_zstd():
    override = os.environ.get("FLAME2RESOLVE_ZSTD")
    if override:
        return ctypes.CDLL(override)
    candidates = [find_library("zstd")]
    if sys.platform == "darwin":
        candidates.extend(("/opt/homebrew/opt/zstd/lib/libzstd.dylib",
                           "/usr/local/opt/zstd/lib/libzstd.dylib", "libzstd.dylib"))
    else:
        candidates.append("libzstd.so.1")
    for candidate in filter(None, candidates):
        try:
            return ctypes.CDLL(candidate)
        except OSError:
            continue
    raise RuntimeError("Install libzstd or set FLAME2RESOLVE_ZSTD to its shared library path.")


LIB = load_zstd()
LIB.ZSTD_decompress.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t)
LIB.ZSTD_decompress.restype = ctypes.c_size_t
LIB.ZSTD_compress.argtypes = (*LIB.ZSTD_decompress.argtypes, ctypes.c_int)
LIB.ZSTD_compress.restype = ctypes.c_size_t
LIB.ZSTD_compressBound.argtypes = (ctypes.c_size_t,)
LIB.ZSTD_compressBound.restype = ctypes.c_size_t


def zstd(data, compress=False):
    limit = LIB.ZSTD_compressBound(len(data)) if compress else 2 << 20
    output = ctypes.create_string_buffer(limit)
    if compress:
        length = LIB.ZSTD_compress(output, limit, data, len(data), 3)
    else:
        length = LIB.ZSTD_decompress(output, limit, data, len(data))
    if length > limit:
        raise ValueError("Invalid Resolve binary data.")
    return output.raw[:length]


def xml(data):
    return ET.fromstring(TAG_NAMESPACE.sub(rb"\1__NAMESPACE__", data))


def xml_bytes(root):
    return XML_HEADER + TAG_NAMESPACE.sub(rb"\1::", ET.tostring(root, encoding="utf-8"))


def uid():
    return str(uuid4())


def enveloped(version, data, compressed=False):
    body = b"\x81" + zstd(data, True) if compressed else b"\x80" + data
    return struct.pack(">II", version, len(body)) + body


def open_blob(blob):
    if blob[8] == 0x80:
        return blob[9:]
    if blob[8] == 0x81:
        return zstd(blob[9:])
    raise ValueError("Unexpected Resolve blob format.")


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def read_varint(data, offset):
    result = 0
    for bit in range(0, 70, 7):
        value = data[offset]
        result |= (value & 127) << bit
        offset += 1
        if value < 128:
            return result, offset
    raise ValueError("Invalid Resolve integer.")


def pb(field, value, wire=None):
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, int):
        return varint(field << 3) + varint(value)
    if wire in (1, 5):
        return varint((field << 3) | wire) + value
    return varint((field << 3) | 2) + varint(len(value)) + value


def fields(data):
    offset = 0
    while offset < len(data):
        start = offset
        tag, offset = read_varint(data, offset)
        field, wire = divmod(tag, 8)
        if wire == 0:
            value, offset = read_varint(data, offset)
        else:
            if wire == 2:
                size, offset = read_varint(data, offset)
            else:
                size = {1: 8, 5: 4}[wire]
            value = data[offset:offset+size]
            offset += size
        yield field, wire, value, data[start:offset]


def qt(entries):
    body = bytearray(struct.pack(">II", 1, len(entries)))
    for key, kind, flags, value in entries:
        key = key.encode("utf-16be")
        body += struct.pack(">I", len(key)) + key + struct.pack(">IB", kind, flags)
        if kind in (10, 12):
            body += struct.pack(">i", -1 if value is None else len(value))
        if value is not None:
            body += value
    return bytes(body)


def qt_fields(blob):
    count, = struct.unpack_from(">I", blob, 4)
    offset = 8
    for _ in range(count):
        size, = struct.unpack_from(">I", blob, offset)
        key = blob[offset+4:offset+4+size].decode("utf-16be")
        offset += size + 4
        kind, flags = struct.unpack_from(">IB", blob, offset)
        offset += 5
        if kind in (10, 12):
            size, = struct.unpack_from(">i", blob, offset)
            offset += 4
        else:
            size = {0:0, 1:1, 2:4, 3:4, 4:8, 5:8, 6:8}[kind]
        value = blob[offset:offset+size] if size >= 0 else None
        offset += max(size, 0)
        yield key, kind, flags, value
    if offset != len(blob):
        raise ValueError("Unsupported Resolve dictionary.")


def qt_set(node, replacements, remove=()):
    original = qt_fields(bytes.fromhex(node.text))
    node.text = qt([(key, kind, flags, replacements.get(key, value))
                    for key, kind, flags, value in original if key not in remove]).hex()


def set_text(node, values):
    for key, value in values.items():
        node.find(key).text = str(value) if value is not None else None


def fresh_ids(*roots):
    # Resolve references shared IDs across XML entries. Rewrite them together.
    ids = {}
    for root in roots:
        for node in root.iter():
            if "DbId" in node.attrib:
                previous = node.get("DbId")
                node.set("DbId", ids.setdefault(previous, uid()))
            if not node.text:
                continue
            if UUID.fullmatch(node.text):
                node.text = ids.setdefault(node.text, uid())
            elif node.tag == "FieldsBlob" and node.text.startswith("00000001"):
                blob = bytes.fromhex(node.text)
                try:
                    entries = list(qt_fields(blob))
                except (ValueError, KeyError, struct.error):
                    continue
                replacements = {}
                for key, kind, _, val in entries:
                    if kind == 10 and val and len(val) == 72:
                        if (string := val.decode("utf-16be")) and UUID.fullmatch(string):
                            replacements[key] = ids.setdefault(string, uid()).encode("utf-16be")
                if replacements:
                    qt_set(node, replacements)
    return ids


def rate_blob(rate):
    return struct.pack("<dd", rate, 0)


def size_blob(width, height):
    return struct.pack(">QQ", width, height)


def format_tc(frames, rate):
    frames = max(0, frames)
    fps = round(rate)
    seconds, frame = divmod(frames, fps)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour % 24:02}:{minute:02}:{second:02}:{frame:02}"


def effect_part(parameter, value):
    data = pb(2, struct.pack("<d", value), 1) if isinstance(value, float) else pb(4, value)
    return pb(9, pb(1, parameter) + pb(3, pb(1, data)))


def effects(properties):
    parts = b""
    transforms = {"Pan": 40, "Tilt": 41, "ZoomX": 42, "ZoomY": 43, "RotationAngle":44}
    if any(key in properties for key in transforms):
        parts += pb(1, pb(1,4) + b"".join(
            effect_part(transforms[key], float(value)) for key, value in properties.items() if key in transforms))
    if any(key in properties for key in ("Scaling", "RetimeProcess", "ResizeFilter")):
        parts += pb(1, pb(1,44) + pb(9,b"") +
                    b"".join(effect_part(number, properties[key]) if key in properties else pb(9,b"")
                             for key, number in (("RetimeProcess",91), ("MotionEstimation",90),
                                                 ("Scaling",92), ("ResizeFilter",93))) + pb(9,b""))
    return enveloped(2, parts).hex() if parts else None


def marker_blob(plan):
    entries = []
    for cut in reversed(plan.cuts):
        if cut.issues:
            note = "\n".join([f"Archived source {cut.name} {cut.version}", *cut.issues])
            body = pb(1, 16) + pb(3, note) + pb(3, str(cut.duration)) + pb(3, f"Review {cut.name}")
            inner = pb(1, body)
            wrapped = struct.pack(">II", 2, len(inner)) + inner
            entries.append(pb(1, pb(1, cut.record) + pb(2, wrapped)))
    native = enveloped(10001, pb(2, b"".join(entries)), True)
    return qt([("BlobData", 12, 0, native)]).hex()


def clip_identity(path):
    name = PurePosixPath(path).name
    parent = str(PurePosixPath(path).parent)
    match = flame.FRAME_RANGE.search(name)
    fps_digits = len(match[1])
    pattern = name[:match.start()] + f"%0{fps_digits}u" + name[match.end():]
    return enveloped(2, pb(1,parent) + pb(2,name) + pb(6,pattern) + pb(7,uid()) +
                     pb(9,"") + pb(10,"") + pb(15,4) + pb(16,100) + pb(18,8238))


def media_element(donor, cut, fps, path):
    element = deepcopy(donor)
    clip = element[0]
    fresh_ids(clip)
    name = PurePosixPath(path).name
    set_text(clip, {"Name": name})
    info = clip.find("./Video/BtVideoInfo")
    first, last = map(int, flame.FRAME_RANGE.search(name).groups())
    rate = cut.source_rate or fps
    ticks = cut.source_start_tc if cut.source_start_tc is not None else first
    set_text(info, {"Clip": clip_identity(path).hex()})
    qt_set(info.find("Time"), {"Timecode": format_tc(ticks, rate).encode("utf-16be"),
                                "StartFrame": struct.pack(">I", first),
                                "NumFrames": struct.pack(">I", last-first+1), "FrameRate": rate_blob(rate)})
    qt_set(info.find("Geometry"), {"Resolution": size_blob(cut.source_width, cut.source_height)})
    return element, rate, ticks, last-first+1


def project_setup(project, plan):
    setup = project.find("CommonConfig/SM_Config/FieldsBlob")
    values = dict((key,value) for key, _, _, value in qt_fields(bytes.fromhex(setup.text)))
    body = open_blob(values["SetupBA"])
    actual = plan.width, plan.height
    result = bytearray(pb(2,plan.width*2) + pb(3,plan.height*2) + pb(15,plan.fps*2))
    for key, _, value, packed in fields(body):
        if key in (2, 3, 15):
            continue
        if key == 235:
            old_width, old_height = (1920,1080)
            old = size_blob(old_width,old_height)
            if old not in value:
                old = size_blob(1440,1800)
            value = value.replace(old, size_blob(*actual))
            packed = pb(key,value)
        result += packed
    qt_set(setup, {"SetupBA": enveloped(1, bytes(result), True)})


def time_map(cut, fps, source_rate, frame_count):
    if cut.retime is None:
        end = (frame_count-1) / source_rate
        return cut.source_in, (b"\x02" + struct.pack(">d", end)).hex()
    speed = cut.retime.speed / 100
    source_end = (frame_count-1) / source_rate
    record_end = (frame_count/source_rate/speed) - (1/fps)
    source_offset = round(cut.retime.anchor) / source_rate
    source_trim = math.floor(source_offset / speed * fps)
    endpoint = pb(1, struct.pack("<d", record_end), 1) + pb(2, struct.pack("<d", record_end * speed), 1)
    keyframes = pb(160, 0) + pb(1, endpoint)
    entries = [
        ("YMax", 6, 0, struct.pack(">d",source_end)),
        ("XMax", 6, 0, struct.pack(">d",record_end)),
        ("UniqueId", 10, 0, uid().encode("utf-16be")),
        ("LastValidYOffset", 6, 0, struct.pack(">d",source_end)),
        ("KeyframesBA", 12, 0, keyframes),
        ("DbType", 10, 0, "Sm2TimeMap".encode("utf-16be")),
    ]
    return source_trim, qt(entries).hex()


def export(plan, paths, settings):
    project, pool, container = [xml((TEMPLATES / name).read_bytes())
                                for name in ("project.xml", "pool.xml", "sequence.xml")]
    fresh_ids(project, pool, container)
    movie_vec = pool.find("MediaVec")
    movie_entries = [node for node in movie_vec if node.find("Sm2MpVideoClip") is not None]
    media_donor = movie_entries[0]
    for node in movie_entries:
        movie_vec.remove(node)
    timeline = movie_vec.find("./Element/Sm2MpTimelineClip")
    set_text(timeline, {"Name": plan.name, "CurPlayheadPosition": plan.start})
    seq = timeline.find(".//Sm2Sequence")
    duration = max(cut.record + cut.duration for cut in plan.cuts)
    set_text(seq, {"FrameRate": rate_blob(plan.fps).hex(), "Resolution": size_blob(plan.width,plan.height).hex(),
                   "MediaExtents": struct.pack("<dd", plan.start/plan.fps, duration/plan.fps).hex()})
    sequence_config = seq.find("FieldsBlob")
    setup, = [value for key, _, _, value in qt_fields(bytes.fromhex(sequence_config.text))
              if key == "SequenceSetup"]
    setup = bytearray(setup)
    struct.pack_into(">QQ", setup, 40, plan.width, plan.height)
    struct.pack_into(">Q", setup, 76, plan.fps)
    struct.pack_into(">QQ", setup, 228, plan.width, plan.height)
    qt_set(sequence_config, {"SequenceSetup": bytes(setup)})
    project.find("ProjectName").text = plan.name
    project_setup(project, plan)
    marker = project.find(".//Sm2SequenceLockableBlob")
    marker.find("FieldsBlob").text = marker_blob(plan)
    marker.find("BlobOwner").text = seq.get("DbId")

    items = container.find("./VideoTrackVec/Element/Sm2TiTrack/Items")
    clip_donor = items[0]
    items.clear()
    media_by_path = {}
    for cut, path, props in zip(plan.cuts, paths, settings, strict=True):
        if path not in media_by_path:
            media, rate, ticks, frames = media_element(media_donor, cut, plan.fps, path)
            movie_vec.append(media)
            media_by_path[path] = (media[0].get("DbId"), rate, ticks, frames)
        ref, rate, ticks, frames = media_by_path[path]
        element = deepcopy(clip_donor)
        clip = element[0]
        fresh_ids(clip)
        if cut.retime:
            props = {**props, "RetimeProcess":2}
        resolve_effects = effects(props)
        trim, media_time = time_map(cut, plan.fps, rate, frames)
        set_text(clip, {"Name": PurePosixPath(path).name, "Start": cut.record + plan.start,
                        "Duration": cut.duration, "In": trim, "MediaTimemapBA": media_time,
                        "MediaRef": ref, "MediaFilePath": path,
                        "MediaStartTime": ticks/rate, "MediaFrameRate": rate_blob(rate).hex(),
                        "EffectFiltersBA": resolve_effects})
        items.append(element)
    output = BytesIO()
    with ZipFile(output,"w",ZIP_DEFLATED) as archive:
        archive.writestr("project.xml", xml_bytes(project))
        archive.writestr(TIMELINE_POOL, xml_bytes(pool))
        archive.writestr(f"SeqContainer/{container.get('DbId')}.xml", xml_bytes(container))
    return output.getvalue()

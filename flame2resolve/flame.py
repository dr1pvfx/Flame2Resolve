"""Read Flame archive metadata, source identities, and Timeline FX sidecars."""

from dataclasses import dataclass, field
import gzip
import math
from pathlib import PurePosixPath
import re
import struct
import tarfile
import xml.etree.ElementTree as ET
import zlib


MAX_METADATA = 64 << 20
MAX_SETUP = 1 << 20
MAX_SEGMENT_SCAN = 256 << 20
OPENCLIP = re.compile(rb'<\?xml[^?]{0,90}\?><clip\b.*?</clip>', re.S)
TIMEWARP = re.compile(rb'<Setup><Base><Version>[^<>]+</Version><NAME>Timewarp</NAME>')
SIDECAR = re.compile(
    rb'MoClipSideSetup\x00\x1b(h[0-9a-f]{8}(?:_[0-9a-f]{8}){2})'
    rb'.{0,200}?(pure_side_car_setup\.[a-zA-Z0-9_]+)', re.S
)
FRAME_RANGE = re.compile(r'\[(\d+)-(\d+)\](?=\.[^/]+$)')
RESIZE_TYPE = {"0": 1, "1": 2, "2": 3, "3": 4}
RESIZE_FILTER = {"0": 3, "1": 11, "2": 4, "3": 12, "4": 10, "8": 13, "9": 9}


@dataclass
class Retime:
    speed: float
    anchor: float


@dataclass
class Cut:
    name: str
    record: int
    duration: int
    source_in: int
    source: str = ""
    version: str = ""
    color: str = ""
    settings: dict = field(default_factory=dict)
    issues: list = field(default_factory=list)
    retime: Retime | None = None
    source_width: int = 0
    source_height: int = 0
    source_rate: float = 0
    source_start_tc: int | None = None


@dataclass
class TimelinePlan:
    name: str
    fps: int
    width: int
    height: int
    cuts: list[Cut]
    start: int = 0


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unsigned(data, offset, size=4):
    require(0 <= offset <= len(data) - size, "Truncated archive field")
    return int.from_bytes(data[offset:offset + size], "big")


def archive_metadata(path):
    with path.open("rb") as stream:
        require(stream.read(4) == bytes.fromhex("8082a7d6"), "Unrecognized archive header")
        stream.seek(0x800)
        decoder = zlib.decompressobj()
        result = bytearray()
        while not decoder.eof:
            chunk = stream.read(256 << 10)
            require(chunk, "Archive metadata is incomplete")
            result.extend(decoder.decompress(chunk, MAX_METADATA + 1 - len(result)))
            require(len(result) <= MAX_METADATA, "Archive metadata exceeds the read limit")
    return bytes(result)


def xml_payload(data):
    require(len(data) <= MAX_SETUP, "Archive XML exceeds the read limit")
    text = data.decode("utf-8-sig")
    require("<!DOCTYPE" not in text and "<!ENTITY" not in text, "Unsupported archive XML")
    return ET.fromstring(text)


def timewarp_payloads(data):
    for match in TIMEWARP.finditer(data):
        offset = match.start()
        end = data.find(b"</Setup>", offset, offset + MAX_SETUP)
        require(end >= 0, f"Truncated Timewarp at {offset:#x}")
        payload = data[offset:end + 8]
        require(unsigned(data, offset - 4) in (len(payload), len(payload) + 1),
                f"Bad Timewarp length at {offset:#x}")
        state = xml_payload(payload).find("State")
        require(state is not None, f"Missing Timewarp state at {offset:#x}")
        yield offset, state


def timewarp_summary(state):
    channels = {}
    for name in ("Speed", "SpeedTiming", "Timing", "DurationTiming"):
        channel = state.find(f"TW_{name}/Channel")
        if channel is not None:
            channels[name] = {
                "value": channel.findtext("Value"),
                "extrapolation": channel.findtext("Extrap"),
                "keys": [{child.tag: child.text for child in key}
                         for key in channel.findall("./KFrames/Key")],
            }
    return {
        "retimer_mode": state.findtext("TW_RetimerMode"),
        "frame_mode": state.findtext("TW_FrameMode"),
        "anchor_offset": state.findtext("TW_AnchorOffset"),
        "channels": channels,
    }


def openclips(data):
    result = {}
    for match in OPENCLIP.finditer(data):
        offset = match.start()
        payload = match.group()
        if data[offset - 21:offset - 20] == b"\x10" and unsigned(data, offset - 4) == len(payload) + 1:
            key = data[offset - 20:offset - 4]
            require(key not in result or result[key] == payload, "Conflicting OpenClip identities")
            result[key] = payload
    return result


def read_cut(data, offset, record, clips, source_records=None):
    require(data[offset:offset + 4] == bytes.fromhex("88aeb6b6") and
            data[offset + 8:offset + 12] == bytes.fromhex("839cb1f4"), "Unexpected cut record")
    end = offset + 8 + unsigned(data, offset + 4)
    name_size = unsigned(data, offset + 70, 2)
    require(offset + 182 + name_size <= end <= len(data), "Malformed cut record")
    name = data[offset + 72:offset + 72 + name_size].decode()
    duration, = struct.unpack_from(">d", data, offset + 81 + name_size)
    source_in, source_out = struct.unpack_from(">dd", data, offset + 126 + name_size)
    require(duration.is_integer() and duration > 0 and source_in.is_integer() and
            source_out.is_integer() and source_out > source_in, f"Unsupported cut timing for {name}")
    ref = data[offset + 170 + name_size:offset + 182 + name_size]
    records = []
    if source_records is not None:
        records = source_records.get(ref, [])
    else:
        for match in re.finditer(re.escape(ref), data):
            at = match.start() - 48
            if at >= 0 and data[at:at + 4] == bytes.fromhex("82b6acb1"):
                records.append(data[at:at + 8 + unsigned(data, at + 4)])
    require(len(records) == 1, f"Ambiguous archive source for {name}")
    matches = [xml_payload(value) for key, value in clips.items() if key in records[0]]
    require(len(matches) == 1 and matches[0].findtext("./handler/name") == "MIO Clip",
            f"Expected one archived OpenClip for {name}")
    source = matches[0]
    require(any(f"/{name}.clip@CLIP(" in (item.text or "") for item in source.iter("GATEWAY_NODE_ID")),
            f"OpenClip identity does not match {name}")
    feeds = source.find("./tracks/track/feeds")
    require(feeds is not None, f"Missing video feeds for {name}")
    version = feeds.get("currentVersion")
    versions = [item for item in feeds.findall("feed") if item.get("vuid") == version]
    require(len(versions) == 1, f"Ambiguous saved version for {name}")
    feed = versions[0]
    paths = feed.findall("./spans/span/path")
    require(len(paths) == 1 and paths[0].text, f"Unsupported compound source for {name}")
    color = (feed.findtext("./storageFormat/colourSpace")
             or feed.findtext("./storageFormat/metadataCS", ""))
    cut = Cut(name, record, int(duration), int(source_in), paths[0].text, version, color)
    cut.source_width = int(feed.findtext("./storageFormat/width", "0"))
    cut.source_height = int(feed.findtext("./storageFormat/height", "0"))
    rate = feed.find("./startTimecode/rate")
    if rate is not None and rate.findtext("denominator", "0") != "0":
        cut.source_rate = int(rate.findtext("numerator", "0")) / int(rate.findtext("denominator"))
    ticks = feed.findtext("./startTimecode/nbTicks")
    if ticks is not None:
        cut.source_start_tc = int(ticks)
    return cut, int(source_out)


class BoundedReader:
    def __init__(self, stream, limit):
        self.stream = stream
        self.limit = limit

    def read(self, size):
        if not 0 <= size <= self.limit:
            raise ValueError("Archive effect segment reached the read limit")
        data = self.stream.read(size)
        self.limit -= len(data)
        return data


def sidecar_setups(path, wanted):
    """Read the first two gzip tar members. Never unpack to disk."""
    if not wanted or not path.is_file():
        return {}
    result = {}
    with path.open("rb") as stream:
        decoder = zlib.decompressobj(31)
        remaining = MAX_METADATA
        while not decoder.eof and stream.tell() < 16 << 20:
            chunk = stream.read(256 << 10)
            if not chunk or not remaining:
                return {}
            remaining -= len(decoder.decompress(chunk, remaining))
        if not decoder.eof:
            return {}
        stream.seek(stream.tell() - len(decoder.unused_data))
        with gzip.GzipFile(fileobj=stream) as inflated, tarfile.open(
                fileobj=BoundedReader(inflated, MAX_SEGMENT_SCAN), mode="r|") as tar:
            for entry in tar:
                parts = PurePosixPath(entry.name).parts
                if parts not in wanted or not entry.isfile() or entry.size > MAX_SETUP:
                    continue
                require(parts not in result, f"Duplicate effect setup {entry.name}")
                result[parts] = xml_payload(tar.extractfile(entry).read())
                if result.keys() == wanted:
                    break
    return result


def translate_resize(setup, width, height):
    state = setup.find("State")
    if state is None or setup.findtext("./Base/NAME") != "Resize":
        return {}, ["Resize setup could not be decoded"]
    size = (state.findtext("DestinationWidth"), state.findtext("DestinationHeight"))
    if size != (str(width), str(height)):
        return {}, [f"Resize destination {size[0]} × {size[1]} differs from the timeline"]
    mode = state.findtext("ResizeType")
    quality = state.findtext("ResizeQuality")
    if state.findtext("ResMLMode", "0") != "0" or mode not in RESIZE_TYPE:
        return {}, ["Resize uses a Flame-specific mode"]
    if mode in ("1", "2") and state.findtext("ResizePreserveAspect") != "True":
        return {}, ["Resize uses a custom aspect ratio"]
    if any(state.findtext(f"ResizeBackColour{channel}") != "0"
           for channel in ("Red", "Green", "Blue", "Alpha")):
        return {}, ["Resize uses custom padding"]
    properties = {"Scaling": RESIZE_TYPE[mode]}
    if quality in RESIZE_FILTER:
        properties["ResizeFilter"] = RESIZE_FILTER[quality]
    warnings = ["Check Resize against Flame. Resolve uses its own sampling and does not "
                "carry Flame’s precision and crisp/soft controls."]
    for channel in state.findall("./ResizeChannels/Channel"):
        if not len(channel):
            continue
        axis = {"Position/x": "Pan", "Position/y": "Tilt"}.get(channel.get("Name"))
        if mode != "2" or not axis or {child.tag for child in channel} != {"Value", "Extrap"} or channel.findtext("Extrap") != "constant":
            warnings.append(f"Resize channel {channel.get('Name')} needs review")
            continue
        source_width = int(state.findtext("SavedWidth", "0"))
        source_height = int(state.findtext("SavedHeight", "0"))
        position = float(channel.findtext("Value"))
        if min(source_width, source_height) <= 0 or not math.isfinite(position):
            warnings.append(f"Resize channel {channel.get('Name')} has invalid geometry")
            continue
        # Flame stores the crop-box center in source pixels. Resolve moves the image.
        scale = max(width / source_width, height / source_height)
        properties[axis] = -position * scale
        warnings.append(f"Check translated {axis} against Flame’s framing.")
    return properties, warnings

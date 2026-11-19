"""Read the observed Flame sequence records and retained OpenClip versions."""

from collections import defaultdict
import gzip
import math
from pathlib import PurePosixPath
import re
import struct
import xml.etree.ElementTree as ET
import zlib
import tarfile

from . import flame

SEQUENCE = re.compile(rb"\x88\xac\xc2\xa4....\x84\x89\x8d\xf6....\x88\xa4\xf1\xdc............\x80\x8f\xd4\xd4", re.S)
TRACK = re.compile(rb"\x88\xa2\xfd\xca....\x81\x93\xa2\xae....\x81\x9f\xd5\xa3....\x81\xa0\xcf\xac", re.S)
CUT = re.compile(rb"\x88\xae\xb6\xb6....\x83\x9c\xb1\xf4", re.S)
SOURCE = re.compile(rb"\x82\xb6\xac\xb1", re.S)
EFFECT = re.compile(rb"<NAME>([\w .+-]{1,80})</NAME>")
FILENAME = re.compile(r"[\w][\w .-]{0,199}", re.UNICODE)
SCALE = {"preserve": None, "center": 1, "fit": 2, "fill": 3, "stretch": 4}
ARCHIVE_ERRORS = (ValueError, OSError, EOFError, ET.ParseError, zlib.error, tarfile.TarError, struct.error)


def validate_files(entries):
    flame.require(isinstance(entries, list) and 1 <= len(entries) <= 3,
                  "Choose one archive header and, optionally, its first segment and archive log.")
    names = []
    for entry in entries:
        flame.require(isinstance(entry, dict) and isinstance(entry.get("name"), str)
                      and FILENAME.fullmatch(entry["name"]) and isinstance(entry.get("size"), int)
                      and 0 < entry["size"] <= 1_000_000_000, "Invalid Flame archive file.")
        names.append(entry["name"])
    flame.require(len(set(names)) == len(names), "Duplicate archive files.")
    headers = [name for name in names if not name.endswith((".seg",".log"))]
    flame.require(len(headers) == 1, "Include one extensionless Flame archive header.")
    header, = headers
    flame.require(all(name in (header, header + "_1.seg") or
                      re.fullmatch(re.escape(header) + r"_\d{8,14}\.log", name)
                      for name in names), "Choose only files from the same Flame archive. This pilot reads the first segment.")
    return header


def end_of(data, start):
    end = start + 8 + flame.unsigned(data, start + 4)
    flame.require(start + 8 <= end <= len(data), "Invalid Flame record length.")
    return end


def source_index(data):
    result = defaultdict(list)
    for match in SOURCE.finditer(data):
        start = match.start()
        end = start + 8 + int.from_bytes(data[start+4:start+8], "big")
        if start+60 < end <= len(data):
            result[data[start+48:start+60]].append(memoryview(data)[start:end])
    return result


def source_index_for_clips(data):
    # Bound the source objects passed to the OpenClip identity check.
    result = {}
    for key, records in source_index(data).items():
        if len(records) != 1:
            continue
        raw = bytes(records[0])
        if len(raw) <= 1 << 20:
            result[key] = [raw]
    return result


def cut_track(data, track, clips, sources, rate):
    end = end_of(data, track)
    base = track + 24
    size = flame.unsigned(data, base + 38, 2)
    body = base + 40 + size
    length, = struct.unpack_from(">d", data, body + 9)
    flame.require(body + 17 < end and data[body+8] == rate and
                  length.is_integer() and 0 < length <= 10_000_000, "Unsupported track timing.")
    start = 0
    cuts = []
    boundaries = []
    last = track
    for match in CUT.finditer(data, body + 17, end):
        at = match.start()
        if at < last:
            raise ValueError("Nested effects or layered clips need manual recovery.")
        last = end_of(data, at)
        flame.require(last <= end and len(cuts) < 1800, "Unsupported track layout.")
        cut, out = flame.read_cut(data, at, start, clips, sources)
        flame.require(cut.source.lower().endswith(".exr") and "%" not in cut.source and path_parts(cut.source) and
                      cut.source_width > 0 and cut.source_height > 0,
                      "This edit needs a source format other than a numbered OpenEXR sequence.")
        frames = flame.FRAME_RANGE.search(cut.source)
        flame.require(frames and len(frames[1]) == len(frames[2]) and 0 <= cut.source_in,
                      "The archived EXR range is invalid.")
        frame_count = int(frames[2]) - int(frames[1]) + 1
        flame.require(0 < frame_count <= 100_000, "The archived EXR range is invalid.")
        qualify_retime(cut, data[at:last], frame_count)
        if not cut.retime:
            flame.require(out - cut.source_in == cut.duration and cut.source_in + cut.duration <= frame_count,
                          "Timewarp or source geometry needs manual recovery.")
        # A 1000/1001 rate requires a separately qualified mixed-rate map.
        flame.require(not cut.source_rate or abs(cut.source_rate-rate) < 0.001, "Mixed-rate clip needs manual recovery.")
        cuts.append(cut)
        boundaries.append(at)
        start += cut.duration
        flame.require(start <= length, "Track has overlaps or nested edits.")
    flame.require(cuts and start == length, "Track contains gaps or an unsupported edit layout.")
    effects = []
    for at, stop in zip(boundaries, boundaries[1:] + [end]):
        refs = {tuple(part.decode() for part in match.groups())
                for match in flame.SIDECAR.finditer(data, at, stop)}
        inline = {match[1].decode() for match in EFFECT.finditer(data, at, stop)}
        effects.append((refs, inline))
    return cuts, effects


def qualify_retime(cut, record_bytes, frame_count):
    payloads = list(flame.timewarp_payloads(record_bytes))
    if len(payloads) != 1:
        return
    _, state = payloads[0]
    speed_state = state.find("TW_Speed/Channel")
    if (state.findtext("TW_RetimerMode") != "0" or state.findtext("TW_FrameMode") != "2"
        or speed_state is None or any(key.tag == "Key" for key in state.iter("Key"))):
        return
    speed = float(speed_state.findtext("Value", "nan"))
    anchor = float(state.findtext("TW_AnchorOffset", "nan"))
    if (math.isfinite(anchor) and 0.0 <= anchor and 0.0 < speed <= 1000 and speed != 100
        and round(anchor) + cut.duration * speed / 100 <= frame_count):
        cut.retime = flame.Retime(speed, anchor)


def finish_plan(sequence, effects, setups):
    dims = set()
    for refs, _ in effects:
        for key in refs:
            setup = setups.get(key)
            if key[1].endswith(".resize_node") and setup is not None:
                size = tuple(int(setup.findtext(f"State/Destination{axis}", "0")) for axis in ("Width", "Height"))
                if all(16 <= n <= 16384 for n in size):
                    dims.add(size)
    if len(dims) == 1:
        sequence.width, sequence.height = dims.pop()
    if not sequence.width:
        sequence.width, sequence.height = 1920, 1080
        sequence.cuts[0].issues.append("Sequence format could not be read. Export defaults to 1920 × 1080. Verify the framing.")
    for cut, (refs, inline) in zip(sequence.cuts, effects):
        if cut.retime:
            inline.discard("Timewarp")
        if inline:
            cut.issues.extend(f"Untranslated effect: {name}" for name in sorted(inline))
        resize = []
        for key in sorted(refs):
            name = key[1].removeprefix("pure_side_car_setup.").removesuffix("_node")
            setup = setups.get(key)
            if name == "resize" and setup is not None:
                resize.append(setup)
            elif setup is None:
                cut.issues.append(f"Untranslated effect: {name}. Setup unavailable.")
            else:
                cut.issues.append(f"Untranslated effect: {setup.findtext('./Base/NAME', name)}.")
        if len(resize) == 1:
            geometry = tuple(int(resize[0].findtext(f"State/Saved{axis}", "0"))
                             for axis in ("Width", "Height"))
            if all(geometry) and geometry != (cut.source_width, cut.source_height):
                cut.issues.append("Untranslated Resize: its input was already reframed upstream.")
            else:
                settings, issues = flame.translate_resize(resize[0], sequence.width, sequence.height)
                cut.settings.update(settings)
                cut.issues.extend(issues)
        elif resize:
            cut.issues.append("Untranslated effect: multiple Resize nodes on the same clip.")
        if cut.color:
            cut.issues.append(f"Verify input color space and viewing transform. Archived tag: {cut.color}.")


def inspect(header):
    data = flame.archive_metadata(header)
    entries = []
    clips = flame.openclips(data)
    sources = source_index_for_clips(data)
    batches = {}
    pending = {}
    for match in SEQUENCE.finditer(data):
        at = match.start()
        if at + 64 > len(data):
            continue
        base = at + 24
        size = flame.unsigned(data, base + 38, 2)
        start = base + 40 + size
        if not 0 < size <= 512 or data[base+31:base+33] != b"\x08\x50" or start + 9 >= len(data):
            continue
        name = data[base+40:start].decode("utf-8", "replace")
        if any(ord(c) < 32 for c in name):
            continue
        rate = data[start+8]
        time, = struct.unpack_from(">d", data, start)
        entry = {"id": f"{at:x}", "name": name, "fps": rate}
        try:
            end = end_of(data, at)
            flame.require(start + 9 < end and rate in (24,25,30) and time.is_integer() and
                          0 <= time <= 24*3600*rate, "Unsupported sequence timecode or frame rate.")
            tracks = list(TRACK.finditer(data, start + 9, end))
            flame.require(len(tracks) == 1, "Multiple tracks or an unrecognized sequence layout.")
            track = tracks[0].start()
            cuts, effects = cut_track(data, track, clips, sources, rate)
            plan = flame.TimelinePlan(name, rate, 0, 0, cuts, int(time))
            batches[entry["id"]] = plan
            pending[entry["id"]] = effects
            entry.update(cuts=len(cuts), frames=sum(cut.duration for cut in cuts))
        except ARCHIVE_ERRORS as error:
            entry["reason"] = str(error)[:220]
        entries.append(entry)
        flame.require(len(entries) <= 1250, "Too many sequence records for one upload.")
    flame.require(entries, "No Flame sequence records found in this archive header.")
    first = header.with_name(header.name + "_1.seg")
    if first.exists():
        with first.open("rb") as file:
            flame.require(file.read(3) == b"\x1f\x8b\x08", "Unrecognized Flame segment.")
        with gzip.open(first, "rb") as file:
            header_bytes = file.read(512)
            flame.require(len(header_bytes) == 512, "Truncated Flame segment catalog.")
            tarfile.TarInfo.frombuf(header_bytes, "utf-8", "replace")
    log_pattern = re.compile(re.escape(header.name) + r"_\d{8,14}\.log")
    for log in (p for p in header.parent.glob("*.log") if log_pattern.fullmatch(p.name)):
        with log.open("rb") as file:
            flame.require(file.readline(120).startswith(b"# Discreet flame_archive log file - "),
                          "Unrecognized Flame archive log.")
    wanted = {ref for effects in pending.values() for refs, _ in effects for ref in refs
              if ref[1].endswith(".resize_node")}
    try:
        setups = flame.sidecar_setups(first, wanted)
    except ARCHIVE_ERRORS:
        setups = {}
    for key, plan in batches.items():
        finish_plan(plan, pending[key], setups)
    for entry in entries:
        if plan := batches.get(entry["id"]):
            entry.update(width=plan.width, height=plan.height)
    return entries, batches


def path_parts(value):
    flame.require(isinstance(value, str) and len(value) <= 4096 and value.startswith("/")
                  and not value.startswith("//") and "\\" not in value and
                  not any(ord(c) < 32 or ord(c) == 127 for c in value) and
                  not any(part in (".", "..") for part in value.split("/")),
                  "Paths must be absolute POSIX paths without . or .. components.")
    return PurePosixPath(value).parts


def source_bases(plans):
    groups = defaultdict(list)
    for plan in plans:
        for cut in plan.cuts:
            parents = path_parts(cut.source)[:-1]
            if len(parents) > 1:
                anchor_size = 3 if parents[1] in ("Volumes", "mnt", "media") else 2
                groups[parents[:anchor_size]].append(parents)
            else:
                groups[parents].append(parents)
    bases = []
    for members in groups.values():
        shared = []
        for values in zip(*members):
            if len(set(values)) != 1:
                break
            shared.append(values[0])
        bases.append(str(PurePosixPath(*shared)).rstrip("/") + "/")
    return sorted(set(bases))


def mappings(rows):
    flame.require(isinstance(rows, list) and len(rows) <= 100, "Too many remapping rules.")
    result = {}
    for row in rows:
        flame.require(isinstance(row, dict), "Invalid remapping row.")
        source = path_parts(row.get("source"))
        target = path_parts(row.get("target"))
        flame.require(source not in result, "A source base can only have one destination.")
        result[source] = target
    return sorted(result.items(), key=lambda item: -len(item[0]))


def remap(value, rules):
    pieces = path_parts(value)
    for source, target in rules:
        if pieces[:len(source)] == source and len(pieces) > len(source):
            return str(PurePosixPath(*target, *pieces[len(source):])), True
    return value, False


def scale_factors(mode, cut, width, height):
    x, y = width/cut.source_width, height/cut.source_height
    return {1: (1,1), 2: (min(x,y),)*2, 3: (max(x,y),)*2, 4: (x,y)}[mode]


def framing(plan, cut, selected):
    settings = cut.settings.copy()
    requested = SCALE[selected]
    archived = settings.get("Scaling")
    if requested is None:
        return settings
    settings["Scaling"] = requested
    if archived and cut.source_width > 0 and cut.source_height > 0:
        old_x, old_y = scale_factors(archived, cut, plan.width, plan.height)
        new_x, new_y = scale_factors(requested, cut, plan.width, plan.height)
        settings.update(ZoomX=old_x/new_x, ZoomY=old_y/new_y)
        if requested != archived:
            cut.issues.append("Check framing at clipped edges. Resolve input scaling and Inspector zoom use different processing stages.")
    return settings

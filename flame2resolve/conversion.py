"""Shared export and review policy for the web app and command line."""

from copy import deepcopy

from . import archives, drt


def convert(original, mode, rules, remapping=False):
    if not isinstance(mode, str) or mode not in archives.SCALE:
        raise ValueError("Select a valid Resolve scale mode.")
    plan = deepcopy(original)
    paths = []
    settings = []
    rows = []
    for cut in plan.cuts:
        path, matched = archives.remap(cut.source, rules)
        if len(path) > 4096:
            raise ValueError("Mapped path is too long.")
        if remapping and not matched:
            cut.issues.append("No path mapping matched this source. Resolve keeps the archived path.")
        if cut.retime:
            cut.issues.append(f"Editable {cut.retime.speed:g}% Timewarp uses Resolve Frame Blend. "
                              "Check the starting frame and interpolation against Flame.")
        settings.append(archives.framing(plan, cut, mode))
        paths.append(path)
        if cut.issues or cut.source != path:
            rows.append({"name": cut.name, "frame": cut.record, "duration": cut.duration,
                         "issues": cut.issues, "source": cut.source, "destination": path})
    report = {"timeline": plan.name, "fps": plan.fps, "start": plan.start,
              "clips": len(plan.cuts), "rows": rows}
    return drt.export(plan, paths, settings), report

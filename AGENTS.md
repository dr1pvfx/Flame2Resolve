# Flame2Resolve coding instructions

Read [README.md](README.md) for the supported archive subset before making changes. The default goal is to preserve media identity, frame placement and the review notes for a single editable Resolve timeline.

## Source map

| Path | Responsibility |
| --- | --- |
| `flame2resolve/flame.py` | Bounded reads of native archive metadata, embedded OpenClip versions and effect setup sidecars. |
| `flame2resolve/archives.py` | Timeline recognition, input validation, effect qualification, base-path suggestions and scaling calculations. |
| `flame2resolve/drt.py` | Resolve timeline container, media identities, clip properties, time maps and markers. XML shells in `templates/` come from a synthetic Resolve export. |
| `flame2resolve/conversion.py` | Shared path mapping, effect review and export used by the browser and CLI. |
| `flame2resolve/server.py` and `flame2resolve/static/` | Password gate, session-owned uploads, timeline selection, mapping, download and printable review. |
| `tests/make_fixture.py` | Synthetic byte layout for regression tests. It contains no production media. |

## Verify changes

1. Run `python3 -m unittest discover -s tests -v` from the repo root. For browser changes, exercise sign-in, upload, mapping toggle, export and print. For command-line changes, generate a local fixture and run the README examples.
2. Treat binary records as length-delimited, version-dependent input. Preserve decompression and upload limits, and leave references as data. Add fixtures for altered timing or file layouts with boundaries, record start, duration, source in and archived feed version assertions.
3. Before expanding a DRT compatibility or effect claim, import the generated timeline into a scratch Resolve project. Verify frame rate, resolution, the media-path readback, cuts, Inspector properties, and markers. For framing or Timewarp claims, compare rendered frames or source timecodes against a reference. Record the tool versions and distinguish a native check from a synthetic test.
4. Decline a sequence with ambiguous editorial timing. For confidently placed cuts with unsupported effects, keep a review marker and explain the limitation in the report. Add supported formats to the README alongside the evidence.

Keep real archives, media paths, passwords, verifier files, API tokens and customer metadata out of the repo, test fixtures, issues and example outputs. Use the ignored `local/` directory for private experiments and the synthetic fixture for redistributable tests. Keep names, dates, paths, UUIDs and image data from workstation exports out of committed Resolve shells.

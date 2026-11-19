# Flame2Resolve

Recover an editable DaVinci Resolve timeline from a supported Autodesk Flame archive. Flame2Resolve reads the archive’s edit metadata, follows the OpenClip versions saved with the sequence, and writes one downloadable `.drt` for the timeline you select. Detected conform problems travel with the edit as Resolve timeline markers and a printable review.

Run it on your workstation or self-host the password-gated browser app. The reader and writer run without a Flame installation or license. Use DaVinci Resolve to import and check the result.

> **Early release.** Keep your source archive and verify the import against a Flame reference when you have one. The native archive and DRT layouts can vary by version.

## What works

| Area | Current coverage |
| --- | --- |
| Archive input | Native disk archive header, optional matching `_1.seg`, and optional matching archive log. The browser accepts up to **1 GB combined** in 8 MiB chunks. |
| Timeline selection | Recognized contiguous single-video-track sequences at 24, 25 or 30 fps. Frame counts distinguish duplicate sequence names. |
| Media conform | Numbered OpenEXR sequences from the **saved OpenClip feed version**, with source trim and record placement. Resolve reads the media at the path in the output. |
| Path mapping | Optional directory-prefix rules. **Add source base paths** suggests common roots across recoverable sequences. The longest matching rule wins. |
| Resize | Supported static Resize modes, matching Resolve filter choices and eligible crop offsets. Select Preserve Flame framing, Fit, Fill, Center or Stretch. When the baseline changes, Inspector zoom compensates for the supported Flame geometry. |
| Timewarp | Selected forward constant-speed setups with a decodable anchor. Resolve Frame Blend approximates Flame interpolation, and the review calls out anchor and frame checks. |
| Review | Per-cut notes in the browser and in the DRT’s timeline markers. Print the current report or save it as a PDF from the browser. The CLI returns the same review as JSON. |

## Coverage limits

- **Edit layout.** Audio, multiple tracks, gaps, nested timelines, transitions and mixed frame rates need additional coverage. The browser lists recognized candidates whose cut placement cannot be recovered.
- **Effects.** Action and 2D Transform stacks, animated Resize, ramps, reverse, holds, Matchbox, color and other Flame-specific rendering need manual reconstruction. Intermediate crops and differences in sampling can affect apparent framing. The report covers detected effects on recovered cuts. Unsupported sequence structures can produce a layout warning with limited effect detail.
- **Archive content.** Use sequence archives that point to external media. Batch setups, media extraction, physical tape archives and continuation segments are outside this release. The first segment supplies supported Timeline FX setups. Header-only exports can have fewer translated effects.
- **Conform.** This release accepts POSIX paths and linked numbered EXRs. Mount the referenced media on the Resolve workstation or use a path map and relink there. A sequence without a decoded format exports at 1920 × 1080 with a marker. Resolve compatibility was checked with **Studio 21.0.4.5**.

## Quick start

Requires **Python 3.11+** and the system **libzstd** shared library on macOS or Linux. There are no Python package dependencies.

```sh
# macOS with Homebrew
brew install zstd

# Debian or Ubuntu
sudo apt install python3 libzstd1

# Fedora or Rocky Linux
sudo dnf install python3.11 libzstd
```

Use the command for your platform. For a custom zstd installation, set `FLAME2RESOLVE_ZSTD` to the shared library path. Substitute `python3.11` below if that is the executable for Python 3.11+ on your system.

```sh
git clone https://github.com/dr1pvfx/Flame2Resolve.git
cd Flame2Resolve
python3 -m flame2resolve init-auth
python3 -m flame2resolve serve
```

`init-auth` asks for a password interactively and writes a local salted verifier. Open [127.0.0.1:8427](http://127.0.0.1:8427/) and enter the password you just chose.

### Recover a timeline in the browser

1. In Flame, select the sequence or folder of sequences and archive with media excluded. Keep the files written by Flame together.
2. In Flame2Resolve, choose the archive header and the matching first segment if available. Click **Read archive**, then select a recoverable sequence.
3. If the media moved, enable **Remap media paths** and click **Add source base paths**. Fill in the destination for each rule you want to use. Remove unused rows.
4. Choose Resolve input scaling and click **Prepare .drt**. Download the timeline and import it into the Resolve Media Pool using the timeline import command.
5. Check linked media, framing and markers in Resolve. Use **Print review** to share the handoff notes.

For example, `/loc/a/b/c/shot.exr` and `/loc/a/b/d/other.exr` share the base `/loc/a/b/`. Map that base to `/Volumes/new-storage/project/` and the output retains the `c/` and `d/` subdirectories.

### Recover a timeline from the terminal

Keep the header and optional first segment in the same directory. `inspect` prints candidate IDs, dimensions, supported cut counts and source base paths. Use an ID without a `reason` field.

```sh
python3 -m flame2resolve inspect /path/to/archive/snapshot

python3 -m flame2resolve export /path/to/archive/snapshot \
  --timeline SEQUENCE_ID \
  --scale preserve \
  --map /Volumes/old-storage/project/ /Volumes/new-storage/project/ \
  --output recovered.drt > recovery-review.json
```

Repeat `--map` for multiple rules or omit it to preserve original paths. `audit-timewarp` prints raw, length-validated Timewarp setup records for debugging. Those records can include multiple copies and setup versions.

### Try it with synthetic data

```sh
python3 tests/make_fixture.py local/demo
python3 -m flame2resolve inspect local/demo
python3 -m unittest discover -s tests -v
```

The fixture models the reader’s supported byte layout and demonstrates saved OpenClip versions, Resize and a constant-speed Timewarp. Its referenced media paths are fictional. It is useful for checking conversion and markers, and the resulting clips are offline until corresponding media is available.

## Self-hosting

The app listens on loopback and has a single shared development password. To serve a small trusted team, put an HTTPS reverse proxy in front and start it with the exact public origin.

```sh
python3 -m flame2resolve serve --origin https://flame2resolve.example.org
```

Forward requests to `http://127.0.0.1:8427`. Set the proxy’s request limit above 8 MiB and preserve `Content-Length`, `Origin` and cookies. Use the included [systemd unit](deploy/flame2resolve.service) as a starting point on Linux. It expects this checkout at `/opt/flame2resolve`, a Python 3.11+ executable in `ExecStart`, and a verifier at `/etc/flame2resolve/password-verifier`. Generate the verifier interactively, keep it readable only by the operator, and set `FLAME2RESOLVE_ORIGIN=https://flame2resolve.example.org` in `/etc/flame2resolve/service.env` for HTTPS.

The browser upload stays in a session-owned scratch directory until inspection finishes. Raw files are removed after inspection. The service removes expired sessions and abandoned uploads after an hour. Restarting the service clears active sessions and results. Proxy upload and connection limits still matter if you make the service internet-accessible.

## Contributing with a coding agent

Start with [AGENTS.md](AGENTS.md). It describes the parser and writer boundaries, how to build a synthetic regression fixture, and what to verify in Resolve before extending format coverage. When reporting an issue, share the Flame and Resolve versions, the observed sequence layout and a small redacted reproduction you have permission to publish.

[MIT License](LICENSE)

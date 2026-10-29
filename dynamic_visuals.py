#!/usr/bin/env python3
"""
dynamic_visuals.py
==================
Level 1 visual upgrade for a NotebookLM (or any slideshow-style) video.

It keeps NotebookLM's audio untouched (you said the voice is good) and rebuilds
ONLY the picture so it stops looking like a static AI slide deck:

  1. SCENE DETECT - finds where each slide changes.
  2. MOTION       - gives every slide its own movement (zoom in / out / pan in
                    different directions, cycled) so nothing sits still.
  3. TRANSITIONS  - a quick dip between slides (kept short so audio stays in sync).
  4. COLOR GRADE  - a mild consistent cinematic look across the whole video.
  5. BRANDING     - optional: cover the NotebookLM logo (delogo) and drop your
                    own channel logo in a corner.

Audio length is preserved exactly, so the kept narration never desyncs.

--------------------------------------------------------------------------------
INSTALL
    sudo apt-get install ffmpeg          # only dependency

USAGE (just dynamic motion + grade, keep everything else)
    python dynamic_visuals.py input.mp4 -o dynamic.mp4

USAGE (also add your logo and cover NotebookLM's logo)
    python dynamic_visuals.py input.mp4 -o dynamic.mp4 \
        --logo channel_logo.png --cover 40:30:230:70

    --cover takes x:y:w:h of the region to blur out (find the NotebookLM logo's
    box once in any frame, then reuse the same numbers every time).
--------------------------------------------------------------------------------
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def run(cmd, capture_stderr=False):
    res = subprocess.run(
        cmd, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Command failed:\n  {' '.join(cmd)}\n{res.stderr[-2000:]}")
    return res.stderr if capture_stderr else None


def log(msg):
    print(f"[visuals] {msg}", flush=True)


def probe_duration(path):
    return float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path], text=True).strip())


def probe_resolution(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
        text=True).strip()
    w, h = out.split("x")
    return int(w), int(h)


def probe_fps(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate", "-of", "default=nw=1:nk=1", path],
        text=True).strip()
    num, den = (out.split("/") + ["1"])[:2]
    return float(num) / float(den) if float(den) else float(num)


# --------------------------------------------------------------------------- #
# 1. scene detection
# --------------------------------------------------------------------------- #
def detect_scenes(path, threshold, min_seg, total, workdir):
    """Return a list of (start, end) covering the whole video, split at slide
    changes. Falls back to fixed chunks if detection finds too little."""
    log(f"detecting slide changes (threshold={threshold}) ...")
    # NOTE: metadata=print must write to a real file. With ':file=-' it goes to
    # stdout (which we discard), so detection always read nothing and fell back
    # to fixed chunks. Write to a temp file and parse that instead.
    scenefile = os.path.join(workdir, "scenes.txt")
    run(
        ["ffmpeg", "-i", path, "-filter:v",
         f"select='gt(scene,{threshold})',metadata=print:file={scenefile}",
         "-an", "-f", "null", "-"],
    )
    text = Path(scenefile).read_text() if os.path.exists(scenefile) else ""
    times = sorted({float(m) for m in re.findall(r"pts_time:([0-9.]+)", text)})

    # build boundaries, merge anything shorter than min_seg
    bounds = [0.0]
    for t in times:
        if t - bounds[-1] >= min_seg and (total - t) >= min_seg:
            bounds.append(t)
    bounds.append(total)

    # fallback: detection too weak -> fixed chunks so motion still varies
    if len(bounds) <= 2:
        log("  few/no scene cuts found; falling back to fixed chunks")
        chunk = max(min_seg, 7.0)
        bounds = [0.0]
        while bounds[-1] + chunk < total:
            bounds.append(round(bounds[-1] + chunk, 3))
        bounds.append(total)

    segs = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    log(f"  -> {len(segs)} segments")
    return segs


# --------------------------------------------------------------------------- #
# 2. per-scene motion
# --------------------------------------------------------------------------- #
def motion_filter(idx, frames, W, H, fps):
    """Cycle through motion styles so consecutive slides never move the same way."""
    F = max(frames, 1)
    center = f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    styles = [
        f"zoompan=z='min(1.0+0.0010*on,1.18)':d=1:{center}:s={W}x{H}:fps={fps}",          # zoom in
        f"zoompan=z='if(lte(1.18-0.0010*on,1.0),1.0,1.18-0.0010*on)':d=1:{center}:s={W}x{H}:fps={fps}",  # zoom out
        f"zoompan=z='1.12':d=1:x='(iw-iw/zoom)*on/{F}':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}",   # pan right
        f"zoompan=z='1.12':d=1:x='(iw-iw/zoom)*(1-on/{F})':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}", # pan left
        f"zoompan=z='1.12':d=1:x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*on/{F}':s={W}x{H}:fps={fps}",   # pan down
        f"zoompan=z='1.12':d=1:x='iw/2-(iw/zoom/2)':y='(ih-ih/zoom)*(1-on/{F})':s={W}x{H}:fps={fps}", # pan up
    ]
    return styles[idx % len(styles)]


def process_segment(cfg, src, idx, start, dur, out_path):
    W, H, fps = cfg.width, cfg.height, cfg.fps
    frames = max(1, round(dur * fps))
    parts = []

    # Cover the NotebookLM logo BEFORE motion: in the source the logo sits at a
    # fixed spot on flat background, so delogo interpolates cleanly. (Doing this
    # after zoompan smears it, because the zoom/pan has moved the logo.)
    if cfg.cover:
        x, y, w, h = cfg.cover.split(":")
        parts.append(f"delogo=x={x}:y={y}:w={w}:h={h}")

    parts.append(motion_filter(idx, frames, W, H, fps))

    if cfg.grade:
        parts.append("eq=contrast=1.06:saturation=1.12:brightness=0.01")

    td = cfg.transition
    if td > 0 and dur > 2 * td:
        parts.append(f"fade=t=in:st=0:d={td}")
        parts.append(f"fade=t=out:st={dur - td:.3f}:d={td}")

    vf = ",".join(parts)
    run(["ffmpeg", "-y", "-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}",
         "-vf", vf, "-an", "-r", str(fps),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-pix_fmt", "yuv420p", out_path])


# --------------------------------------------------------------------------- #
# 5. final assembly: concat, cover/logo overlay, re-attach original audio
# --------------------------------------------------------------------------- #
def assemble(cfg, seg_paths, out):
    workdir = cfg.work
    listfile = os.path.join(workdir, "concat.txt")
    Path(listfile).write_text("".join(f"file '{p}'\n" for p in seg_paths))
    concat = os.path.join(workdir, "concat.mp4")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
         "-c", "copy", concat])

    # NotebookLM logo is already covered per-segment (before motion); here we
    # only add the channel logo as a fixed badge in the output corner.
    inputs = ["-i", concat, "-i", cfg.input]
    if cfg.logo:
        inputs += ["-i", cfg.logo]

    if cfg.logo:
        # scale logo to ~1/9 of height, place bottom-right with padding
        chain = (f"[2:0]scale=-1:{cfg.height // 9}[lg];"
                 f"[0:v][lg]overlay=W-w-40:H-h-40[v]")
        run(["ffmpeg", "-y"] + inputs +
            ["-filter_complex", chain, "-map", "[v]", "-map", "1:a",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20",
             "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
             "-shortest", out])
    else:
        run(["ffmpeg", "-y"] + inputs +
            ["-map", "0:v", "-map", "1:a", "-c:v", "copy",
             "-c:a", "aac", "-b:a", "192k", "-shortest", out])


# --------------------------------------------------------------------------- #
@dataclass
class Config:
    input: str
    work: str
    width: int
    height: int
    fps: int
    grade: bool = True
    transition: float = 0.12
    cover: str | None = None
    logo: str | None = None


def main():
    p = argparse.ArgumentParser(description="Add dynamic motion to a slideshow video.")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--threshold", type=float, default=0.30, help="scene-cut sensitivity (lower = more cuts)")
    p.add_argument("--min-seg", type=float, default=1.5, help="merge slides shorter than this (sec)")
    p.add_argument("--transition", type=float, default=0.12, help="dip duration between slides (sec); 0 = off")
    p.add_argument("--no-grade", action="store_true")
    p.add_argument("--cover", help="x:y:w:h region to blur out (e.g. the NotebookLM logo)")
    p.add_argument("--logo", help="your channel logo PNG (placed bottom-right)")
    args = p.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"Input not found: {args.input}")

    W, H = probe_resolution(args.input)
    fps = round(probe_fps(args.input))
    total = probe_duration(args.input)
    workdir = tempfile.mkdtemp(prefix="visuals_")
    log(f"source: {W}x{H} @ {fps}fps, {total:.1f}s")

    cfg = Config(
        input=args.input, work=workdir, width=W, height=H, fps=fps,
        grade=not args.no_grade, transition=args.transition,
        cover=args.cover, logo=args.logo,
    )

    segs = detect_scenes(args.input, args.threshold, args.min_seg, total, workdir)

    seg_paths = []
    for i, (start, end) in enumerate(segs):
        out_seg = os.path.join(workdir, f"seg_{i:04d}.mp4")
        log(f"  motion on segment {i+1}/{len(segs)} ({start:.1f}-{end:.1f}s)")
        process_segment(cfg, args.input, i, start, end - start, out_seg)
        seg_paths.append(out_seg)

    log("assembling final video (keeping original audio) ...")
    assemble(cfg, seg_paths, args.output)
    log(f"DONE -> {args.output}")


if __name__ == "__main__":
    main()

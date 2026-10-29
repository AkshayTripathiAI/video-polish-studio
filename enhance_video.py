#!/usr/bin/env python3
"""
enhance_video.py
================
Takes a video you generated with NotebookLM (or anything else) and adds a
human/production layer on top of it so the final upload reads as a genuinely
edited piece of content rather than a raw AI dump.

What it does (each stage is optional, controlled by flags):
  1. TRANSCRIBE   - faster-whisper pulls a word/segment transcript with timings.
  2. RE-VOICE     - regenerate narration with YOUR voice (ElevenLabs clone) or any
                    TTS, then time-fit each segment back onto the original timeline
                    so picture and audio stay in sync.
  3. CAPTIONS     - burn styled, mobile-friendly subtitles (built from the transcript).
  4. MOTION       - subtle continuous zoom so static slides feel alive.
  5. MUSIC        - mix a quiet background music bed under the narration.
  6. VERTICAL     - optional 9:16 reframe (blurred-pad) for Shorts.
  7. BRANDING     - optional intro/outro stitched on.
  8. METADATA     - dumps the transcript to JSON for your title/description/SEO.

NOTE ON POLICY: This improves production quality. If the content is still
substantially AI-generated/synthetic, you must still enable YouTube's
"Altered or synthetic content" disclosure. This tool is about quality, not deception.

--------------------------------------------------------------------------------
INSTALL
    sudo apt-get install ffmpeg            # system dependency
    pip install faster-whisper pydub requests

USAGE (basic - captions + motion + music, keep original voice)
    python enhance_video.py input.mp4 -o final.mp4 \
        --music bed.mp3 --zoom

USAGE (re-voice with your ElevenLabs clone)
    export ELEVENLABS_API_KEY=sk-...
    python enhance_video.py input.mp4 -o final.mp4 \
        --revoice --voice-id YOUR_VOICE_ID --zoom --music bed.mp3

USAGE (vertical Short with branding)
    python enhance_video.py input.mp4 -o short.mp4 \
        --vertical --intro intro.mp4 --outro outro.mp4
--------------------------------------------------------------------------------
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def run(cmd: list, quiet: bool = True):
    """Run a subprocess command, raising on failure."""
    res = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.PIPE,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Command failed:\n  {' '.join(cmd)}\n{res.stderr}")
    return res


def ffprobe_duration(path: str) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path],
        text=True,
    )
    return float(out.strip())


def ffprobe_resolution(path: str):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
        text=True,
    ).strip()
    w, h = out.split("x")
    return int(w), int(h)


def log(msg: str):
    print(f"[enhance] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    input: str
    output: str
    work: str
    width: int = 1920
    height: int = 1080
    fps: int = 30
    # stages
    revoice: bool = False
    captions: bool = True
    zoom: bool = False
    vertical: bool = False
    music: str | None = None
    intro: str | None = None
    outro: str | None = None
    # transcription / tts
    whisper_model: str = "small"
    language: str | None = None          # e.g. "hi" for Hindi; None = auto
    voice_id: str | None = None
    music_volume: float = 0.08
    # caption styling
    caption_font: str = "Arial"
    caption_size: int = 64


# --------------------------------------------------------------------------- #
# Stage 1: transcription
# --------------------------------------------------------------------------- #
def transcribe(cfg: Config):
    """Return a list of {start, end, text} segments via faster-whisper."""
    from faster_whisper import WhisperModel

    log(f"transcribing with whisper '{cfg.whisper_model}' ...")
    model = WhisperModel(cfg.whisper_model, device="auto", compute_type="auto")
    segments, info = model.transcribe(
        cfg.input, language=cfg.language, vad_filter=True, word_timestamps=False
    )
    out = []
    for s in segments:
        text = s.text.strip()
        if text:
            out.append({"start": float(s.start), "end": float(s.end), "text": text})
    log(f"  -> {len(out)} segments, detected language: {info.language}")
    return out


# --------------------------------------------------------------------------- #
# Stage 2: re-voicing (pluggable TTS)
# --------------------------------------------------------------------------- #
def synthesize_tts(text: str, out_path: str, cfg: Config):
    """
    Turn one segment of text into an audio file at out_path.

    Default implementation uses ElevenLabs. Swap this function body to use
    OpenAI TTS, Piper (local/free), or a recording — the rest of the pipeline
    is agnostic to how the audio is produced.
    """
    import requests

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("Set ELEVENLABS_API_KEY, or replace synthesize_tts().")
    if not cfg.voice_id:
        raise RuntimeError("Pass --voice-id (your cloned voice).")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{cfg.voice_id}"
    resp = requests.post(
        url,
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2",   # handles Hindi etc.
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        },
        timeout=120,
    )
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)


def _atempo_chain(factor: float) -> str:
    """ffmpeg atempo only accepts 0.5–2.0; chain filters for bigger factors."""
    factor = max(0.25, min(factor, 4.0))
    parts = []
    while factor > 2.0:
        parts.append("atempo=2.0")
        factor /= 2.0
    while factor < 0.5:
        parts.append("atempo=0.5")
        factor /= 0.5
    parts.append(f"atempo={factor:.4f}")
    return ",".join(parts)


def revoice(segments, cfg: Config) -> str:
    """
    Generate new narration per segment, time-fit each to its original slot
    (preserving pitch), and lay them onto a silent timeline of the full length.
    Returns the path to the assembled WAV.
    """
    from pydub import AudioSegment

    total_ms = int(ffprobe_duration(cfg.input) * 1000)
    timeline = AudioSegment.silent(duration=total_ms)
    log(f"re-voicing {len(segments)} segments ...")

    for i, seg in enumerate(segments):
        target_dur = max(0.3, seg["end"] - seg["start"])
        raw = os.path.join(cfg.work, f"seg_{i:04d}_raw.mp3")
        fit = os.path.join(cfg.work, f"seg_{i:04d}_fit.wav")

        synthesize_tts(seg["text"], raw, cfg)
        actual = ffprobe_duration(raw)
        factor = actual / target_dur          # >1 means speed up to fit

        run(["ffmpeg", "-y", "-i", raw,
             "-filter:a", _atempo_chain(factor),
             "-ar", "44100", "-ac", "2", fit])

        clip = AudioSegment.from_file(fit)
        timeline = timeline.overlay(clip, position=int(seg["start"] * 1000))

    out_wav = os.path.join(cfg.work, "revoiced.wav")
    timeline.export(out_wav, format="wav")
    log("  -> revoiced track assembled")
    return out_wav


def extract_audio(cfg: Config) -> str:
    """Pull the original audio track to a WAV (used when not re-voicing)."""
    out_wav = os.path.join(cfg.work, "original.wav")
    run(["ffmpeg", "-y", "-i", cfg.input, "-vn", "-ar", "44100", "-ac", "2", out_wav])
    return out_wav


# --------------------------------------------------------------------------- #
# Stage 3: captions (ASS subtitle file)
# --------------------------------------------------------------------------- #
def _ass_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def build_captions(segments, cfg: Config, vertical: bool) -> str:
    """Write a styled .ass subtitle file. Big, bottom-center, readable on phones."""
    play_w, play_h = (1080, 1920) if vertical else (cfg.width, cfg.height)
    margin_v = 220 if vertical else 90
    path = os.path.join(cfg.work, "captions.ass")

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Outline, Shadow, Alignment, MarginL, MarginR, MarginV
Style: Main,{cfg.caption_font},{cfg.caption_size},&H00FFFFFF,&H00000000,&H80000000,1,4,1,2,80,80,{margin_v}

[Events]
Format: Layer, Start, End, Style, Text
"""
    lines = [header]
    for seg in segments:
        text = seg["text"].replace("\n", " ").strip()
        lines.append(
            f"Dialogue: 0,{_ass_time(seg['start'])},{_ass_time(seg['end'])},Main,,{text}\n"
        )
    Path(path).write_text("".join(lines), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Stage 4-6: render (motion, vertical reframe, music mix, captions burn-in)
# --------------------------------------------------------------------------- #
def render(cfg: Config, audio_wav: str, ass_path: str | None):
    """Build one ffmpeg filter_complex doing motion + reframe + captions + audio mix."""
    out = cfg.output
    W, H = cfg.width, cfg.height

    inputs = ["-i", cfg.input, "-i", audio_wav]
    if cfg.music:
        inputs += ["-stream_loop", "-1", "-i", cfg.music]

    # ---- video chain ----
    if cfg.zoom:
        # slow continuous zoom-in; caps at 1.12x
        vchain = (
            f"zoompan=z='min(zoom+0.0004,1.12)':d=1:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"s={W}x{H}:fps={cfg.fps}"
        )
    else:
        vchain = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,fps={cfg.fps}"

    if cfg.vertical:
        # blurred-pad 9:16: foreground fit on top of a blurred filled background
        VW, VH = 1080, 1920
        filtergraph = (
            f"[0:v]{vchain}[base];"
            f"[base]split[bg][fg];"
            f"[bg]scale={VW}:{VH}:force_original_aspect_ratio=increase,"
            f"crop={VW}:{VH},gblur=sigma=25[bgb];"
            f"[fg]scale={VW}:-1[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[vraw]"
        )
        last = "vraw"
    else:
        filtergraph = f"[0:v]{vchain}[vraw]"
        last = "vraw"

    if ass_path:
        ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        filtergraph += f";[{last}]ass='{ass_escaped}'[v]"
    else:
        filtergraph += f";[{last}]copy[v]"

    # ---- audio chain ----
    if cfg.music:
        filtergraph += (
            f";[2:a]volume={cfg.music_volume}[mus];"
            f"[1:a][mus]amix=inputs=2:duration=first:dropout_transition=2[a]"
        )
        amap = "[a]"
    else:
        amap = "1:a"

    cmd = (
        ["ffmpeg", "-y"] + inputs +
        ["-filter_complex", filtergraph,
         "-map", "[v]", "-map", amap,
         "-c:v", "libx264", "-preset", "medium", "-crf", "20",
         "-c:a", "aac", "-b:a", "192k",
         "-shortest", "-pix_fmt", "yuv420p", out]
    )
    log("rendering final video (this is the slow part) ...")
    run(cmd)


# --------------------------------------------------------------------------- #
# Stage 7: branding (intro/outro)
# --------------------------------------------------------------------------- #
def add_branding(cfg: Config, core: str) -> str:
    """Normalize intro/core/outro to the same params and concat them."""
    pieces = []
    norm_dir = cfg.work
    for label, src in [("intro", cfg.intro), ("core", core), ("outro", cfg.outro)]:
        if not src:
            continue
        target = (1080, 1920) if cfg.vertical else (cfg.width, cfg.height)
        norm = os.path.join(norm_dir, f"norm_{label}.mp4")
        run([
            "ffmpeg", "-y", "-i", src,
            "-vf", f"scale={target[0]}:{target[1]}:force_original_aspect_ratio=decrease,"
                   f"pad={target[0]}:{target[1]}:(ow-iw)/2:(oh-ih)/2,fps={cfg.fps}",
            "-c:v", "libx264", "-crf", "20", "-preset", "medium",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-pix_fmt", "yuv420p", norm,
        ])
        pieces.append(norm)

    if len(pieces) == 1:
        return pieces[0]

    listfile = os.path.join(cfg.work, "concat.txt")
    Path(listfile).write_text("".join(f"file '{p}'\n" for p in pieces))
    branded = os.path.join(cfg.work, "branded.mp4")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", listfile,
         "-c", "copy", branded])
    return branded


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def main():
    p = argparse.ArgumentParser(description="Enhance a NotebookLM video for YouTube.")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--revoice", action="store_true", help="regenerate narration via TTS")
    p.add_argument("--voice-id", help="ElevenLabs voice id (for --revoice)")
    p.add_argument("--no-captions", action="store_true")
    p.add_argument("--zoom", action="store_true", help="add subtle continuous zoom")
    p.add_argument("--vertical", action="store_true", help="9:16 reframe for Shorts")
    p.add_argument("--music", help="background music file")
    p.add_argument("--music-volume", type=float, default=0.08)
    p.add_argument("--intro", help="intro clip to prepend")
    p.add_argument("--outro", help="outro clip to append")
    p.add_argument("--whisper-model", default="small")
    p.add_argument("--language", help="force language, e.g. 'hi' for Hindi")
    p.add_argument("--font", default="Arial")
    p.add_argument("--font-size", type=int, default=64)
    args = p.parse_args()

    if not os.path.isfile(args.input):
        sys.exit(f"Input not found: {args.input}")

    workdir = tempfile.mkdtemp(prefix="enhance_")
    src_w, src_h = ffprobe_resolution(args.input)

    cfg = Config(
        input=args.input, output=args.output, work=workdir,
        width=src_w, height=src_h,
        revoice=args.revoice, captions=not args.no_captions,
        zoom=args.zoom, vertical=args.vertical,
        music=args.music, music_volume=args.music_volume,
        intro=args.intro, outro=args.outro,
        whisper_model=args.whisper_model, language=args.language,
        voice_id=args.voice_id, caption_font=args.font, caption_size=args.font_size,
    )

    # 1. transcribe (needed for captions and/or re-voicing)
    segments = []
    if cfg.captions or cfg.revoice:
        segments = transcribe(cfg)

    # 2. audio
    audio = revoice(segments, cfg) if cfg.revoice else extract_audio(cfg)

    # 3. captions
    ass = build_captions(segments, cfg, cfg.vertical) if cfg.captions else None

    # 4-6. render core
    core = os.path.join(workdir, "core.mp4")
    cfg_core = cfg
    cfg_core.output = core
    render(cfg_core, audio, ass)

    # 7. branding
    final = add_branding(cfg, core) if (cfg.intro or cfg.outro) else core
    if final != args.output:
        run(["ffmpeg", "-y", "-i", final, "-c", "copy", args.output])

    # 8. metadata dump for titles/description/SEO
    meta = {
        "source": args.input,
        "duration_sec": round(ffprobe_duration(args.output), 2),
        "transcript": " ".join(s["text"] for s in segments),
        "segments": segments,
    }
    meta_path = Path(args.output).with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"DONE -> {args.output}")
    log(f"transcript/metadata -> {meta_path}")


if __name__ == "__main__":
    main()

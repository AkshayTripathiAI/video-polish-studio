# video-polish-studio

Turn a raw NotebookLM (or any slideshow-style) video into an edited, upload-ready YouTube video.
Two standalone ffmpeg-based scripts:

| Script | What it does |
|---|---|
| `dynamic_visuals.py` | Rebuilds only the picture: scene detection, per-slide zoom/pan, short transitions, colour grade, logo cover + channel logo. Audio stays untouched and in sync. |
| `enhance_video.py` | Production layer: faster-whisper transcript, optional re-voice with an ElevenLabs voice clone (time-fitted to the original timeline), burned-in captions, zoom, music bed, 9:16 Shorts reframe, intro/outro, transcript JSON for SEO. |

## Requirements

- Python 3 with the packages in `requirements.txt` (`faster-whisper`, `pydub`, `requests`)
- `ffmpeg` and `ffprobe` on your `PATH` (both scripts call them directly); burned-in captions need an ffmpeg build with libass
- `ELEVENLABS_API_KEY` environment variable, only for `enhance_video.py --revoice` (together with `--voice-id`). No other API keys are used.
- faster-whisper downloads the selected Whisper model (`--whisper-model`, default `small`) on first run

`dynamic_visuals.py` needs only ffmpeg; it has no Python package dependencies.

## Setup

```bash
# system dependency
sudo apt-get install ffmpeg        # macOS: brew install ffmpeg · Windows: winget install ffmpeg

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

```bash
# motion + grade, cover the NotebookLM logo, add channel logo
python dynamic_visuals.py input.mp4 -o dynamic.mp4 --logo assets/c_logo.jpg --cover 40:30:230:70

# captions + zoom + music (keep original voice)
python enhance_video.py input.mp4 -o final.mp4 --zoom --music bed.mp3 --language hi

# re-voice with your ElevenLabs clone
export ELEVENLABS_API_KEY=...      # never commit this
python enhance_video.py input.mp4 -o final.mp4 --revoice --voice-id YOUR_VOICE_ID --zoom

# vertical Short with branding
python enhance_video.py input.mp4 -o short.mp4 --vertical --intro assets/c_intro.mp4
```

Run either script with `-h` for all options.

## Project structure

```
dynamic_visuals.py   scene detection, per-slide motion, transitions, grade, logo cover/overlay
enhance_video.py     transcribe, re-voice, captions, zoom, music, 9:16 reframe, intro/outro
requirements.txt     Python dependencies for enhance_video.py
assets/              channel banner, logo, intro clip
examples/            transcript/metadata JSON from a real run (Hindi, "Abhimanyu" episode)
```

`enhance_video.py` also writes `<output>.meta.json` next to the rendered video (source, duration, full transcript and timed segments), the same format as the files in `examples/`.

Rendered videos are git-ignored. Keep outputs outside the repo or in cloud storage.

## Note

If the content is substantially AI-generated, enable YouTube's "Altered or synthetic content" disclosure.
This tool improves production quality; it is not for passing off synthetic content as real.

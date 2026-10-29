# video-polish-studio

Turn a raw NotebookLM (or any slideshow-style) video into an edited, upload-ready YouTube video.
Two standalone ffmpeg-based scripts:

| Script | What it does |
|---|---|
| `dynamic_visuals.py` | Rebuilds only the picture: scene detection, per-slide zoom/pan, short transitions, colour grade, logo cover + channel logo. Audio stays untouched and in sync. |
| `enhance_video.py` | Production layer: faster-whisper transcript, optional re-voice with an ElevenLabs voice clone (time-fitted to the original timeline), burned-in captions, zoom, music bed, 9:16 Shorts reframe, intro/outro, transcript JSON for SEO. |

## Setup

```bash
# system dependency
sudo apt-get install ffmpeg        # macOS: brew install ffmpeg · Windows: winget install ffmpeg

python -m venv .venv && source .venv/bin/activate
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

## Layout

```
assets/     channel banner, logo, intro clip
examples/   transcript/metadata JSON from a real run (Hindi, "Abhimanyu" episode)
```

Rendered videos are git-ignored. Keep outputs outside the repo or in cloud storage.

## Note

If the content is substantially AI-generated, enable YouTube's "Altered or synthetic content" disclosure.
This tool improves production quality; it is not for passing off synthetic content as real.

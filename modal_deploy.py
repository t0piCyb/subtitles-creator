"""
Modal deployment for subtitles-creator heavy compute tasks.

Deploys two CPU functions (no GPU needed for short videos <3min):
- transcribe_video: Whisper transcription (CPU, 4 cores)
- burn_subtitles: FFmpeg subtitle burning (CPU, 4 cores)

Deploy: modal deploy modal_deploy.py
"""

import modal

app = modal.App("subtitles-creator")

# Image with FFmpeg, Whisper, and fonts
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg", "fonts-liberation", "curl", "fontconfig")
    .run_commands(
        "mkdir -p /usr/share/fonts/truetype/custom",
        "curl -L -o /usr/share/fonts/truetype/custom/Slabo27px-Regular.ttf "
        "'https://github.com/google/fonts/raw/main/ofl/slabo27px/Slabo27px-Regular.ttf'",
        "fc-cache -fv",
    )
    .pip_install(
        "faster-whisper==1.0.3",
    )
)

# Cache Whisper models across invocations
whisper_cache = modal.Volume.from_name("subtitles-whisper-cache", create_if_missing=True)


@app.function(
    image=image,
    cpu=4,
    volumes={"/root/.cache": whisper_cache},
    timeout=300,
    memory=4096,
)
def transcribe_video(video_bytes: bytes, filename: str, model_name: str = "base") -> dict:
    """Transcribe video using Whisper on CPU (sufficient for videos <3min)."""
    import re
    import tempfile
    from pathlib import Path
    from faster_whisper import WhisperModel

    # Write video to temp file
    suffix = Path(filename).suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(video_bytes)
        video_path = f.name

    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=4)

        segments, info = model.transcribe(
            video_path,
            language=None,
            word_timestamps=True,
            vad_filter=False,
            beam_size=5,
        )

        raw_words = []
        for segment in segments:
            if not segment.words:
                continue
            for w in segment.words:
                raw_words.append({
                    "text": w.word.strip(),
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                })

        # Merge compound words (French contractions)
        words = _merge_compound_words(raw_words)

        return {
            "language": info.language,
            "language_probability": round(info.language_probability, 2),
            "raw_word_count": len(raw_words),
            "merged_word_count": len(words),
            "subtitles": words,
        }
    finally:
        Path(video_path).unlink(missing_ok=True)


@app.function(
    image=image,
    cpu=4,
    timeout=300,
    memory=4096,
)
def burn_subtitles(video_bytes: bytes, subtitles: list, filename: str) -> bytes:
    """Burn subtitles into video using FFmpeg (CPU, sufficient for videos <3min)."""
    import subprocess
    import tempfile
    from pathlib import Path

    suffix = Path(filename).suffix or ".mp4"

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = Path(tmpdir) / f"input{suffix}"
        ass_path = Path(tmpdir) / "subtitles.ass"
        output_path = Path(tmpdir) / "output.mp4"

        # Write input video
        input_path.write_bytes(video_bytes)

        # Get dimensions
        width, height = _get_video_dimensions(input_path)

        # Generate ASS file
        _generate_ass(subtitles, ass_path, width, height)

        # Run FFmpeg
        cmd = [
            "ffmpeg",
            "-i", str(input_path),
            "-vf", f"ass={ass_path}",
            "-c:a", "copy",
            "-y",
            str(output_path),
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=500)

        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {result.stderr[-500:]}")

        return output_path.read_bytes()


# --- Helpers (duplicated from main.py to run in Modal sandbox) ---

def _merge_compound_words(words: list) -> list:
    """Merge words connected by apostrophes or hyphens."""
    import re

    if not words:
        return words

    merged = [dict(words[0])]

    for w in words[1:]:
        prev = merged[-1]
        text = w["text"]

        should_merge = (
            prev["text"].endswith("'")
            or prev["text"].endswith("-")
            or text == "-"
            or text.startswith("-")
            or text.startswith("'")
        )

        if should_merge:
            prev["text"] = prev["text"] + text
            prev["end"] = w["end"]
        else:
            merged.append(dict(w))

    for m in merged:
        m["text"] = re.sub(r'\s*-\s*', '-', m["text"])
        m["text"] = m["text"].strip("-").strip() or m["text"]

    return [m for m in merged if m["text"].strip()]


def _get_video_dimensions(video_path) -> tuple:
    """Get video width and height using ffprobe."""
    import subprocess

    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0:s=x", str(video_path)],
            capture_output=True, text=True, timeout=10,
        )
        w, h = result.stdout.strip().split("x")
        return int(w), int(h)
    except Exception:
        return 1920, 1080


def _generate_ass(subtitles: list, output_path, width: int = 1920, height: int = 1080) -> None:
    """Generate ASS subtitle file adapted to video orientation."""
    is_vertical = height > width

    if is_vertical:
        play_res_x, play_res_y = 1080, 1920
        font_size = 52
        margin_v = 250
        outline = 3
    else:
        play_res_x, play_res_y = 1920, 1080
        font_size = 72
        margin_v = 80
        outline = 4

    header = f"""[Script Info]
Title: Subtitles
ScriptType: v4.00+
WrapStyle: 0
PlayResX: {play_res_x}
PlayResY: {play_res_y}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Slabo 27px,{font_size},&H0000FFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{outline},2,2,10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for sub in subtitles:
        start = _format_ass_time(sub["start"])
        end = _format_ass_time(sub["end"])
        text = sub["text"].upper()
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(lines))
        f.write("\n")


def _format_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

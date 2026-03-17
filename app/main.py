import os
import re
import uuid
import json
import logging
import subprocess
from pathlib import Path
from typing import List, Dict

from fastapi import FastAPI, File, UploadFile, HTTPException, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="Subtitles Creator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
PROCESSED_DIR = Path("processed")
PROCESSED_DIR.mkdir(exist_ok=True)

# Load Whisper model
MODEL_NAME = os.getenv("WHISPER_MODEL", "base")
logger.info(f"Loading Whisper model: {MODEL_NAME}")
model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8", cpu_threads=4)
logger.info("Model loaded")


@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.post("/api/upload")
async def upload_and_transcribe(file: UploadFile = File(...)):
    """Upload video, transcribe, return editable word-by-word subtitles."""
    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="Le fichier doit être une vidéo")

    file_id = str(uuid.uuid4())
    ext = Path(file.filename).suffix or ".mp4"
    video_path = UPLOAD_DIR / f"{file_id}{ext}"

    try:
        content = await file.read()
        logger.info(f"Upload: {file.filename} ({len(content) / (1024*1024):.1f} MB) -> {file_id}")
        with open(video_path, "wb") as f:
            f.write(content)

        # Transcribe
        segments, info = model.transcribe(
            str(video_path),
            language=None,
            word_timestamps=True,
            vad_filter=False,
            beam_size=5,
        )
        lang = info.language
        logger.info(f"Langue détectée: {lang} ({info.language_probability:.0%})")

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

        # Merge compound words (hyphens, apostrophes)
        words = merge_compound_words(raw_words)

        logger.info(f"Transcription: {len(raw_words)} mots bruts -> {len(words)} après fusion")

        # Save subtitles JSON alongside video
        subs_path = UPLOAD_DIR / f"{file_id}.json"
        with open(subs_path, "w", encoding="utf-8") as f:
            json.dump(words, f, ensure_ascii=False)

        return JSONResponse(content={
            "success": True,
            "file_id": file_id,
            "language": lang,
            "subtitles": words,
        })

    except Exception as e:
        logger.error(f"Erreur upload/transcription: {e}")
        if video_path.exists():
            video_path.unlink()
        raise HTTPException(status_code=500, detail=str(e))


def merge_compound_words(words: List[Dict]) -> List[Dict]:
    """
    Merge words connected by apostrophes or hyphens.
    Whisper often splits "qu'est-ce" into ["qu'", "est", "-", "ce"]
    or "l'homme" into ["l'", "homme"].
    """
    if not words:
        return words

    merged = [dict(words[0])]

    for w in words[1:]:
        prev = merged[-1]
        text = w["text"]

        # Merge if: previous word ends with ' or -, current word is -, or current starts with '
        should_merge = (
            prev["text"].endswith("'")
            or prev["text"].endswith("-")
            or text == "-"
            or text.startswith("-")
            or text.startswith("'")
        )

        if should_merge:
            # If previous ends with ' or - just concat, otherwise add separator
            if prev["text"].endswith("'") or prev["text"].endswith("-"):
                prev["text"] = prev["text"] + text
            elif text.startswith("-") or text.startswith("'") or text == "-":
                prev["text"] = prev["text"] + text
            else:
                prev["text"] = prev["text"] + text
            prev["end"] = w["end"]
        else:
            merged.append(dict(w))

    # Second pass: clean up standalone hyphens that got merged weirdly
    for m in merged:
        m["text"] = re.sub(r'\s*-\s*', '-', m["text"])
        m["text"] = m["text"].strip("-").strip() or m["text"]

    return [m for m in merged if m["text"].strip()]


@app.post("/api/generate")
async def generate_video(payload: dict = Body(...)):
    """Burn subtitles into video with SSE progress streaming."""
    file_id = payload.get("file_id")
    subtitles = payload.get("subtitles")

    if not file_id or not subtitles:
        raise HTTPException(status_code=400, detail="file_id et subtitles requis")

    # Find source video
    video_path = find_video(file_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="Vidéo source introuvable")

    ass_path = UPLOAD_DIR / f"{file_id}.ass"
    output_path = PROCESSED_DIR / f"{file_id}_subtitled.mp4"

    if output_path.exists():
        output_path.unlink()

    # Detect video dimensions for ASS sizing
    width, height = get_video_dimensions(video_path)

    # Generate ASS adapted to video orientation
    generate_ass(subtitles, ass_path, width, height)

    # Get video duration for progress calculation
    duration = get_video_duration(video_path)

    def stream_progress():
        try:
            cmd = [
                "ffmpeg",
                "-i", str(video_path),
                "-vf", f"ass={ass_path}",
                "-c:a", "copy",
                "-y",
                "-progress", "pipe:1",
                "-nostats",
                str(output_path),
            ]
            logger.info(f"FFmpeg: {' '.join(cmd)}")

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )

            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_ms="):
                    try:
                        time_ms = int(line.split("=")[1])
                        if duration > 0:
                            pct = min(99, int((time_ms / 1000 / duration) * 100))
                            yield f"data: {json.dumps({'progress': pct, 'status': 'encoding'})}\n\n"
                    except (ValueError, ZeroDivisionError):
                        pass
                elif line.startswith("progress=end"):
                    break

            proc.wait(timeout=600)

            if proc.returncode != 0:
                stderr = proc.stderr.read()
                logger.error(f"FFmpeg error: {stderr[-500:]}")
                yield f"data: {json.dumps({'error': 'FFmpeg a échoué'})}\n\n"
            else:
                size_mb = output_path.stat().st_size / (1024 * 1024)
                logger.info(f"Vidéo générée: {output_path} ({size_mb:.1f} MB)")
                yield f"data: {json.dumps({'progress': 100, 'status': 'done', 'download_url': f'/api/download/{file_id}', 'size_mb': round(size_mb, 1)})}\n\n"

        except Exception as e:
            logger.error(f"Erreur génération: {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if ass_path.exists():
                ass_path.unlink()

    return StreamingResponse(stream_progress(), media_type="text/event-stream")


@app.get("/api/video/{file_id}")
async def serve_original_video(file_id: str):
    """Serve the original uploaded video for preview."""
    path = find_video(file_id)
    if path:
        return FileResponse(path, media_type="video/mp4")
    raise HTTPException(status_code=404, detail="Vidéo introuvable")


@app.get("/api/download/{file_id}")
async def download_video(file_id: str):
    """Download the processed video with burned subtitles."""
    output_path = PROCESSED_DIR / f"{file_id}_subtitled.mp4"
    if output_path.exists():
        return FileResponse(
            path=output_path,
            media_type="video/mp4",
            filename="video_sous_titree.mp4",
            headers={"Content-Disposition": "attachment; filename=video_sous_titree.mp4"},
        )
    raise HTTPException(status_code=404, detail="Vidéo traitée introuvable")


@app.delete("/api/cleanup/{file_id}")
async def cleanup(file_id: str):
    """Clean up all files for a given file_id."""
    count = 0
    for directory in [UPLOAD_DIR, PROCESSED_DIR]:
        for f in directory.glob(f"{file_id}*"):
            f.unlink()
            count += 1
    return {"deleted": count}


@app.get("/health")
async def health():
    return {"status": "ok", "model": MODEL_NAME}


# --- Helpers ---

def find_video(file_id: str) -> Path | None:
    for ext in [".mp4", ".mov", ".avi", ".webm", ".mkv"]:
        path = UPLOAD_DIR / f"{file_id}{ext}"
        if path.exists():
            return path
    return None


def get_video_duration(video_path: Path) -> float:
    """Get video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=10,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0


def get_video_dimensions(video_path: Path) -> tuple[int, int]:
    """Get video width and height using ffprobe."""
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


def generate_ass(subtitles: List[Dict], output_path: Path, width: int = 1920, height: int = 1080) -> None:
    """Generate ASS subtitle file adapted to video orientation."""
    is_vertical = height > width

    if is_vertical:
        # Vertical (9:16): smaller font, lower position, PlayRes matches vertical
        play_res_x, play_res_y = 1080, 1920
        font_size = 52
        margin_v = 250
        outline = 3
    else:
        # Horizontal (16:9): standard
        play_res_x, play_res_y = 1920, 1080
        font_size = 72
        margin_v = 80
        outline = 4

    # ASS color format: &HAABBGGRR — Yellow = &H0000FFFF
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
        start = format_ass_time(sub["start"])
        end = format_ass_time(sub["end"])
        text = sub["text"].upper()
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(lines))
        f.write("\n")

    orientation = "vertical" if is_vertical else "horizontal"
    logger.info(f"ASS généré: {len(lines)} entrées ({orientation} {width}x{height}, font={font_size})")


def format_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

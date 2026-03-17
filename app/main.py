import os
import uuid
import json
import logging
import subprocess
from pathlib import Path
from typing import List, Dict

from fastapi import FastAPI, File, UploadFile, HTTPException, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
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

        words = []
        for segment in segments:
            if not segment.words:
                continue
            for w in segment.words:
                words.append({
                    "text": w.word.strip(),
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                })

        logger.info(f"Transcription: {len(words)} mots extraits")

        # Save subtitles JSON alongside video for later use
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
        # Cleanup on error
        if video_path.exists():
            video_path.unlink()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/generate")
async def generate_video(payload: dict = Body(...)):
    """Burn edited subtitles into the video and return download info."""
    file_id = payload.get("file_id")
    subtitles = payload.get("subtitles")

    if not file_id or not subtitles:
        raise HTTPException(status_code=400, detail="file_id et subtitles requis")

    # Find source video
    video_path = None
    for ext in [".mp4", ".mov", ".avi", ".webm", ".mkv"]:
        candidate = UPLOAD_DIR / f"{file_id}{ext}"
        if candidate.exists():
            video_path = candidate
            break

    if not video_path:
        raise HTTPException(status_code=404, detail="Vidéo source introuvable")

    ass_path = UPLOAD_DIR / f"{file_id}.ass"
    output_path = PROCESSED_DIR / f"{file_id}_subtitled.mp4"

    # Remove previous output if regenerating
    if output_path.exists():
        output_path.unlink()

    try:
        # Generate ASS file from edited subtitles
        generate_ass(subtitles, ass_path)

        # Burn subtitles with FFmpeg
        burn_subtitles(video_path, ass_path, output_path)

        return JSONResponse(content={
            "success": True,
            "file_id": file_id,
            "download_url": f"/api/download/{file_id}",
        })

    except Exception as e:
        logger.error(f"Erreur génération: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if ass_path.exists():
            ass_path.unlink()


@app.get("/api/video/{file_id}")
async def serve_original_video(file_id: str):
    """Serve the original uploaded video for preview."""
    for ext in [".mp4", ".mov", ".avi", ".webm", ".mkv"]:
        path = UPLOAD_DIR / f"{file_id}{ext}"
        if path.exists():
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


def generate_ass(subtitles: List[Dict], output_path: Path) -> None:
    """Generate ASS subtitle file with Slabo 27px font, word-by-word display."""
    header = """[Script Info]
Title: Subtitles
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Slabo 27px,72,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,4,2,2,10,10,80,1

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

    logger.info(f"ASS généré: {len(lines)} entrées")


def format_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def burn_subtitles(video_path: Path, ass_path: Path, output_path: Path) -> None:
    """Use FFmpeg to burn ASS subtitles into the video."""
    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-vf", f"ass={ass_path}",
        "-c:a", "copy",
        "-y",
        str(output_path),
    ]
    logger.info(f"FFmpeg: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        logger.error(f"FFmpeg stderr: {result.stderr}")
        raise RuntimeError(f"FFmpeg a échoué: {result.stderr[-500:]}")

    logger.info(f"Vidéo générée: {output_path} ({output_path.stat().st_size / (1024*1024):.1f} MB)")

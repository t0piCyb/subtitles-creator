import os
import re
import uuid
import json
import asyncio
import logging
import subprocess
import threading
from pathlib import Path
from typing import List, Dict

from fastapi import FastAPI, File, UploadFile, HTTPException, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware

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

# Background job progress tracking: {file_id: {progress: int, status: str, error: str|None, size_mb: float|None}}
jobs: Dict[str, Dict] = {}

# Modal mode: offload transcription and video generation to Modal GPU
USE_MODAL = os.getenv("USE_MODAL", "false").lower() in ("true", "1", "yes")
MODEL_NAME = os.getenv("WHISPER_MODEL", "base")

if USE_MODAL:
    logger.info(f"Modal mode enabled — heavy compute offloaded to Modal (model={MODEL_NAME})")
    model = None
else:
    from faster_whisper import WhisperModel
    logger.info(f"Loading Whisper model locally: {MODEL_NAME}")
    model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8", cpu_threads=4)
    logger.info("Model loaded")


@app.get("/", response_class=HTMLResponse)
async def read_root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), headers={"Cache-Control": "no-cache"})


@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    """Upload video, start background transcription, return file_id immediately."""
    if not file.content_type or not file.content_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="Le fichier doit être une vidéo")

    file_id = str(uuid.uuid4())
    ext = Path(file.filename).suffix or ".mp4"
    video_path = UPLOAD_DIR / f"{file_id}{ext}"

    try:
        content = await file.read()
        size_mb = len(content) / (1024 * 1024)
        logger.info(f"Upload: {file.filename} ({size_mb:.1f} MB) -> {file_id}")
        with open(video_path, "wb") as f:
            f.write(content)

        # Start background transcription
        jobs[file_id] = {"progress": 0, "status": "transcribing", "error": None, "size_mb": None}

        if USE_MODAL:
            async def run_modal_transcribe():
                try:
                    from app.modal_client import modal_transcribe
                    jobs[file_id]["progress"] = 10
                    result = await modal_transcribe(content, file.filename, MODEL_NAME)
                    words = result["subtitles"]
                    lang = result["language"]
                    logger.info(f"Modal transcription: {result['raw_word_count']} -> {result['merged_word_count']} mots, lang={lang}")

                    subs_path = UPLOAD_DIR / f"{file_id}.json"
                    with open(subs_path, "w", encoding="utf-8") as sf:
                        json.dump({"language": lang, "subtitles": words}, sf, ensure_ascii=False)

                    jobs[file_id].update(progress=100, status="transcribed")
                except Exception as e:
                    logger.error(f"Erreur Modal transcription: {e}")
                    jobs[file_id].update(status="error", error=str(e))

            asyncio.create_task(run_modal_transcribe())
        else:
            def run_local_transcribe():
                try:
                    jobs[file_id]["progress"] = 10
                    segments, info = model.transcribe(
                        str(video_path),
                        language=None,
                        word_timestamps=True,
                        vad_filter=False,
                        beam_size=5,
                    )
                    lang = info.language
                    logger.info(f"Langue détectée: {lang} ({info.language_probability:.0%})")
                    jobs[file_id]["progress"] = 50

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

                    words = merge_compound_words(raw_words)
                    logger.info(f"Transcription: {len(raw_words)} -> {len(words)} mots")

                    subs_path = UPLOAD_DIR / f"{file_id}.json"
                    with open(subs_path, "w", encoding="utf-8") as sf:
                        json.dump({"language": lang, "subtitles": words}, sf, ensure_ascii=False)

                    jobs[file_id].update(progress=100, status="transcribed")
                except Exception as e:
                    logger.error(f"Erreur transcription: {e}")
                    jobs[file_id].update(status="error", error=str(e))

            threading.Thread(target=run_local_transcribe, daemon=True).start()

        return JSONResponse(content={
            "success": True,
            "file_id": file_id,
        })

    except Exception as e:
        logger.error(f"Erreur upload: {e}")
        if video_path.exists():
            video_path.unlink()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/transcribe-progress/{file_id}")
async def get_transcribe_progress(file_id: str):
    """Poll transcription progress."""
    job = jobs.get(file_id)
    if not job:
        # Check if transcription was already completed (subtitles JSON exists)
        subs_path = UPLOAD_DIR / f"{file_id}.json"
        if subs_path.exists():
            with open(subs_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return JSONResponse(content={
                "progress": 100,
                "status": "transcribed",
                "language": data.get("language", "unknown"),
                "subtitles": data.get("subtitles", data if isinstance(data, list) else []),
            })
        raise HTTPException(status_code=404, detail="Job introuvable")

    result = dict(job)
    if job["status"] == "transcribed":
        subs_path = UPLOAD_DIR / f"{file_id}.json"
        with open(subs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        result["language"] = data.get("language", "unknown")
        result["subtitles"] = data.get("subtitles", data if isinstance(data, list) else [])
    return JSONResponse(content=result)


@app.get("/api/session/{file_id}")
async def get_session(file_id: str):
    """Check if a session still exists and return its state."""
    video_path = find_video(file_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="Session introuvable")

    subs_path = UPLOAD_DIR / f"{file_id}.json"
    output_path = PROCESSED_DIR / f"{file_id}_subtitled.mp4"
    job = jobs.get(file_id)

    result = {"file_id": file_id, "video_exists": True}

    if subs_path.exists():
        with open(subs_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        result["subtitles"] = data.get("subtitles", data if isinstance(data, list) else [])
        result["language"] = data.get("language", "unknown")
        result["transcribed"] = True
    else:
        result["transcribed"] = False

    if output_path.exists():
        result["generated"] = True
        result["size_mb"] = round(output_path.stat().st_size / (1024 * 1024), 1)
    else:
        result["generated"] = False

    if job:
        result["job_status"] = job["status"]
        result["job_progress"] = job["progress"]
        result["job_error"] = job.get("error")

    return JSONResponse(content=result)


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
    """Start burning subtitles into video as a background job."""
    file_id = payload.get("file_id")
    subs = payload.get("subtitles")

    if not file_id or not subs:
        raise HTTPException(status_code=400, detail="file_id et subtitles requis")

    video_path = find_video(file_id)
    if not video_path:
        raise HTTPException(status_code=404, detail="Vidéo source introuvable")

    output_path = PROCESSED_DIR / f"{file_id}_subtitled.mp4"

    if output_path.exists():
        output_path.unlink()

    jobs.pop(file_id, None)
    jobs[file_id] = {"progress": 0, "status": "encoding", "error": None, "size_mb": None}

    if USE_MODAL:
        async def run_modal_burn():
            try:
                from app.modal_client import modal_burn_subtitles
                video_bytes = video_path.read_bytes()
                jobs[file_id]["progress"] = 10  # Uploading to Modal
                result_bytes = await modal_burn_subtitles(video_bytes, subs, video_path.name)
                output_path.write_bytes(result_bytes)
                size_mb = len(result_bytes) / (1024 * 1024)
                logger.info(f"Modal vidéo générée: {output_path} ({size_mb:.1f} MB)")
                jobs[file_id].update(progress=100, status="done", size_mb=round(size_mb, 1))
            except Exception as e:
                logger.error(f"Erreur Modal génération: {e}")
                jobs[file_id].update(status="error", error=str(e))

        asyncio.create_task(run_modal_burn())
    else:
        ass_path = UPLOAD_DIR / f"{file_id}.ass"
        width, height = get_video_dimensions(video_path)
        generate_ass(subs, ass_path, width, height)
        duration = get_video_duration(video_path)

        def run_ffmpeg():
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
                                jobs[file_id]["progress"] = pct
                        except (ValueError, ZeroDivisionError):
                            pass
                    elif line.startswith("progress=end"):
                        break

                proc.wait(timeout=600)

                if proc.returncode != 0:
                    stderr = proc.stderr.read()
                    logger.error(f"FFmpeg error: {stderr[-500:]}")
                    jobs[file_id].update(status="error", error="FFmpeg a échoué")
                else:
                    size_mb = output_path.stat().st_size / (1024 * 1024)
                    logger.info(f"Vidéo générée: {output_path} ({size_mb:.1f} MB)")
                    jobs[file_id].update(progress=100, status="done", size_mb=round(size_mb, 1))

            except Exception as e:
                logger.error(f"Erreur génération: {e}")
                jobs[file_id].update(status="error", error=str(e))
            finally:
                if ass_path.exists():
                    ass_path.unlink()

        threading.Thread(target=run_ffmpeg, daemon=True).start()

    return JSONResponse(content={"started": True, "file_id": file_id})


@app.get("/api/progress/{file_id}")
async def get_progress(file_id: str):
    """Poll encoding progress for a background job."""
    job = jobs.get(file_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job introuvable")

    result = dict(job)
    if job["status"] == "done":
        result["download_url"] = f"/api/download/{file_id}"
    return JSONResponse(content=result)


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
    jobs.pop(file_id, None)
    count = 0
    for directory in [UPLOAD_DIR, PROCESSED_DIR]:
        for f in directory.glob(f"{file_id}*"):
            f.unlink()
            count += 1
    return {"deleted": count}


@app.get("/api/videos")
async def list_videos():
    """List all uploaded videos with their processing state."""
    videos = []
    seen_ids = set()

    # Scan uploads for video files
    for ext in [".mp4", ".mov", ".avi", ".webm", ".mkv"]:
        for path in UPLOAD_DIR.glob(f"*{ext}"):
            file_id = path.stem
            if file_id in seen_ids:
                continue
            seen_ids.add(file_id)

            subs_path = UPLOAD_DIR / f"{file_id}.json"
            output_path = PROCESSED_DIR / f"{file_id}_subtitled.mp4"
            job = jobs.get(file_id)

            video_info = {
                "file_id": file_id,
                "filename": path.name,
                "size_mb": round(path.stat().st_size / (1024 * 1024), 1),
                "created": path.stat().st_mtime,
                "transcribed": subs_path.exists(),
                "generated": output_path.exists(),
            }

            if output_path.exists():
                video_info["output_size_mb"] = round(output_path.stat().st_size / (1024 * 1024), 1)

            if subs_path.exists():
                try:
                    with open(subs_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    subs = data.get("subtitles", data if isinstance(data, list) else [])
                    video_info["subtitle_count"] = len(subs)
                except Exception:
                    video_info["subtitle_count"] = 0

            if job:
                video_info["job_status"] = job["status"]
                video_info["job_progress"] = job["progress"]

            videos.append(video_info)

    # Sort by creation date, newest first
    videos.sort(key=lambda v: v["created"], reverse=True)
    return JSONResponse(content={"videos": videos})


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
        # Vertical (9:16): raised high to avoid YouTube/TikTok UI overlay
        play_res_x, play_res_y = 1080, 1920
        font_size = 68
        margin_v = 550
        outline = 4
    else:
        # Horizontal (16:9): standard
        play_res_x, play_res_y = 1920, 1080
        font_size = 90
        margin_v = 80
        outline = 5

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
Style: Default,Montserrat,{font_size},&H0000FFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,{outline},2,2,10,10,{margin_v},1

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

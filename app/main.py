import os
import sys
import uuid
import logging
import traceback
import signal
import atexit
import subprocess
from pathlib import Path
from typing import List, Dict
from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from faster_whisper import WhisperModel

# Enable fault handler for native crashes
import faulthandler
faulthandler.enable()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Signal handler for debugging unexpected terminations
def signal_handler(signum, frame):
    logger.critical(f"!!! Received signal {signum} - Process is being terminated !!!")
    logger.critical(f"Signal name: {signal.Signals(signum).name}")
    logger.critical(f"Stack trace: {traceback.format_stack(frame)}")
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(1)

# Register signal handlers
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Override sys.exit to log all exits
original_exit = sys.exit
def logged_exit(status=0):
    logger.critical(f"!!! sys.exit() called with status={status} !!!")
    logger.critical(f"Stack trace:\n{''.join(traceback.format_stack())}")
    sys.stdout.flush()
    sys.stderr.flush()
    original_exit(status)

sys.exit = logged_exit

# Override os._exit to catch hard exits
original_os_exit = os._exit
def logged_os_exit(status):
    logger.critical(f"!!! os._exit() called with status={status} !!!")
    logger.critical(f"Stack trace:\n{''.join(traceback.format_stack())}")
    sys.stdout.flush()
    sys.stderr.flush()
    original_os_exit(status)

os._exit = logged_os_exit

# Exit handler to log when process exits
def exit_handler():
    logger.critical("!!! Process is exiting via atexit handler !!!")
    logger.critical(f"Stack trace:\n{''.join(traceback.format_stack())}")
    sys.stdout.flush()
    sys.stderr.flush()

atexit.register(exit_handler)

# Helper function to log memory usage
def log_memory_usage():
    try:
        import psutil
        process = psutil.Process()
        mem_info = process.memory_info()
        logger.info(f"Memory usage: RSS={mem_info.rss / (1024*1024):.2f} MB, VMS={mem_info.vms / (1024*1024):.2f} MB")
    except ImportError:
        logger.debug("psutil not available, skipping memory logging")
    except Exception as e:
        logger.debug(f"Could not log memory usage: {e}")

app = FastAPI(title="Subtitles Creator")

# Configuration CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Montage du dossier static
app.mount("/static", StaticFiles(directory="static"), name="static")

# Dossiers de travail
UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
PROCESSED_DIR = Path("processed")
PROCESSED_DIR.mkdir(exist_ok=True)

# Charger le modèle Whisper avec faster-whisper
MODEL_NAME = os.getenv("WHISPER_MODEL", "base")
logger.info(f"Loading Whisper model: {MODEL_NAME}")
device = "cpu"  # faster-whisper uses "cpu" or "cuda"
compute_type = "int8"  # Use int8 for better CPU performance
logger.info(f"Using device: {device}, compute_type: {compute_type}")

try:
    # faster-whisper automatically handles model downloading
    model = WhisperModel(MODEL_NAME, device=device, compute_type=compute_type, cpu_threads=4)
    logger.info(f"Model loaded successfully on {device}")
except Exception as e:
    logger.error(f"Failed to load model: {e}")
    logger.error(traceback.format_exc())
    raise


@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main HTML page"""
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.post("/api/transcribe")
async def transcribe_video(file: UploadFile = File(...)):
    """
    Transcribe a video file and return word-level subtitles for TikTok-style display
    """
    logger.info(f"=== Starting transcription request ===")
    logger.info(f"Received file: {file.filename}, content_type: {file.content_type}")

    # Validation du fichier
    if not file.content_type.startswith("video/"):
        logger.error(f"Invalid file type: {file.content_type}")
        raise HTTPException(status_code=400, detail="File must be a video")

    # Sauvegarder le fichier uploadé
    file_id = str(uuid.uuid4())
    file_extension = Path(file.filename).suffix
    video_path = UPLOAD_DIR / f"{file_id}{file_extension}"
    logger.info(f"Generated file_id: {file_id}, saving to: {video_path}")

    try:
        # Écrire le fichier
        logger.info("Reading uploaded file content...")
        content = await file.read()
        file_size = len(content)
        logger.info(f"File size: {file_size / (1024*1024):.2f} MB")

        logger.info(f"Writing file to disk: {video_path}")
        with open(video_path, "wb") as buffer:
            buffer.write(content)
        logger.info("File written successfully")
        log_memory_usage()

        logger.info(f"Starting transcription with model={MODEL_NAME}, device={device}")
        logger.info(f"Video path: {video_path}")
        log_memory_usage()

        # Transcription avec stable-ts
        try:
            logger.info("Calling model.transcribe()...")
            logger.info("About to enter transcription - process should NOT exit after this point")
            sys.stdout.flush()
            sys.stderr.flush()

            # Use threading to monitor the transcription
            import threading
            import time

            transcription_done = threading.Event()
            result_holder = {'result': None, 'error': None}

            def transcribe_worker():
                try:
                    logger.info("Transcription worker thread started")
                    logger.info("Calling model.transcribe with parameters...")
                    # faster-whisper transcribe returns (segments, info) tuple
                    segments, info = model.transcribe(
                        str(video_path),
                        language=None,  # Auto-detect language (French, English, Spanish, etc.)
                        word_timestamps=True,  # Enable word-level timestamps
                        vad_filter=False,  # Disable VAD filtering
                        beam_size=5
                    )
                    logger.info(f"Detected language: {info.language} with probability {info.language_probability:.2f}")

                    # Convert generator to list
                    segments_list = list(segments)
                    logger.info(f"Transcription completed: {len(segments_list)} segments")
                    result_holder['result'] = segments_list
                    logger.info("Transcription worker thread completed")
                except Exception as e:
                    logger.error(f"Exception in transcription worker: {e}")
                    logger.error(f"Exception type: {type(e).__name__}")
                    logger.error(f"Full traceback: {traceback.format_exc()}")
                    result_holder['error'] = e
                finally:
                    transcription_done.set()

            # Start transcription in a separate thread
            worker_thread = threading.Thread(target=transcribe_worker, daemon=False)
            worker_thread.start()
            logger.info("Transcription worker thread launched")

            # Monitor progress
            last_log_time = time.time()
            while worker_thread.is_alive():
                worker_thread.join(timeout=10)  # Check every 10 seconds
                if worker_thread.is_alive():
                    current_time = time.time()
                    if current_time - last_log_time >= 10:
                        logger.info(f"Transcription still in progress... ({int(current_time - last_log_time)}s elapsed)")
                        log_memory_usage()
                        last_log_time = current_time

            logger.info("Transcription worker thread finished, checking result...")

            if result_holder['error']:
                raise result_holder['error']

            segments_list = result_holder['result']
            if segments_list is None:
                raise RuntimeError("Transcription completed but result is None")

            logger.info("Transcription completed successfully")
            log_memory_usage()
            logger.info(f"Number of segments: {len(segments_list)}")
        except Exception as transcribe_error:
            logger.error(f"Error during model.transcribe(): {str(transcribe_error)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

        # Extract word-level timestamps for TikTok-style subtitles
        logger.info("Starting to extract word-level timestamps...")
        try:
            subtitles = extract_word_timestamps(segments_list)
            logger.info(f"Successfully extracted {len(subtitles)} word timestamps")
        except Exception as extract_error:
            logger.error(f"Error during extract_word_timestamps(): {str(extract_error)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

        logger.info("Preparing response...")
        response_data = {
            "success": True,
            "subtitles": subtitles,
            "file_id": file_id
        }
        logger.info(f"=== Transcription completed successfully. Returning {len(subtitles)} subtitles ===")

        return JSONResponse(content=response_data)

    except Exception as e:
        logger.error(f"!!! FATAL ERROR during transcription !!!")
        logger.error(f"Error type: {type(e).__name__}")
        logger.error(f"Error message: {str(e)}")
        logger.error(f"Full traceback:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)}")

    finally:
        # Nettoyage du fichier uploadé
        logger.info("Cleanup: Attempting to delete uploaded file...")
        if video_path.exists():
            try:
                video_path.unlink()
                logger.info(f"Cleanup: Successfully deleted {video_path}")
            except Exception as cleanup_error:
                logger.error(f"Cleanup: Failed to delete {video_path}: {str(cleanup_error)}")
        else:
            logger.warning(f"Cleanup: File {video_path} does not exist")


def extract_word_timestamps(segments_list) -> List[Dict]:
    """
    Extract individual word timestamps from faster-whisper segments for TikTok-style display
    Returns a list of individual words with start, end, and text
    """
    logger.info("extract_word_timestamps: Starting...")
    words = []

    # Extract all words with their timestamps
    logger.info(f"extract_word_timestamps: Processing {len(segments_list)} segments")
    for segment_idx, segment in enumerate(segments_list):
        if not segment.words:
            logger.warning(f"extract_word_timestamps: Segment {segment_idx} has no words")
            continue

        logger.debug(f"extract_word_timestamps: Segment {segment_idx} has {len(segment.words)} words")
        for word in segment.words:
            # faster-whisper word format: .word (text), .start, .end
            words.append({
                "text": word.word.strip(),
                "start": word.start,
                "end": word.end
            })

    logger.info(f"extract_word_timestamps: Total words extracted: {len(words)}")
    return words


def group_words_by_three(segments_list) -> List[Dict]:
    """
    DEPRECATED: Group words by 3 from faster-whisper segments
    Kept for backward compatibility
    Returns a list of subtitle segments with start, end, and text
    """
    logger.info("group_words_by_three: Starting...")
    subtitles = []
    all_words = []

    # Extraire tous les mots avec leurs timestamps
    logger.info(f"group_words_by_three: Processing {len(segments_list)} segments")
    for segment_idx, segment in enumerate(segments_list):
        if not segment.words:
            logger.warning(f"group_words_by_three: Segment {segment_idx} has no words")
            continue

        logger.debug(f"group_words_by_three: Segment {segment_idx} has {len(segment.words)} words")
        for word in segment.words:
            # faster-whisper word format: .word (text), .start, .end
            all_words.append({
                "text": word.word.strip(),
                "start": word.start,
                "end": word.end
            })

    logger.info(f"group_words_by_three: Total words extracted: {len(all_words)}")

    # Regrouper par 3 mots
    for i in range(0, len(all_words), 3):
        group = all_words[i:i+3]
        if group:
            subtitles.append({
                "start": group[0]["start"],
                "end": group[-1]["end"],
                "text": " ".join([w["text"] for w in group])
            })

    logger.info(f"group_words_by_three: Created {len(subtitles)} subtitle groups")
    return subtitles


def generate_ass_subtitles(words: List[Dict], output_path: Path) -> None:
    """
    Generate ASS subtitle file with TikTok-style formatting
    ASS color format: &HAABBGGRR (alpha, blue, green, red)
    """
    logger.info(f"Generating ASS subtitles to {output_path}")

    # ASS file header with TikTok-style formatting
    ass_content = """[Script Info]
Title: TikTok-Style Subtitles
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TikTok,Arial,80,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,10,10,120,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    # Add each word as a subtitle event
    for word in words:
        start_time = format_ass_time(word['start'])
        end_time = format_ass_time(word['end'])
        text = word['text'].upper()  # TikTok style: uppercase

        # Dialogue line format
        ass_content += f"Dialogue: 0,{start_time},{end_time},TikTok,,0,0,0,,{text}\n"

    # Write ASS file
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(ass_content)

    logger.info(f"ASS file generated with {len(words)} words")


def format_ass_time(seconds: float) -> str:
    """
    Convert seconds to ASS time format (H:MM:SS.CS)
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centiseconds = int((seconds % 1) * 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def process_video_with_subtitles(video_path: Path, ass_path: Path, output_path: Path) -> None:
    """
    Use FFmpeg to burn ASS subtitles into the video
    """
    logger.info(f"Processing video with FFmpeg: {video_path} -> {output_path}")

    # FFmpeg command to burn subtitles
    cmd = [
        'ffmpeg',
        '-i', str(video_path),
        '-vf', f"ass={ass_path}",
        '-c:a', 'copy',  # Copy audio without re-encoding
        '-y',  # Overwrite output file
        str(output_path)
    ]

    logger.info(f"Running FFmpeg command: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600  # 10 minute timeout
        )

        if result.returncode != 0:
            logger.error(f"FFmpeg error: {result.stderr}")
            raise RuntimeError(f"FFmpeg processing failed: {result.stderr}")

        logger.info("FFmpeg processing completed successfully")
    except subprocess.TimeoutExpired:
        logger.error("FFmpeg processing timed out")
        raise RuntimeError("Video processing timed out (max 10 minutes)")
    except Exception as e:
        logger.error(f"FFmpeg processing error: {str(e)}")
        raise


@app.post("/api/process-video")
async def process_video(file: UploadFile = File(...)):
    """
    Process a video file: transcribe, generate subtitles, and burn them into the video
    Returns the processed video file ID for download
    """
    logger.info(f"=== Starting video processing request ===")
    logger.info(f"Received file: {file.filename}, content_type: {file.content_type}")

    # Validation
    if not file.content_type.startswith("video/"):
        logger.error(f"Invalid file type: {file.content_type}")
        raise HTTPException(status_code=400, detail="File must be a video")

    file_id = str(uuid.uuid4())
    file_extension = Path(file.filename).suffix
    video_path = UPLOAD_DIR / f"{file_id}{file_extension}"
    ass_path = UPLOAD_DIR / f"{file_id}.ass"
    output_path = PROCESSED_DIR / f"{file_id}_subtitled{file_extension}"

    try:
        # Step 1: Save uploaded video
        logger.info("Step 1: Saving uploaded file...")
        content = await file.read()
        file_size = len(content)
        logger.info(f"File size: {file_size / (1024*1024):.2f} MB")

        with open(video_path, "wb") as buffer:
            buffer.write(content)
        logger.info("File saved successfully")

        # Step 2: Transcribe video
        logger.info("Step 2: Transcribing video...")
        import threading
        import time

        transcription_done = threading.Event()
        result_holder = {'result': None, 'error': None}

        def transcribe_worker():
            try:
                logger.info("Transcription worker started")
                segments, info = model.transcribe(
                    str(video_path),
                    language=None,  # Auto-detect language (French, English, Spanish, etc.)
                    word_timestamps=True,
                    vad_filter=False,
                    beam_size=5
                )
                logger.info(f"Detected language: {info.language} (probability: {info.language_probability:.2f})")
                segments_list = list(segments)
                logger.info(f"Transcription completed: {len(segments_list)} segments")
                result_holder['result'] = segments_list
            except Exception as e:
                logger.error(f"Transcription error: {e}")
                result_holder['error'] = e
            finally:
                transcription_done.set()

        worker_thread = threading.Thread(target=transcribe_worker, daemon=False)
        worker_thread.start()
        worker_thread.join(timeout=300)  # 5 minute timeout

        if result_holder['error']:
            raise result_holder['error']

        segments_list = result_holder['result']
        if not segments_list:
            raise RuntimeError("Transcription failed")

        # Step 3: Extract word timestamps
        logger.info("Step 3: Extracting word timestamps...")
        words = extract_word_timestamps(segments_list)
        logger.info(f"Extracted {len(words)} words")

        if not words:
            raise RuntimeError("No words extracted from transcription")

        # Step 4: Generate ASS subtitle file
        logger.info("Step 4: Generating ASS subtitles...")
        generate_ass_subtitles(words, ass_path)

        # Step 5: Process video with FFmpeg
        logger.info("Step 5: Burning subtitles into video with FFmpeg...")
        process_video_with_subtitles(video_path, ass_path, output_path)

        logger.info(f"=== Video processing completed successfully ===")
        logger.info(f"Output file: {output_path}")

        return JSONResponse(content={
            "success": True,
            "file_id": file_id,
            "word_count": len(words),
            "download_url": f"/api/download/{file_id}"
        })

    except Exception as e:
        logger.error(f"!!! FATAL ERROR during video processing !!!")
        logger.error(f"Error: {str(e)}")
        logger.error(f"Traceback:\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Video processing failed: {str(e)}")

    finally:
        # Cleanup temporary files
        logger.info("Cleanup: Removing temporary files...")
        for temp_file in [video_path, ass_path]:
            if temp_file.exists():
                try:
                    temp_file.unlink()
                    logger.info(f"Deleted: {temp_file}")
                except Exception as e:
                    logger.error(f"Failed to delete {temp_file}: {e}")


@app.get("/api/download/{file_id}")
async def download_video(file_id: str):
    """
    Download the processed video with burned-in subtitles
    """
    logger.info(f"Download request for file_id: {file_id}")

    # Find the processed file (try common video extensions)
    for ext in ['.mp4', '.mov', '.avi', '.webm']:
        processed_file = PROCESSED_DIR / f"{file_id}_subtitled{ext}"
        if processed_file.exists():
            logger.info(f"Serving file: {processed_file}")
            return FileResponse(
                path=processed_file,
                media_type="video/mp4",
                filename=f"subtitled_video{ext}",
                headers={"Content-Disposition": f"attachment; filename=subtitled_video{ext}"}
            )

    logger.error(f"Processed file not found for file_id: {file_id}")
    raise HTTPException(status_code=404, detail="Processed video not found")


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "model": MODEL_NAME, "device": device}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

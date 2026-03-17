"""
Client for calling Modal functions remotely from the VPS.

Usage:
    from app.modal_client import modal_transcribe, modal_burn_subtitles
"""

import logging
import modal

logger = logging.getLogger(__name__)

APP_NAME = "subtitles-creator"


async def modal_transcribe(video_bytes: bytes, filename: str, model_name: str = "base") -> dict:
    """
    Call Modal transcribe_video function remotely (async).

    Returns: {language, language_probability, raw_word_count, merged_word_count, subtitles}
    """
    logger.info(f"Modal transcribe: {filename} ({len(video_bytes) / (1024*1024):.1f} MB), model={model_name}")
    fn = modal.Function.from_name(APP_NAME, "transcribe_video")
    result = await fn.remote.aio(video_bytes=video_bytes, filename=filename, model_name=model_name)
    logger.info(f"Modal transcribe done: {result['merged_word_count']} words, lang={result['language']}")
    return result


async def modal_burn_subtitles(video_bytes: bytes, subtitles: list, filename: str) -> bytes:
    """
    Call Modal burn_subtitles function remotely (async).

    Returns: processed video bytes
    """
    logger.info(f"Modal burn: {filename} ({len(video_bytes) / (1024*1024):.1f} MB), {len(subtitles)} subs")
    fn = modal.Function.from_name(APP_NAME, "burn_subtitles")
    result = await fn.remote.aio(video_bytes=video_bytes, subtitles=subtitles, filename=filename)
    logger.info(f"Modal burn done: {len(result) / (1024*1024):.1f} MB output")
    return result

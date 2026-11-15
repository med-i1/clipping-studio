"""Viral clip discovery pipeline for Clipping Studio.

The package is intentionally dependency-light: it uses the existing
yt-dlp/ffmpeg tools plus the Python standard library. Optional providers
(OpenAI-compatible APIs, faster-whisper) are loaded only when configured.
"""

__version__ = "1.0.0"

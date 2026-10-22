#
# snapWONDERS MCP — local Model Context Protocol server
#
# Copyright (c) 2026 Kenneth Springer @ snapWONDERS. MIT Licensed — see LICENSE.
# The MIT licence covers this server only; the snapWONDERS API it calls is proprietary.
#
# Author: Kenneth Springer @ snapWONDERS <kenneth@snapwonders.com> (https://kennethbspringer.au)
#

"""A snapWONDERS MCP server that runs on the user's own machine.

**Why this exists.** snapWONDERS already has a remote MCP server at
``https://snapwonders.com/mcp``. It works, and for orchestration it is the right thing. But it
runs on our servers, so it cannot see the user's files — and MCP tool calls carry JSON, not
bytes. An assistant with a photo on the desktop could create a session and then have nowhere to
put the file.

This server closes that gap by running *where the files are*. Each tool takes a path, reads it
locally, and uploads it over plain HTTPS alongside the MCP conversation. That is the whole
difference, and it is what makes snapWONDERS usable from Claude Desktop, which has no shell for
an assistant to fall back on.

**Why the tools are coarse.** The REST API is a session → upload → job → poll → results
sequence. Exposed one call per step, a model has to hold five-step state and will sometimes get
it wrong. Each tool here is a whole task — "analyse this file" — and the orchestration lives in
Python where it is deterministic. Fewer tools also matters: some clients degrade with long tool
lists, and the remote server already publishes nineteen.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from snapwonders import Client
from snapwonders.exceptions import SnapwondersError

mcp = FastMCP("snapwonders")

_API_KEY_ENV = "SNAPWONDERS_API_KEY"
_BASE_URL_ENV = "SNAPWONDERS_BASE_URL"


def _client() -> Client:
    """Build an API client, failing with something a model can act on.

    The key is read from the environment rather than taken as a tool argument, so it never
    passes through the model's context — a key in a tool call would end up in transcripts and
    logs.
    """
    key = os.environ.get(_API_KEY_ENV, "").strip()
    if not key:
        raise ValueError(
            f"No API key. Set {_API_KEY_ENV} in the MCP server's environment "
            "(get a key at https://snapwonders.com/profile/api-keys). "
            "The user must add it to their client config; you cannot supply it yourself."
        )
    base_url = os.environ.get(_BASE_URL_ENV, "").strip()
    return Client(api_key=key, base_url=base_url) if base_url else Client(api_key=key)


def _resolve(file_path: str) -> Path:
    """Turn a model-supplied path into one we are willing to read.

    Models routinely pass ``~/Pictures/x.jpg`` or a relative path, so both are expanded. The
    file must exist and be a regular file: pointing this at a directory or a device node should
    fail with an explanation, not a stack trace.
    """
    path = Path(os.path.expanduser(file_path)).resolve()
    if not path.exists():
        raise ValueError(f"No such file: {path}")
    if not path.is_file():
        raise ValueError(f"Not a file: {path}")
    return path


def _describe(error: Exception) -> str:
    """Phrase a failure so the model's next move is obvious."""
    return f"{type(error).__name__}: {error}"


# The convert API takes a *different option key* for images and videos, and silently falls back
# to its default when given an unrecognised key or value — so `output_format="webp"` on the wrong
# key produces a JPEG and reports success. These lists mirror ConvertApiService's own constants;
# validating here turns that silent wrong answer into an error the model can act on.
_IMAGE_FORMATS = ("jpeg", "png", "webp", "avif", "heic", "jxl")
_VIDEO_FORMATS = ("mp4h264", "mp4h265", "webm", "mkv", "mov")

# Deliberately generous. A container missing from this list falls into the image branch and
# produces a misleading (but loud) "not an image output format" error — never a silent wrong
# conversion, which is the failure that actually matters here.
_VIDEO_SUFFIXES = frozenset({
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv", ".flv",
    ".3gp", ".3g2", ".ts", ".mts", ".m2ts", ".ogv", ".vob", ".asf", ".rm", ".rmvb", ".divx",
})


def _convert_option(path: Path, output_format: str) -> dict[str, str]:
    """Map a requested format onto the option key the convert API actually reads."""
    fmt = output_format.strip().lower()
    is_video = path.suffix.lower() in _VIDEO_SUFFIXES

    if is_video:
        if fmt not in _VIDEO_FORMATS:
            raise ValueError(f"{output_format!r} is not a video output format. Choose one of: {', '.join(_VIDEO_FORMATS)}")
        return {"video_format": fmt}

    if fmt not in _IMAGE_FORMATS:
        raise ValueError(f"{output_format!r} is not an image output format. Choose one of: {', '.join(_IMAGE_FORMATS)}")
    return {"image_format": fmt}


@mcp.tool()
def analyse_file(file_path: str, face_detection: bool = True, text_detection: bool = True) -> dict[str, Any]:
    """Run forensic analysis on a local image or video and return the per-file verdicts.

    Reports the A–F grade, what the metadata reveals (GPS, device fingerprint, encoder
    signatures), manipulation evidence, and any hidden content detected.

    **A null verdict means no answer, not a negative one.** Fields such as
    ``steganography_suspected`` come back ``null`` when that check produced no result — because it
    was skipped, or could not run on this file. Do not report a file as clean, unmodified or free
    of hidden content on the strength of a null; say the check did not return a verdict.

    Args:
        file_path: Path to a file on this machine. ``~`` and relative paths are fine.
        face_detection: Locate faces and return a count. On by default, matching the API.
        text_detection: Run OCR and return detected text. On by default, matching the API.
    """
    try:
        path = _resolve(file_path)
        job = _client().analyse.run([str(path)], face_detection=face_detection, text_detection=text_detection)
        return {
            "file": path.name,
            "job_uid": job.job_uid,
            # Return everything the analysis produced, not a convenient subset.
            #
            # An earlier version returned only filename/grade/face_count, which was actively
            # dangerous: a model asked "does this photo contain anything hidden?" would see no
            # such field, read that as "nothing found", and tell the user the file is clean.
            # steganography_suspected exists precisely to answer that, and omitting it turned a
            # missing answer into a confident wrong one. text_detection was payable but
            # unreadable for the same reason.
            "items": [
                {
                    "filename": item.filename,
                    "grade": item.grade,
                    "face_count": item.face_count,
                    "text_region_count": item.text_region_count,
                    "watermark_flagged": item.watermark_flagged,
                    "steganography_suspected": item.steganography_suspected,
                    # ai_generation, c2pa, camera_fingerprint, findings — the set grows over
                    # time, so pass it through rather than picking keys that will go stale.
                    "verdicts": item.verdicts,
                }
                for item in job.results()
            ],
        }
    except (SnapwondersError, ValueError, OSError, TypeError) as exc:
        return {"error": _describe(exc)}


def _save_results(job: Any, output_dir: str | None, default_beside: Path) -> list[dict[str, Any]]:
    """Download every output to the user's disk and report where it landed.

    Running locally is the entire point of this server, so a job that produces files should
    leave the user holding files — not an asset id they would need a separate HTTP client to
    redeem. Results are written next to the input unless the caller names a directory, which is
    where someone would look for them first.
    """
    target = Path(os.path.expanduser(output_dir)).resolve() if output_dir else default_beside
    target.mkdir(parents=True, exist_ok=True)

    saved = []
    for result in job.results():
        # Trailing separator tells ResultFile.download() this is a directory, so it appends the
        # server-supplied name rather than writing to a file literally called e.g. "out".
        written = result.download(str(target) + os.sep)
        saved.append({"name": result.name, "saved_to": str(written), "bytes": result.file_size})
    return saved


@mcp.tool()
def hide_file(secret_path: str, cover_path: str, password: str, output_dir: str | None = None) -> dict[str, Any]:
    """Hide a file inside a cover image or video, and save the result to disk.

    The output looks like an ordinary photo or video. Recovering the hidden file needs this
    exact password — it is not stored anywhere and cannot be reset, so make sure the user knows
    what it is before running this.

    Args:
        secret_path: The file to conceal.
        cover_path: The image or video that will carry it.
        password: Passphrase required to reveal it later.
        output_dir: Where to write the result. Defaults to alongside the cover file.
    """
    try:
        secret = _resolve(secret_path)
        cover = _resolve(cover_path)
        # The SDK's convention: everything before the last path is a secret, the last is the cover.
        job = _client().stego.hide([str(secret), str(cover)], password=password)
        return {"job_uid": job.job_uid, "saved": _save_results(job, output_dir, cover.parent)}
    except (SnapwondersError, ValueError, OSError, TypeError) as exc:
        return {"error": _describe(exc)}


@mcp.tool()
def reveal_file(stego_path: str, password: str, output_dir: str | None = None) -> dict[str, Any]:
    """Extract content hidden inside an image or video, and save it to disk.

    Args:
        stego_path: The file believed to contain hidden content.
        password: The passphrase it was hidden with.
        output_dir: Where to write what is recovered. Defaults to alongside the input.
    """
    try:
        path = _resolve(stego_path)
        # A single path, not a list — unlike hide(), which takes several. Passing a list here
        # reaches Path([...]) deep inside the uploader and raises TypeError.
        job = _client().stego.reveal(str(path), password=password)
        return {"job_uid": job.job_uid, "saved": _save_results(job, output_dir, path.parent)}
    except (SnapwondersError, ValueError, OSError, TypeError) as exc:
        return {"error": _describe(exc)}


@mcp.tool()
def convert_file(file_path: str, output_format: str, output_dir: str | None = None) -> dict[str, Any]:
    """Convert a local image or video to another format, and save the output to disk.

    Args:
        file_path: The file to convert.
        output_format: Target format. Images: ``jpeg``, ``png``, ``webp``, ``avif``, ``heic``,
            ``jxl``. Videos: ``mp4h264``, ``mp4h265``, ``webm``, ``mkv``, ``mov``.
        output_dir: Where to write the converted file. Defaults to alongside the input.
    """
    try:
        path = _resolve(file_path)
        # Not output_format= — the API reads image_format or video_format depending on the
        # input, and ignores anything else without complaint.
        job = _client().convert.run([str(path)], **_convert_option(path, output_format))
        return {"job_uid": job.job_uid, "saved": _save_results(job, output_dir, path.parent)}
    except (SnapwondersError, ValueError, OSError, TypeError) as exc:
        return {"error": _describe(exc)}


def main() -> None:
    """Entry point for ``snapwonders-mcp`` / ``uvx snapwonders-mcp``.

    stdio transport, because that is what a locally-launched MCP server uses — the client starts
    this process and talks to it over the pipe. Nothing listens on a port.
    """
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

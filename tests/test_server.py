"""What the MCP server guarantees, without touching the live API.

The value of these is mostly in the failure paths. A tool that raises leaves the assistant with
a stack trace it cannot act on; a tool that returns ``{"error": ...}`` leaves it with something
to say to the user. That distinction is the whole reason each tool has a try/except, so it is
what gets pinned here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# In this monorepo the SDK sits alongside as source; in the standalone snapWONDERS-MCP repo it
# is an installed dependency and this path will not exist. Add it only when it does, so the
# same tests run in both places.
_sdk_src = Path(__file__).resolve().parents[2] / "snapwonders-python" / "src"
if _sdk_src.is_dir():
    sys.path.insert(0, str(_sdk_src))

from snapwonders_mcp import server  # noqa: E402


def test_missing_api_key_explains_who_must_fix_it(monkeypatch):
    """The model cannot supply a key itself, so the message has to send it to the user."""
    monkeypatch.delenv("SNAPWONDERS_API_KEY", raising=False)

    with pytest.raises(ValueError) as exc:
        server._client()

    message = str(exc.value)
    assert "SNAPWONDERS_API_KEY" in message
    assert "profile/api-keys" in message, "tell them where to get one"
    assert "cannot supply it yourself" in message, "stop the model inventing a key argument"


def test_blank_api_key_is_treated_as_missing(monkeypatch):
    """An env var set to empty or whitespace is a misconfiguration, not a key."""
    monkeypatch.setenv("SNAPWONDERS_API_KEY", "   ")

    with pytest.raises(ValueError):
        server._client()


def test_paths_are_expanded_and_resolved(tmp_path, monkeypatch):
    """Models routinely pass ~/... or a relative path; both must work."""
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"data")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert server._resolve("photo.jpg") == target.resolve()
    assert server._resolve("~/photo.jpg") == target.resolve()


def test_a_missing_file_is_named_in_the_error(tmp_path):
    with pytest.raises(ValueError, match="No such file"):
        server._resolve(str(tmp_path / "nope.jpg"))


def test_a_directory_is_rejected_rather_than_read(tmp_path):
    """Pointing this at a folder should explain itself, not fail deep inside an upload."""
    with pytest.raises(ValueError, match="Not a file"):
        server._resolve(str(tmp_path))


def test_tools_return_an_error_dict_instead_of_raising(tmp_path, monkeypatch):
    """The contract every tool relies on: failures come back as data the model can read."""
    monkeypatch.delenv("SNAPWONDERS_API_KEY", raising=False)
    sample = tmp_path / "photo.jpg"
    sample.write_bytes(b"data")

    for call in (
        lambda: server.analyse_file(str(sample)),
        lambda: server.convert_file(str(sample), "webp"),
        lambda: server.reveal_file(str(sample), "pw"),
        lambda: server.hide_file(str(sample), str(sample), "pw"),
    ):
        result = call()
        assert "error" in result, "a tool must never raise into the MCP transport"
        assert "SNAPWONDERS_API_KEY" in result["error"]


def test_convert_maps_the_format_onto_the_key_the_api_actually_reads():
    """The API takes image_format or video_format — never output_format.

    Passing an unrecognised key is not an error server-side: it is ignored, the default applies,
    and the job reports success. So asking for WebP would have quietly returned a JPEG.
    """
    assert server._convert_option(Path("photo.png"), "webp") == {"image_format": "webp"}
    assert server._convert_option(Path("clip.mp4"), "webm") == {"video_format": "webm"}
    assert server._convert_option(Path("PHOTO.JPG"), "AVIF") == {"image_format": "avif"}


def test_an_unusable_format_fails_loudly_rather_than_silently_defaulting():
    """Same trap one level down: a valid key with a bad value also falls back to the default."""
    with pytest.raises(ValueError, match="not an image output format"):
        server._convert_option(Path("photo.png"), "mp4h264")

    with pytest.raises(ValueError, match="not a video output format"):
        server._convert_option(Path("clip.mp4"), "webp")

    with pytest.raises(ValueError, match="not an image output format"):
        server._convert_option(Path("photo.png"), "gif")


def test_analyse_defaults_match_the_api_defaults():
    """The API enables face and text detection by default; the tool must not quietly disable them."""
    import inspect

    params = inspect.signature(server.analyse_file).parameters
    assert params["face_detection"].default is True
    assert params["text_detection"].default is True


def test_every_tool_is_registered_with_mcp():
    """Four whole-task tools — not one per REST call. See the module docstring for why.

    Asserts against the MCP registry, not module attributes: a version of this that checked
    ``callable(getattr(server, name))`` passed even with the ``@mcp.tool()`` decorators removed,
    which is the one failure it existed to catch.
    """
    import asyncio

    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert names == {"analyse_file", "hide_file", "reveal_file", "convert_file"}


class _RecordingStego:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def hide(self, *args, **kwargs):
        self.calls.append(("hide", args, kwargs))
        return _StubJob()

    def reveal(self, *args, **kwargs):
        self.calls.append(("reveal", args, kwargs))
        return _StubJob()


class _StubJob:
    job_uid = "job-1"

    def results(self):
        return []


class _StubClient:
    def __init__(self) -> None:
        self.stego = _RecordingStego()


def test_reveal_passes_a_single_path_not_a_list(tmp_path, monkeypatch):
    """The bug this test exists for: ``reveal()`` takes one path, ``hide()`` takes a list.

    Passing a list to reveal reached ``Path([...])`` inside the uploader and raised TypeError —
    which the tool did not catch either, so it broke the "never raise" contract as well. Every
    other test failed at the missing-API-key check before argument shapes were ever exercised,
    so nothing caught it.
    """
    sample = tmp_path / "stego.png"
    sample.write_bytes(b"data")

    client = _StubClient()
    monkeypatch.setattr(server, "_client", lambda: client)

    server.reveal_file(str(sample), "pw", output_dir=str(tmp_path))

    _, args, kwargs = client.stego.calls[0]
    assert isinstance(args[0], str), "reveal() takes a single path, not a list"
    assert kwargs["password"] == "pw"


def test_hide_passes_secrets_then_cover_as_a_list(tmp_path, monkeypatch):
    """hide() genuinely does take a list, and order carries meaning: cover last."""
    secret = tmp_path / "secret.pdf"
    cover = tmp_path / "cover.jpg"
    secret.write_bytes(b"s")
    cover.write_bytes(b"c")

    client = _StubClient()
    monkeypatch.setattr(server, "_client", lambda: client)

    server.hide_file(str(secret), str(cover), "pw", output_dir=str(tmp_path))

    _, args, kwargs = client.stego.calls[0]
    assert isinstance(args[0], list) and len(args[0]) == 2
    assert args[0][-1].endswith("cover.jpg"), "the cover must be last — that is how hide() reads it"
    assert kwargs["password"] == "pw"

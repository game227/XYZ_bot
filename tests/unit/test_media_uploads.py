import pytest

from app.database.models import MediaKind
from app.services import media
from app.services.errors import InvalidInput


@pytest.mark.parametrize(
    ("ext", "head", "ok"),
    [
        ("pdf", b"%PDF-1.7\n", True),
        ("pdf", b"MZ\x90\x00", False),
        ("png", b"\x89PNG\r\n\x1a\n....", True),
        ("jpg", b"\xff\xd8\xff\xe0", True),
        ("mp4", b"\x00\x00\x00\x20ftypisom", True),
        ("mp4", b"<html>", False),
        ("mkv", b"\x1a\x45\xdf\xa3", True),
        ("docx", b"PK\x03\x04", True),
        ("txt", b"hello\x00", False),
    ],
)
def test_sniff(ext, head, ok):
    assert media.sniff_matches(ext, head) is ok


def test_validate_upload_kind_and_size():
    assert media.validate_upload("Dars 1.MP4", 10) == MediaKind.VIDEO
    assert media.validate_upload("notes.pdf", 10) == MediaKind.PDF
    with pytest.raises(InvalidInput):
        media.validate_upload("virus.exe", 10)
    with pytest.raises(InvalidInput):
        media.validate_upload("a.pdf", 10, MediaKind.VIDEO)
    with pytest.raises(InvalidInput):
        media.validate_upload("a.mp4", 0)
    with pytest.raises(InvalidInput):
        media.validate_upload("a.mp4", 10**13)


def test_safe_filename_strips_paths():
    assert media.safe_filename("../../etc/passwd") == "passwd"
    assert media.safe_filename("C:\\Users\\x\\dars.mp4") == "dars.mp4"
    assert media.safe_filename("") == "file"


def test_storage_key_is_unguessable_and_keeps_extension():
    a = media.new_storage_key(MediaKind.VIDEO, "x.mp4")
    b = media.new_storage_key(MediaKind.VIDEO, "x.mp4")
    assert a != b and a.startswith("originals/video/") and a.endswith(".mp4")


def test_upload_part_sizes():
    state = media.UploadState(1, "u", "k.mp4", size=20, part_size=8, lesson_id=None, user_id=1)
    assert state.total_parts == 3
    assert [state.expected_part_size(n) for n in (1, 2, 3)] == [8, 8, 4]
    exact = media.UploadState(1, "u", "k.mp4", size=16, part_size=8, lesson_id=None, user_id=1)
    assert exact.total_parts == 2 and exact.expected_part_size(2) == 8


async def test_put_part_rejects_bad_checksum_and_size(monkeypatch):
    async def fake_upload_part(*args):
        return "etag"

    monkeypatch.setattr(media.storage, "upload_part", fake_upload_part)
    state = media.UploadState(1, "u", "k.pdf", size=12, part_size=8, lesson_id=None, user_id=1)
    first = b"%PDF-1.7" + b"\n"[:0]
    assert len(first) == 8
    import hashlib

    assert await media.put_part(state, 1, first, hashlib.sha256(first).hexdigest()) == "etag"
    with pytest.raises(InvalidInput):
        await media.put_part(state, 1, first, "0" * 64)  # checksum mismatch
    with pytest.raises(InvalidInput):
        await media.put_part(state, 2, b"12", None)  # last part must be 4 bytes
    with pytest.raises(InvalidInput):
        await media.put_part(state, 3, b"1234", None)  # out of range
    bad_head = media.UploadState(1, "u", "k.pdf", size=8, part_size=8, lesson_id=None, user_id=1)
    with pytest.raises(InvalidInput):
        await media.put_part(bad_head, 1, b"MZ......", None)  # not a PDF

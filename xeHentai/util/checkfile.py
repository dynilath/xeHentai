from dataclasses import dataclass
import os
import hashlib

from ..const import RE_IMGHASH


@dataclass
class ImgUrlInfo:
    """Info that can be extracted from an image URL, used for checking file integrity and determining file format.

    Attributes:
        sha1: SHA-1 hash of the image.
        filesize: Size of the image file in bytes.
        width: Width of the image in pixels.
        height: Height of the image in pixels.
        format: Image file format (e.g., ".jpg", ".png").
    """

    sha1: str
    filesize: int
    width: int
    height: int
    format: str


def check_file(path: str, sha1: str) -> bool:
    """Check whether a local file matches the expected SHA-1.

    Args:
        path: Path to the local file.
        sha1: Expected SHA-1 hex digest.

    Returns:
        True if the file exists and SHA-1 matches, else False.
    """
    if not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        data = f.read()
        h = hashlib.sha1()
        h.update(data)
        return h.hexdigest()[: len(sha1)] == sha1


def extract_img_url_info(img_url: str) -> ImgUrlInfo | None:
    """Extract image hash metadata from an image URL.

    Args:
        img_url: Image URL containing encoded hash and dimension fields.

    Returns:
        An ImgUrlInfo instance when parsing succeeds, otherwise None.
    """
    m = RE_IMGHASH.findall(img_url)
    if m:
        sha1, size, width, height, ext = m[0]

        format = ".webp" if ext.lower() == "wbp" else f".{ext.lower()}"
        return ImgUrlInfo(
            sha1=sha1,
            filesize=int(size),
            width=int(width),
            height=int(height),
            format=format,
        )
    return None

def file_hash(path: str, length: int = 10) -> str:
    """Calculate the SHA-1 hash of a file.

    Args:
        path: Path to the file to be hashed.
        length: Length of the hash digest to return (default is 10).
    Returns:
        The SHA-1 hash digest of the file, truncated to the specified length.
    """
    with open(path, "rb") as handle:
        digest = hashlib.sha1()
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()[:length]

from pathlib import Path


def detect_image_ext(path: str) -> str | None:
    """Detect the image file type based on its header bytes."""
    
    with open(path, "rb") as f:
        header = f.read(32)

    # JPEG
    if header.startswith(b"\xFF\xD8\xFF"):
        return ".jpg"

    # PNG
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"

    # GIF
    if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
        return ".gif"

    # BMP
    if header.startswith(b"BM"):
        return ".bmp"

    # WEBP
    if (
        len(header) >= 12
        and header[:4] == b"RIFF"
        and header[8:12] == b"WEBP"
    ):
        return ".webp"

    return None
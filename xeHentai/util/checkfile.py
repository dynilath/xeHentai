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

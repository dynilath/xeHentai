
from dataclasses import dataclass
import os
import hashlib

from ..const import RE_IMGHASH

    
@dataclass
class ImgUrlInfo:
    sha1: str
    filesize: int
    width: int
    height: int
    format: str

def check_file(path:str, sha1:str, size:int)->bool:
    """Check whether a local file matches the expected SHA-1 and file size.

    Args:
        path: Path to the local file.
        sha1: Expected SHA-1 hex digest.
        size: Expected file size in bytes.

    Returns:
        True if the file exists and both size and SHA-1 match, else False.
    """
    if not os.path.exists(path):
        return False
    if os.stat(path).st_size != size:
        return False
    with open(path, 'rb') as f:
        data = f.read()
        h = hashlib.sha1()
        h.update(data)
        return h.hexdigest() == sha1
    

def check_file(path:str, info: ImgUrlInfo)->bool:
    """Check whether a local file matches metadata parsed from image URL info.

    Args:
        path: Path to the local file.
        info: Parsed image metadata containing expected SHA-1 and file size.

    Returns:
        True if the file exists and matches info.filesize and info.sha1, else False.
    """
    if not os.path.exists(path):
        return False
    if os.stat(path).st_size != info.filesize:
        return False
    with open(path, 'rb') as f:
        data = f.read()
        h = hashlib.sha1()
        h.update(data)
        return h.hexdigest() == info.sha1

def extract_img_url_info(img_url:str)->ImgUrlInfo|None:
    """Extract image hash metadata from an image URL.

    Args:
        img_url: Image URL containing encoded hash and dimension fields.

    Returns:
        An ImgUrlInfo instance when parsing succeeds, otherwise None.
    """
    m = RE_IMGHASH.findall(img_url)
    if m:
        sha1, size, width, height, ext = m[0]
        return ImgUrlInfo(sha1=sha1, filesize=int(size), width=int(width), height=int(height), format=ext)
    return None

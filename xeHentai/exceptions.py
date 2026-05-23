#!/usr/bin/env python
# coding:utf-8

from typing import Optional

from .const import ERR_CONNECTION_ERROR, ERR_IMAGE_BROKEN, ERR_KEY_EXPIRED, ERR_QUOTA_EXCEEDED, ERR_SCAN_REGEX_FAILED


class FilterException(Exception):
    """Base exception raised by the flt_quota_check decorator layer.

    Attributes:
        code   (int): The error code constant from const.py (e.g. ERR_QUOTA_EXCEEDED).
        url    (str): The real request URL that triggered the error.
        reason (str): Human-readable description of the specific trigger condition, or None.
    """
    def __init__(self, code: int, url:str, reason:Optional[str]=None):
        Exception.__init__(self, url)
        self.code = code
        self.url = url
        self.reason = reason


class QuotaExceededException(FilterException):
    """Raised when the server reports a bandwidth / image-viewing quota exceeded.

    The reason attribute describes which detection heuristic fired:
    HTTP 509, a known quota-page content-length fingerprint, a 509.gif URL,
    or the 'exceeded your image viewing limits' text in the response body.
    """
    def __init__(self, url, reason=None):
        FilterException.__init__(self, ERR_QUOTA_EXCEEDED, url, reason)


class KeyExpiredException(FilterException):
    """Raised when the server returns HTTP 403, indicating the download key
    has expired and the URL needs to be refreshed before retrying.
    """
    def __init__(self, url):
        FilterException.__init__(self, ERR_KEY_EXPIRED, url)


class ConnectionFilterException(FilterException):
    """Raised for transport-level failures detected inside the filter layer:
    HTTP 600 (synthetic TCP error from _FakeResponse) and HTTP 503
    (backend fetch failed / service unavailable).
    """
    def __init__(self, url):
        FilterException.__init__(self, ERR_CONNECTION_ERROR, url)
        
class ImagePageInfoParseException(FilterException):
    """Raised when the page info parsing fails, e.g. due to site structure change."""
    def __init__(self, url, reason=None):
        FilterException.__init__(self, ERR_SCAN_REGEX_FAILED, url, reason)
        
class ImageFileException(FilterException):
    """Raised when the downloaded image file is broken, e.g. content-length mismatch or zero-length."""
    def __init__(self, url, reason=None):
        FilterException.__init__(self, ERR_IMAGE_BROKEN, url, reason)


class RequestLayerException(Exception):
    """Base exception for request-layer failures outside filter callbacks."""


class RequestRetryExhaustedException(RequestLayerException):
    """Raised when HttpRequest retries are exhausted without a valid response."""
    def __init__(self, url: str, retry: int):
        Exception.__init__(self, message)
        self.url = url
        self.retry = retry
        message = "request retry exhausted: url=%s retry=%d" % (url, retry)
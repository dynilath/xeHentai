#!/usr/bin/env python
# coding:utf-8

from .const import ERR_CONNECTION_ERROR, ERR_KEY_EXPIRED, ERR_QUOTA_EXCEEDED


class FilterException(Exception):
    """Base exception raised by the flt_quota_check decorator layer.

    Attributes:
        code (int): The error code constant from const.py (e.g. ERR_QUOTA_EXCEEDED).
        url  (str): The real request URL that triggered the error.
    """
    def __init__(self, code, url):
        Exception.__init__(self, url)
        self.code = code
        self.url = url


class QuotaExceededException(FilterException):
    """Raised when the server reports a bandwidth / image-viewing quota exceeded
    (HTTP 509, known content-length fingerprints, 509.gif redirect, or the
    'exceeded your image viewing limits' error page).
    """
    def __init__(self, url):
        FilterException.__init__(self, ERR_QUOTA_EXCEEDED, url)


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
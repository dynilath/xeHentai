#!/usr/bin/env python
# coding:utf-8

from dataclasses import dataclass
import traceback
from typing import Iterable, Optional, Set

from .const import (
    ERR_HATH_NOT_FOUND,
    ERR_IMAGE_BROKEN,
    ERR_IMAGE_RESAMPLED,
    ERR_KEY_EXPIRED,
    ERR_NO_PAGEURL_FOUND,
    ERR_QUOTA_EXCEEDED,
    ERR_SCAN_REGEX_FAILED,
    ERR_STREAM_NOT_IMPLEMENTED,
)
from .stage_flow import StageAction


class FilterException(Exception):
    """Base exception raised by the flt_quota_check decorator layer.

    Attributes:
        code   (int): The error code constant from const.py (e.g. ERR_QUOTA_EXCEEDED).
        url    (str): The real request URL that triggered the error.
        reason (str): Human-readable description of the specific trigger condition, or None.
    """

    def __init__(self, code: int, url: str, reason: Optional[str] = None):
        Exception.__init__(self, url)
        self.code = code
        self.url = url
        self.reason = reason


class QuotaExceededException(FilterException):
    """Raised when the server reports a bandwidth / image-viewing quota exceeded.

    The reason attribute describes which detection heuristic fired:
    HTTP 509, a known quota-page content-length fingerprint, a 509.gif URL,
    or the 'exceeded your image viewing limits' text in the response body.
    Should switch to the next proxy and retry the request with reloaded URL.
    The necessary waiting is handled by the proxy control logic.
    """

    def __init__(self, url, reason=None):
        FilterException.__init__(self, ERR_QUOTA_EXCEEDED, url, reason)


class KeyExpiredException(FilterException):
    """Raised when the server returns HTTP 403, indicating the download key
    has expired and the URL needs to be refreshed before retrying.
    """

    def __init__(self, url):
        FilterException.__init__(self, ERR_KEY_EXPIRED, url)


class GalleryDetailPageParseException(FilterException):
    """Raised when the gallery page parsing fails, e.g. due to site structure change.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url, reason=None):
        FilterException.__init__(self, ERR_NO_PAGEURL_FOUND, url, reason)


class ImagePageInfoParseException(FilterException):
    """Raised when the page info parsing fails, e.g. due to site structure change.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url, reason=None):
        FilterException.__init__(self, ERR_SCAN_REGEX_FAILED, url, reason)


class ImageFileException(FilterException):
    """Raised when the downloaded image file is broken, e.g. content-length mismatch or zero-length.
    Should retry the image download with reloaded URL.
    """

    def __init__(self, url, reason=None):
        FilterException.__init__(self, ERR_IMAGE_BROKEN, url, reason)


class ImagePageInvalidException(FilterException):
    """Raised when the image page is not found, e.g. due to deletion or resampling.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url):
        FilterException.__init__(self, ERR_IMAGE_RESAMPLED, url)


class ImageFileNotFoundException(FilterException):
    """Raised when the image file is not found, e.g. due to deletion.
    Should retry the image download with reloaded URL.
    """

    def __init__(self, url):
        FilterException.__init__(self, ERR_HATH_NOT_FOUND, url)


class ImageFileStreamException(FilterException):
    """Raised when the image file is served in an unsupported streaming format.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url, reason=None):
        FilterException.__init__(self, ERR_STREAM_NOT_IMPLEMENTED, url, reason)


class DownloadException(Exception):
    """Base exception for download failures that are not directly related to filter checks."""

    def __init__(self, url, message=None):
        Exception.__init__(self, message or url)
        self.url = url


class DownloadConnectionException(DownloadException):
    """Raised when a download request fails due to a connection error, e.g. network failure or proxy error.
    Should retry the download with reloaded URL.
    """

    def __init__(self, url, reason=None):
        message = "connection error during download: %s, reason: %s" % (url, reason)
        DownloadException.__init__(self, url, message)


class DownloadLengthMismatchException(DownloadException):
    """Raised when the downloaded file's content length does not match the expected length.
    Should retry the download with reloaded URL.
    """

    def __init__(self, url, expected_length, actual_length):
        message = "downloaded file length mismatch: %s, expected: %d, actual: %d" % (
            url,
            expected_length,
            actual_length,
        )
        DownloadException.__init__(self, url, message)


class RequestLayerException(Exception):
    """Base exception for request-layer failures outside filter callbacks."""

    def __init__(self, url: str, message: Optional[str] = None):
        Exception.__init__(self, message or url)
        self.url = url


class RequestInvalidURLException(RequestLayerException):
    """Raised when the request URL is invalid or empty.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url: str):
        RequestLayerException.__init__(self, url, "invalid request URL: %s" % url)


class RequestRetryExhaustedException(RequestLayerException):
    """Raised when HttpRequest retries are exhausted without a valid response.
    If it was during image download or individual page crawl, should retry the operation with reloaded URL.
    Otherwise, should abort the entire gallery crawl.
    """

    def __init__(self, url: str, retry: int, last_ex: Optional[Exception] = None):
        self.retry = retry
        self.last_ex = last_ex
        message = "request retry exhausted: url=%s retry=%d last_ex=%s" % (
            url,
            retry,
            last_ex,
        )
        RequestLayerException.__init__(self, url, message)


@dataclass
class ExceptionPolicy:
    action: StageAction
    delay: float = 0.0
    fail_detail: Optional[str] = None


def map_exception_policy(
    stage: str, ex: Exception, ignored_errors: Optional[Iterable[int]] = None
) -> ExceptionPolicy:
    ignored: Set[int] = set(ignored_errors or ())

    if isinstance(ex, FilterException) and ex.code in ignored:
        return ExceptionPolicy(action=StageAction.SKIP)

    if isinstance(ex, QuotaExceededException):
        return ExceptionPolicy(action=StageAction.RETRY, delay=1.0)

    if isinstance(ex, KeyExpiredException):
        if stage in ("scan_img", "download_img"):
            return ExceptionPolicy(action=StageAction.PIPELINE_RETRY)
        return ExceptionPolicy(action=StageAction.RETRY, delay=0.5)

    if isinstance(
        ex,
        (
            ImageFileException,
            DownloadConnectionException,
            DownloadLengthMismatchException,
        ),
    ):
        return ExceptionPolicy(action=StageAction.PIPELINE_RETRY, delay=1.0)

    if isinstance(ex, ImageFileNotFoundException):
        return ExceptionPolicy(action=StageAction.PIPELINE_RETRY)

    if isinstance(ex, RequestRetryExhaustedException):
        if stage in ("scan_img", "download_img"):
            return ExceptionPolicy(action=StageAction.PIPELINE_RETRY, delay=1.0)
        return ExceptionPolicy(
            action=StageAction.FAIL, fail_detail=traceback.format_exc()
        )

    if isinstance(
        ex,
        (
            RequestInvalidURLException,
            GalleryDetailPageParseException,
            ImagePageInfoParseException,
            ImagePageInvalidException,
            ImageFileStreamException,
        ),
    ):
        return ExceptionPolicy(
            action=StageAction.FAIL, fail_detail=traceback.format_exc()
        )

    return ExceptionPolicy(action=StageAction.FAIL, fail_detail=traceback.format_exc())


if __name__ == "__main__":
    # policy smoke checks
    assert (
        map_exception_policy("scan_img", QuotaExceededException("u")).action
        == StageAction.RETRY
    )
    assert (
        map_exception_policy("scan_img", KeyExpiredException("u")).action
        == StageAction.PIPELINE_RETRY
    )
    assert (
        map_exception_policy("get_meta", KeyExpiredException("u")).action
        == StageAction.RETRY
    )
    assert (
        map_exception_policy("download_img", ImageFileNotFoundException("u")).action
        == StageAction.PIPELINE_RETRY
    )
    assert (
        map_exception_policy("scan_page", RequestRetryExhaustedException("u", 3)).action
        == StageAction.FAIL
    )
    assert (
        map_exception_policy("scan_img", RequestRetryExhaustedException("u", 3)).action
        == StageAction.RETRY
    )

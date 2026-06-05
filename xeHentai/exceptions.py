#!/usr/bin/env python
# coding:utf-8

import traceback
from typing import Never, Optional

from .const import (
    TASK_STATE_FAILED,
    TASK_STATE_ERR_CONNECTION_ERROR,
    TASK_STATE_ERR_GALLERY_NOT_FOUND,
    TASK_STATE_ERR_GALLERY_REMOVED,
    TASK_STATE_ERR_HATH_NOT_FOUND,
    TASK_STATE_ERR_IMAGE_BROKEN,
    TASK_STATE_ERR_IMAGE_RESAMPLED,
    TASK_STATE_ERR_IP_BANNED,
    TASK_STATE_ERR_KEY_EXPIRED,
    TASK_STATE_ERR_NO_PAGEURL_FOUND,
    TASK_STATE_ERR_ONLY_VISIBLE_EXH,
    TASK_STATE_ERR_QUOTA_EXCEEDED,
    TASK_STATE_ERR_SCAN_REGEX_FAILED,
)
from .stage_flow import (
    TaskControlFlow,
    TaskFailed,
    TaskAbort,
    TaskFinished,
    StageRetry,
    StageSkip,
    ScanDownloadRetry,
    TaskRetry,
)


class CrawlerException(Exception):
    """Base exception raised by the flt_quota_check decorator layer.

    Attributes:
        code   (int): A negative TASK_STATE_ERR_* constant encoding the failure reason.
        url    (str): The real request URL that triggered the error.
        reason (str): Human-readable description of the specific trigger condition, or None.
    """

    def __init__(self, code: int, url: str, reason: Optional[str] = None):
        Exception.__init__(self, url)
        self.code = code
        self.url = url
        self.reason = reason


class ParseException(CrawlerException):
    """Raised when the page parsing fails, e.g. due to site structure change.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url, reason=None):
        CrawlerException.__init__(self, TASK_STATE_ERR_CONNECTION_ERROR, url, reason)


class VitalCrawlerException(CrawlerException):
    """Raised when a critical filter check fails, e.g. gallery removed or not found.
    Should abort the entire gallery crawl.
    """

    def __init__(self, code: int, url: str, reason: Optional[str] = None):
        CrawlerException.__init__(self, code, url, reason)


class GalleryRemovedException(VitalCrawlerException):
    """Raised when the gallery is removed, e.g. due to DMCA takedown.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url):
        CrawlerException.__init__(self, TASK_STATE_ERR_GALLERY_REMOVED, url)


class GalleryNotFoundException(VitalCrawlerException):
    """Raised when the gallery is not found, e.g. due to deletion or pending availability.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url):
        CrawlerException.__init__(self, TASK_STATE_ERR_GALLERY_NOT_FOUND, url)


class VisibleOnlyInExhentaiException(CrawlerException):
    """Raised when the gallery is only visible in exhentai.org, e.g. due to content sensitivity.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url):
        CrawlerException.__init__(self, TASK_STATE_ERR_ONLY_VISIBLE_EXH, url)


class IPBannedException(CrawlerException):
    """Raised when the server reports the client's IP address has been temporarily banned.
    Should switch to the next proxy and retry the request with reloaded URL.
    The necessary waiting is handled by the proxy control logic.
    """

    def __init__(self, url, reason=None):
        CrawlerException.__init__(self, TASK_STATE_ERR_IP_BANNED, url, reason)


class MetaDataParseException(ParseException):
    """Raised when the gallery metadata parsing fails, e.g. due to site structure change.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url, reason=None):
        CrawlerException.__init__(self, TASK_STATE_ERR_CONNECTION_ERROR, url, reason)


class QuotaExceededException(CrawlerException):
    """Raised when the server reports a bandwidth / image-viewing quota exceeded.

    The reason attribute describes which detection heuristic fired:
    HTTP 509, a known quota-page content-length fingerprint, a 509.gif URL,
    or the 'exceeded your image viewing limits' text in the response body.
    Should switch to the next proxy and retry the request with reloaded URL.
    The necessary waiting is handled by the proxy control logic.
    """

    def __init__(self, url, reason=None):
        CrawlerException.__init__(self, TASK_STATE_ERR_QUOTA_EXCEEDED, url, reason)


class KeyExpiredException(CrawlerException):
    """Raised when the server returns HTTP 403, indicating the download key
    has expired and the URL needs to be refreshed before retrying.
    """

    def __init__(self, url):
        CrawlerException.__init__(self, TASK_STATE_ERR_KEY_EXPIRED, url)


class GalleryDetailPageParseException(ParseException):
    """Raised when the gallery page parsing fails, e.g. due to site structure change.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url, reason=None):
        CrawlerException.__init__(self, TASK_STATE_ERR_NO_PAGEURL_FOUND, url, reason)


class ImagePageInfoParseException(ParseException):
    """Raised when the page info parsing fails, e.g. due to site structure change.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url, reason=None):
        CrawlerException.__init__(self, TASK_STATE_ERR_SCAN_REGEX_FAILED, url, reason)


class ImageFileException(CrawlerException):
    """Raised when the downloaded image file is broken, e.g. content-length mismatch or zero-length.
    Should retry the image download with reloaded URL.
    """

    def __init__(self, url, reason=None):
        CrawlerException.__init__(self, TASK_STATE_ERR_IMAGE_BROKEN, url, reason)


class ImagePageInvalidException(VitalCrawlerException):
    """Raised when the image page is not found, e.g. due to deletion or resampling.
    Should abort the entire gallery crawl.
    """

    def __init__(self, url):
        CrawlerException.__init__(self, TASK_STATE_ERR_IMAGE_RESAMPLED, url)


class ImageFileNotFoundException(CrawlerException):
    """Raised when the image file is not found, e.g. due to deletion.
    Should retry the image download with reloaded URL.
    """

    def __init__(self, url):
        CrawlerException.__init__(self, TASK_STATE_ERR_HATH_NOT_FOUND, url)


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


def raise_for_stage_exception(
    stage: str,
    ex: Exception,
    *,
    result=None,
)->Never:
    """Map an exception directly to a TaskControlFlow subclass and raise it.

    Combines the former two-step ``map_exception_policy`` → ``_raise_mapped_stage_exception``
    pipeline into a single free function, eliminating the StageAction / ExceptionPolicy
    intermediate layer.
    """

    reason = ex.reason if isinstance(ex, CrawlerException) and ex.reason else traceback.format_exc()

    if isinstance(ex, QuotaExceededException):
        raise StageRetry(reason, delay=60, result=result)

    if isinstance(ex, KeyExpiredException):
        if stage in ("scan_img", "download_img"):
            raise ScanDownloadRetry(
                reason, delay=0.5, result=result
            )
        raise StageRetry(reason, delay=0.5, result=result)

    if isinstance(ex, ImageFileException):
        raise ScanDownloadRetry(reason, delay=1.0, result=result)

    if isinstance(ex, ImageFileNotFoundException):
        raise ScanDownloadRetry(reason, result=result)

    if isinstance(ex, RequestRetryExhaustedException):
        if stage in ("scan_img", "download_img"):
            raise ScanDownloadRetry(
                reason, delay=1.0, result=result
            )
        raise TaskRetry(reason, delay=10.0, result=result)

    if isinstance(ex, CrawlerException):
        raise TaskFailed(reason, task_state=ex.code, result=result)

    raise TaskFailed(reason, task_state=TASK_STATE_FAILED, result=result)

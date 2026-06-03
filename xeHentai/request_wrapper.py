from dataclasses import dataclass

import requests
from . import util
from .const import DEFAULT_MAX_REDIRECTS
from .exceptions import RequestInvalidURLException, RequestRetryExhaustedException
from .i18n import i18n
from .proxy import ProxyPool, ProxyPoolDepleted
from .util.logger import Logger

import random
import re
import time
import urllib3
from typing import Callable, Dict, Optional

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
urllib3.util.ssl_.DEFAULT_CIPHERS += ":HIGH:!DH:!aNULL"  # type: ignore


def _is_509gif(content: str) -> bool:
    return content.find("hentai.org/img/509.gif") != -1


def _is_retryable_request_exception(ex: Exception) -> bool:
    # Treat low-level transport/handshake failures as transient and retryable.
    return isinstance(
        ex,
        (
            requests.exceptions.ProxyError,
            requests.exceptions.ConnectTimeout,
            requests.exceptions.ReadTimeout,
            requests.exceptions.SSLError,
            requests.exceptions.ConnectionError,
        ),
    )


@dataclass
class HttpRequestResult:
    response: requests.Response
    content_length: int
    final_url: str


class HttpRequest(object):
    def __init__(
        self, headers: Dict[str, str], logger: Logger, logger_prefix: str = "main"
    ):
        self.session = requests.Session()
        self.session.headers.update(headers)
        self.logger = logger
        self.logger_prefix = logger_prefix

    def request(
        self,
        method,
        url,
        data=None,
        stream=False,
        retry=2,
        timeout=10,
        proxy: Optional[ProxyPool] = None,
        proxy_wait=False,
        logger: Optional[Logger] = None,
        logger_prefix=None,
    ):
        logger = self.logger if logger is None else logger
        logger_prefix = self.logger_prefix if logger_prefix is None else logger_prefix
        retry_count = 0
        url_history = [url]
        last_ex = None

        r: Optional[requests.Response] = None
        content_length: Optional[int] = None
        final_url: Optional[str] = None

        while retry_count < retry:
            proxy_control = None
            current_url = url_history[-1]

            if not current_url:
                raise RequestInvalidURLException(url=current_url)

            try:
                if proxy:
                    f, proxy_control = proxy.proxied_request(
                        self.session, wait=proxy_wait
                    )
                else:
                    f: Callable[..., requests.Response] = self.session.request

                r = f(
                    method,
                    current_url,
                    allow_redirects=False,
                    data=data,
                    timeout=timeout,
                    stream=stream,
                    verify=False,
                )

                if not stream:
                    content = r.content

            except ProxyPoolDepleted as ex:
                last_ex = ex
                if proxy_control is not None:
                    proxy_control.fail()
                logger.debug(
                    "%s %s %s: %s" % (logger_prefix, method, current_url, str(ex))
                )
                time.sleep(random.random() + 0.618)
                retry_count += 1
                continue
            except requests.RequestException as ex:
                logger.debug(
                    "%s %s %s: %s" % (logger_prefix, method, current_url, str(ex))
                )
                if _is_retryable_request_exception(ex):
                    last_ex = ex
                    if proxy_control is not None:
                        proxy_control.fail()
                    time.sleep(random.random() + 0.618)
                    retry_count += 1
                    continue

                # Non-transport request exceptions should exit immediately.
                raise

            _content_length = r.headers.get("content-length")
            if _content_length is not None:
                content_length = int(_content_length)
            elif not stream:
                content_length = len(r.content)
            else:
                content_length = 0

            logger.debug(
                "%s %s %s %d %d"
                % (logger_prefix, method, current_url, r.status_code, content_length)
            )

            if 300 < r.status_code < 400:
                _new_url = r.headers.get("location")
                if _new_url:
                    url_history.append(_new_url)
                    if len(url_history) > DEFAULT_MAX_REDIRECTS:
                        raise requests.TooManyRedirects(
                            "too many redirects: %s" % url_history[0]
                        )
                    continue

            if r.status_code == 503:
                retry_count += 1
                continue

            if proxy_control is not None:
                if content_length < 1024 and re.search(
                    r"Your IP address has been temporarily banned", r.text
                ):
                    _t = util.parse_human_time(r.text)
                    logger.info(i18n.PROXY_DISABLE_BANNED % _t)
                    proxy_control.cooldown(seconds=_t)
                    retry_count += 1
                    continue
                elif content_length < 1024 and _is_509gif(r.text):
                    proxy_control.cooldown(seconds=3600)
                    retry_count += 1
                    continue
                elif content_length == 0 and r.status_code == 200:
                    proxy_control.fail()
                    retry_count += 1
                    continue

                proxy_control.success()

            r.encoding = "utf-8"
            final_url = r.url
            return HttpRequestResult(
                response=r, content_length=content_length, final_url=final_url
            )

        raise RequestRetryExhaustedException(
            url=url_history[0], retry=retry, last_ex=last_ex
        )

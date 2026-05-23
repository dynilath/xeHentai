#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

from typing import Callable,Optional

from xeHentai.task import Task

from .exceptions import FilterException, RequestRetryExhaustedException
from .proxy import ProxyPool, ProxyPoolDepleted, ProxyPoolUnavailable
from .i18n import i18n
from .const import *
from . import util
from threading import Thread, RLock
import traceback
from requests.adapters import HTTPAdapter
import re
import math
import time
import random
import requests
import urllib3

requests.packages.urllib3.disable_warnings()
requests.packages.urllib3.util.ssl_.DEFAULT_CIPHERS += ':HIGH:!DH:!aNULL'
try:
    requests.packages.urllib3.contrib.pyopenssl.util.ssl_.DEFAULT_CIPHERS += ':HIGH:!DH:!aNULL'
except AttributeError:
    # no pyopenssl support used / needed / available
    pass

from queue import Queue, Empty
from urllib.parse import urlparse, urlunparse

# pinfo = {'http':'socks5://127.0.0.1:16963', 'https':'socks5://127.0.0.1:16963'}


class _FakeResponse(object):
    def __init__(self, url):
        self.status_code = 600
        self.text = ''
        self.url = self._real_url = url
        self.headers = {}


class FallbackIpAdapter(HTTPAdapter):
    def __init__(self, ip_map=FALLBACK_IP_MAP, **kwargs):
        self.ip_map = ip_map
        kwargs.update({'max_retries': 3})
        requests.adapters.HTTPAdapter.__init__(self, **kwargs)

    # override
    def get_connection(self, url, proxies=None):
        if not proxies:
            parsed = urlparse(url)
            _hostname = parsed.hostname
            _scheme = parsed.scheme
            if _hostname in self.ip_map:
                _parsed = list(parsed)
                # alter the hostname
                _hostname = '%s%s' % (self.ip_map[_hostname][0],
                                      (":%d" % parsed.port) if parsed.port else "")
                _scheme = 'https'
            return self.poolmanager.connection_from_host(_hostname, parsed.port, scheme=_scheme,
                                                         pool_kwargs={'assert_hostname': parsed.hostname})
        else:
            # fallback
            return requests.adapters.HTTPAdapter.get_connection(self, url, proxies)

    def add_headers(self, request, **kwargs):
        if not request.headers.get('Host'):
            parsed = urlparse(request.url)
            request.headers['Host'] = parsed.hostname

    def cert_verify(self, conn, url, verify, cert):
        # let the super run verify process
        if url.startswith('http://'):
            url = "https://%s" % url[7:]
        return requests.adapters.HTTPAdapter.cert_verify(self, conn, url, verify, cert)


reg_509gif = re.compile(
    r'<img id="img" src="https://exhentai.org/img/509.gif"')


class HttpReq(object):
    def __init__(self, headers={}, proxy:ProxyPool=None, proxy_policy=None, retry=2, timeout=10, logger=None, tname="main", proxy_wait=True):
        self.session = requests.Session()
        self.session.headers = headers
        # for u in ('forums.e-hentai.org', 'e-hentai.org', 'exhentai.org'):
        #    self.session.mount('http://%s' % u, FallbackIpAdapter())
        #    self.session.mount('https://%s' % u, FallbackIpAdapter())
        # self.session.mount('http://', requests.adapters.HTTPAdapter)
        self.retry = retry
        self.timeout = timeout
        self.proxy = proxy
        self.proxy_policy = proxy_policy
        self.logger = logger
        self.tname = tname
        self.proxy_wait = proxy_wait
        
    
    def request(self, method, url, _filter, suc, fail, data=None, stream_cb: Optional[Callable[...,None]]=None):
        try:
            self.request_base(method, url, _filter, suc, fail, data=data, stream_cb=stream_cb)
        except (ProxyPoolUnavailable, ProxyPoolDepleted):
            raise
        except FilterException as ex:
            if ex.reason:
                self.logger.debug(f"{i18n.THREAD}-{self.tname} filter rejected: {ex.reason}")
            fail((ex.code, ex.url))
        except Exception as ex:
            self.logger.warning(i18n.THREAD_UNCAUGHT_EXCEPTION % (
                self.tname, traceback.format_exc()))
            fail((ERR_CONNECTION_ERROR, url))
        

    def request_base(self, method, url, _filter, suc, fail, data=None, stream_cb: Optional[Callable[...,None]]=None):
        retry = 0
        old_url = str(url)
        url_history = [url]
        while retry < self.retry:
            do_proxy = False
            try:
                headers = {}
                # if proxy_policy is set and match current url, use proxy
                if url and self.proxy and self.proxy_policy and self.proxy_policy.match(url):
                    do_proxy = True
                    f, __proxy_control = self.proxy.proxied_request(
                        self.session, wait=self.proxy_wait)
                else:
                    f: Callable[..., requests.Response] = self.session.request
                r: requests.Response = f(method, url,
                      allow_redirects=False,
                      data=data,
                      timeout=self.timeout,
                      stream=stream_cb is not None,
                      verify=False)
                t = r.text
            except (requests.exceptions.ProxyError, requests.exceptions.ConnectTimeout,
                    requests.exceptions.ReadTimeout, requests.exceptions.SSLError) as ex:
                if do_proxy:
                    __proxy_control.fail()
                    if __proxy_control.state.disabled:
                        self.logger.info("%s-%s proxy %s is disabled for failed too often" %
                                         (i18n.THREAD, self.tname, __proxy_control.addr))

                self.logger.warning("%s-%s %s %s: %s" %
                                    (i18n.THREAD, self.tname, method, url, ex))
                time.sleep(random.random() + 0.618)
            except requests.exceptions.ReadTimeout:
                continue
            except requests.RequestException as ex:
                self.logger.warning("%s-%s %s %s: %s" %
                                    (i18n.THREAD, self.tname, method, url, ex))
                time.sleep(random.random() + 0.618)
            else:
                if r.headers.get('content-length'):
                    r.content_length = int(r.headers.get('content-length'))
                elif not stream_cb:
                    r.content_length = len(r.content)
                else:
                    r.content_length = 0
                self.logger.verbose("%s-%s %s %s %d %d" % (i18n.THREAD,
                                    self.tname, method, url, r.status_code, r.content_length))

                # if it's a redirect, 3xx
                if 300 < r.status_code < 400:
                    _new_url = r.headers.get("location")
                    if _new_url:
                        url_history.append(url)
                        if len(url_history) > DEFAULT_MAX_REDIRECTS:
                            self.logger.warning(
                                "%s-%s %s %s: too many redirects" % (i18n.THREAD, self.tname, method, url))
                            return _filter(_FakeResponse(url_history[0]), suc, fail)
                        url = _new_url
                        continue

                if r.status_code == 503:  # backend fetch failed
                    continue

                # intercept some error to see if we can change IP
                if do_proxy and r.content_length < 1024 and \
                        re.match("Your IP address has been temporarily banned", r.text):
                    _t = util.parse_human_time(r.text)
                    self.logger.warn(i18n.PROXY_DISABLE_BANNED % _t)
                    # fail this proxy immediately and set expire time
                    __proxy_control.cooldown(seconds=_t)
                    self.logger.info("%s-%s proxy %s is banned for %s" %
                                     (i18n.THREAD, self.tname, __proxy_control.addr, _t))
                    continue

                if do_proxy and 'hentai.org/img/509.gif' in r.text:
                    __proxy_control.cooldown(seconds=3600)
                    self.logger.info(
                        "%s-%s proxy %s has exceed band width" % (i18n.THREAD, self.tname, __proxy_control.addr))
                    continue

                if do_proxy and r.ok:
                    __proxy_control.success()
                    self.logger.info("%s-%s proxy %s is very good" %
                                     (i18n.THREAD, self.tname, __proxy_control.addr))

                if r.status_code == 200 and r.content_length == 0:
                    if do_proxy:
                        __proxy_control.fail()
                    continue

                r.encoding = "utf-8"
                # r._text_bytes = r.text.encode("utf-8")
                r._real_url = url_history[-1]

                r.iter_content_cb = stream_cb

                return _filter(r, suc, fail)
            if not do_proxy:
                retry += 1
        return _filter(_FakeResponse(url_history[0]), suc, fail)


class HttpRequest(object):
    def __init__(self, headers=None):
        self.session = requests.Session()
        self.session.headers = headers or {}

    def _log(self, logger, level, msg):
        if logger and hasattr(logger, level):
            getattr(logger, level)(msg)

    def request(self,
                method,
                url,
                data=None,
                stream_cb: Optional[Callable[..., None]] = None,
                retry=2,
                timeout=10,
                proxy: Optional[ProxyPool] = None,
                proxy_policy=None,
                proxy_wait=True,
                logger=None,
                logger_prefix="main",
                allow_redirects=False,
                verify=False):
        retry_count = 0
        url_history = [url]
        last_ex = None

        while retry_count < retry:
            proxy_control = None
            current_url = url_history[-1]
            try:
                if current_url and proxy and proxy_policy and proxy_policy.match(current_url):
                    f, proxy_control = proxy.proxied_request(self.session, wait=proxy_wait)
                else:
                    f = self.session.request

                r: requests.Response = f(
                    method,
                    current_url,
                    allow_redirects=allow_redirects,
                    data=data,
                    timeout=timeout,
                    stream=stream_cb is not None,
                    verify=verify
                )
            except (requests.exceptions.ProxyError,
                    requests.exceptions.ConnectTimeout,
                    requests.exceptions.ReadTimeout,
                    requests.exceptions.SSLError,
                    ProxyPoolDepleted) as ex:
                last_ex = ex
                if proxy_control is not None:
                    proxy_control.fail()
                    if proxy_control.state.disabled:
                        self._log(logger, "info", "%s-%s proxy %s is disabled for failed too often" %
                                  (i18n.THREAD, logger_prefix, proxy_control.addr))
                self._log(logger, "warning", "%s-%s %s %s: %s" %
                          (i18n.THREAD, logger_prefix, method, current_url, ex))
                time.sleep(random.random() + 0.618)
                retry_count += 1
                continue
            except requests.RequestException as ex:
                # Non-timeout/non-proxy request exceptions should exit immediately.
                self._log(logger, "warning", "%s-%s %s %s: %s" %
                          (i18n.THREAD, logger_prefix, method, current_url, ex))
                raise

            if r.headers.get('content-length'):
                r.content_length = int(r.headers.get('content-length'))
            elif not stream_cb:
                r.content_length = len(r.content)
            else:
                r.content_length = 0

            self._log(logger, "verbose", "%s-%s %s %s %d %d" %
                      (i18n.THREAD, logger_prefix, method, current_url, r.status_code, r.content_length))

            if 300 < r.status_code < 400:
                _new_url = r.headers.get("location")
                if _new_url:
                    url_history.append(_new_url)
                    if len(url_history) > DEFAULT_MAX_REDIRECTS:
                        raise requests.TooManyRedirects("too many redirects: %s" % url_history[0])
                    continue

            if r.status_code == 503:
                retry_count += 1
                continue

            if proxy_control is not None and r.content_length < 1024 and \
                    re.match("Your IP address has been temporarily banned", r.text):
                _t = util.parse_human_time(r.text)
                self._log(logger, "warning", i18n.PROXY_DISABLE_BANNED % _t)
                proxy_control.cooldown(seconds=_t)
                self._log(logger, "info", "%s-%s proxy %s is banned for %s" %
                          (i18n.THREAD, logger_prefix, proxy_control.addr, _t))
                retry_count += 1
                continue

            if proxy_control is not None and 'hentai.org/img/509.gif' in r.text:
                proxy_control.cooldown(seconds=3600)
                self._log(logger, "info", "%s-%s proxy %s has exceed band width" %
                          (i18n.THREAD, logger_prefix, proxy_control.addr))
                retry_count += 1
                continue

            if proxy_control is not None and r.ok:
                proxy_control.success()
                self._log(logger, "info", "%s-%s proxy %s is very good" %
                          (i18n.THREAD, logger_prefix, proxy_control.addr))

            if r.status_code == 200 and r.content_length == 0:
                if proxy_control is not None:
                    proxy_control.fail()
                retry_count += 1
                continue

            r.encoding = "utf-8"
            r._real_url = current_url
            r.iter_content_cb = stream_cb
            return r

        if last_ex:
            raise last_ex
        raise RequestRetryExhaustedException(url=url_history[0], retry=retry)


class Monitor(Thread):
    def __init__(self, req, proxy, logger, task: Task, exit_check=None, ignored_errors=[]):
        Thread.__init__(self, name="monitor%s" % task.guid)
        Thread.setDaemon(self, True)
        # the count of votes per error code
        self.vote_result = {}
        # the error code to be ignored
        self.vote_cleared = set().union(ignored_errors)
        self.thread_last_seen = {}
        self.dctlock = RLock()
        self.votelock = RLock()
        self.thread_ref = {}
        self.thread_zombie = set()
        # HttpReq instance
        self.req = req
        # proxy.Pool instance
        self.proxy = proxy
        self.logger = logger
        self.task = task
        self._exit = exit_check if exit_check else lambda x: False
        self._cleaning_up = False

        if os.name == "nt":
            self.set_title = lambda s: os.system("TITLE %s" % s)
        elif os.name == 'posix':
            import sys
            self.set_title = lambda s: sys.stdout.write("\033]2;%s\007" % s)

    def set_vote_ns(self, tnames):
        t = time.time()
        self.thread_last_seen = {k: t for k in tnames}

    def vote(self, tname, code):
        # thread_id, result_code
        self.votelock.acquire()
        if code != ERR_NO_ERROR:
            self.logger.verbose("t-%s vote:%s" % (tname, code))
        if code not in self.vote_result:
            self.vote_result[code] = 1
        else:
            self.vote_result[code] += 1
        self.votelock.release()

    def wrk_keepalive(self, wrk_thread, _exit=False):
        tname = wrk_thread.name
        if tname in self.thread_zombie:
            self.thread_zombie.remove(tname)
        # all image downloaded
        # task is finished or failed
        # monitor is exiting or worker notify its exit
        _ = self.task.meta.finished == self.task.meta.total or \
            self.task.state in (TASK_STATE_FINISHED, TASK_STATE_FAILED) or \
            self._exit("mon") or _exit
        # self.logger.verbose("mon#%s %s ask, %s, %s" % (self.task.guid, tname, _,
        #    self.thread_last_seen))

        if _ or not wrk_thread.is_alive():
            self.dctlock.acquire()
            if tname in self.thread_last_seen:
                del self.thread_last_seen[tname]
            if tname in self.thread_ref:
                del self.thread_ref[tname]
            self.dctlock.release()
        else:
            self.thread_last_seen[tname] = time.time()
            if tname not in self.thread_ref:
                self.thread_ref[tname] = wrk_thread
        return _

    # def _rescan_pages(self):
    #     # not using
    #     # throw away existing page urls
    #     while True:
    #         try:
    #             self.task.page_q.get(False)
    #         except Empty:
    #             break
    #     # put page into task.list_q
    #     [self.task.list_q.put("%s/?p=%d" % (self.task.url, x)
    #         for x in range(1, 1 + int(math.ceil(self.task.meta['total']/20.0))))
    #     ]
    #     print(self.task.list_q.qsize())

    def _check_vote(self):
        if False and ERR_IMAGE_RESAMPLED in self.vote_result and ERR_IMAGE_RESAMPLED not in self.vote_cleared:
            self.logger.warning(i18n.TASK_START_PAGE_RESCAN % self.task.guid)
            self._rescan_pages()
            self.task.meta.has_ori = True
            self.vote_cleared.add(ERR_IMAGE_RESAMPLED)
        elif ERR_QUOTA_EXCEEDED in self.vote_result and \
                ERR_QUOTA_EXCEEDED not in self.vote_cleared and \
                self.vote_result[ERR_QUOTA_EXCEEDED] >= len(self.thread_last_seen):
            self.logger.error(i18n.TASK_STOP_QUOTA_EXCEEDED % self.task.guid)
            self.task.state = TASK_STATE_FAILED

    def run(self):
        CHECK_INTERVAL = 10
        STUCK_INTERVAL = 90
        intv = 0
        self.set_title(i18n.TASK_START % self.task.guid)
        last_change = time.time()
        last_finished = -1
        while len(self.thread_last_seen) > 0:
            intv += 1
            thread_working = 0
            thread_with_pages = 0
            self._check_vote()
            for k in list(self.thread_last_seen.keys()):
                _zombie_threshold = self.thread_ref[k].zombie_threshold if k in self.thread_ref else 30
                if time.time() - self.thread_last_seen[k] > _zombie_threshold:
                    if k in self.thread_ref and self.thread_ref[k].is_alive():
                        self.logger.warning(i18n.THREAD_MAY_BECOME_ZOMBIE % k)
                        self.thread_zombie.add(k)
                    else:
                        self.logger.warning(i18n.THREAD_SWEEP_OUT % k)
                    del self.thread_last_seen[k]

            for t in self.thread_ref.values():
                if t.is_working():
                    thread_working += 1

            if intv == CHECK_INTERVAL:
                _ = "%s %dW/%dR/%dZ, %s %dR/%dD/%dA" % (
                    i18n.THREAD,
                    thread_working, len(self.thread_last_seen), len(
                        self.thread_zombie),
                    i18n.QUEUE,
                    self.task.img_q.qsize() if self.task.img_q else 0,
                    self.task.meta.finished, self.task.meta.total)
                self.logger.info(_)
                self.set_title(_)
                intv = 0
                # if not downloading any new images in 1.5 min, exit
                if last_finished != self.task.meta.finished:
                    last_change = time.time()
                    last_finished = self.task.meta.finished
                else:
                    if time.time() - last_change > STUCK_INTERVAL:
                        self.logger.warning(i18n.TASK_STUCK % self.task.guid)
                        last_change = time.time()
                        CHECK_INTERVAL *= 2
                    if CHECK_INTERVAL > 600:
                        if self.task.state not in (TASK_STATE_PAUSED, TASK_STATE_FINISHED, TASK_STATE_FAILED):
                            if self.task._monitor:
                                self.task._monitor._exit = lambda x: True
                            self.task.state = TASK_STATE_PAUSED
                            CHECK_INTERVAL = 10
                        else:
                            CHECK_INTERVAL = 600
                        break
            time.sleep(0.5)
        if self.task.meta.finished == self.task.meta.total:
            # rename is finished along with downloading process
            self.set_title(i18n.TASK_FINISHED % self.task.guid)
            self.logger.info(i18n.TASK_FINISHED % self.task.guid)
            self.task.state = TASK_STATE_FINISHED
        self.task.cleanup()


if __name__ == '__main__':
    print(HttpReq().request("GET", "https://ipip.tk", lambda x: x, None, None))

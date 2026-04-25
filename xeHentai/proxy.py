#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import re
import time
import random
from collections import deque
from requests.exceptions import ConnectTimeout, ConnectionError, ProxyError, InvalidSchema
from requests.packages.urllib3.exceptions import ProxySchemeUnknown
from . import util
from .const import *

# MAX_FAIL = 256
SUCCESS_THREHOLD = 16


class PoolException(Exception):
    pass


class ProxyControl(object):
    def __init__(self, handle):
        self.handle = handle
        self.good_calls = deque()
        self.bad_calls = deque()
        self.cooldown = 0
        self.disabled = False

    def _clean_calls(self, now):
        while self.good_calls and self.good_calls[0] < now - 3600:
            self.good_calls.popleft()
        while self.bad_calls and self.bad_calls[0] < now - 3600:
            self.bad_calls.popleft()

    def _update_enabled_by_health(self):
        if self.health() < 0.5:
            self.disabled = True

    def health(self):
        if len(self.bad_calls) > 0:
            return (len(self.good_calls) + 32) / (len(self.bad_calls) + len(self.good_calls) + 32)
        else:
            return 1

    def not_good(self):
        self.bad_calls.append(time.time())
        self._clean_calls(time.time())
        self._update_enabled_by_health()

    def good(self):
        self.good_calls.append(time.time())
        self._clean_calls(time.time())
        self._update_enabled_by_health()

    def banned(self, expire=0):
        self.cooldown = time.time() + expire

    def limit_exceeded(self):
        self.cooldown = time.time() + 3600

    def is_disabled(self):
        return self.disabled


class Pool(object):
    # TODO: refactor, a single proxy should have a health and a cooldown
    def __init__(self, logger):
        self.proxies = {}  # Dict[str, ProxyItem]
        self.errors = {}
        self.MAX_FAIL = 16
        self.GOOD_THRESHOLD = 16
        self.logger = logger

    def proxied_request(self, session):
        l_of_proxy = [i for i in self.proxies.values() if not i.is_disabled()]
        if not l_of_proxy:
            raise PoolException("try to use proxy but no proxies avaliable")

        while True:
            timeout = min([i.cooldown for i in l_of_proxy])
            if timeout > time.time():
                self.logger.info("Proxy pool depleted, wait for %s" %
                                 (timeout - time.time()))
                time.sleep(timeout - time.time())
            else:
                break

        t_proxy = random.choice(l_of_proxy)

        return t_proxy.handle(session), t_proxy

    def has_available_proxies(self):
        return len([i for i in self.proxies.keys() if i not in self.disabled]) == 0

    def not_good(self, addr):
        def n(weight=1):
            self.proxies[addr].not_good()
            return addr
        return n

    def limit_exceeded(self, addr):
        def n():
            self.proxies[addr].limit_exceeded()
            return addr
        return n

    def banned(self, addr):
        def n(weight=self.MAX_FAIL, expire=0):
            self.proxies[addr].banned(expire)
            return addr
        return n

    def good(self, addr):
        def n(weight=1):
            self.proxies[addr].good()
            return addr
        return n

    def trace_proxy(self, addr, weight=1, check_func=None, exceptions=[]):
        def _(func):
            def __(*args, **kwargs):
                r = None
                try:
                    r = func(*args, **kwargs)
                except Exception as _ex:
                    raise _ex
                else:
                    if check_func and not check_func(r):
                        self.logger.verbose(
                            "check_func failed for %s" % check_func.__name__)
                        # self.proxies[addr][2] += weight
                    elif check_func:
                        self.logger.verbose(
                            "check_func passed for %s" % check_func.__name__)
                        # self.proxies[addr][1] += weight
                return r
            return __
        return _

    def add_proxy(self, addr):
        if re.match("socks[45][ah]*://([^:^/]+)(\:\d{1,5})*/*$", addr):
            p = socks_proxy(addr, self.trace_proxy)
        elif re.match("https*://([^:^/]+)(\:\d{1,5})*/*$", addr):
            p = http_proxy(addr, self.trace_proxy)
        else:
            raise ValueError("%s is not an acceptable proxy address" % addr)
        # self.proxies[addr] = [p, 0, 0]
        self.proxies[addr] = ProxyControl(p)

    def set_max_fail(self, threhold):
        self.MAX_FAIL = threhold

    def set_good_threshold(self, threhold):
        self.GOOD_THRESHOLD = threhold
        pass


def socks_proxy(addr, trace_proxy):
    proxy_info = {
        'http': addr,
        'https': addr
    }

    def handle(session):
        @trace_proxy(addr, exceptions=[ProxySchemeUnknown, InvalidSchema])
        def f(*args, **kwargs):
            kwargs.update({'proxies': proxy_info})
            return session.request(*args, **kwargs)
        return f
    return handle


def http_proxy(addr, trace_proxy):
    proxy_info = {
        'http': addr,
        'https': addr
    }

    def handle(session):
        @trace_proxy(addr)
        def f(*args, **kwargs):
            kwargs.update({'proxies': proxy_info})
            return session.request(*args, **kwargs)
        return f
    return handle


if __name__ == '__main__':
    import requests
    p = Pool()
    p.add_proxy("sock5://127.0.0.1:16961")
    print(p.proxied_request(requests.Session())(
        "GET", "http://ipip.tk", headers={}, timeout=2).headers)

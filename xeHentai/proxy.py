#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

from dataclasses import dataclass
import math
import re
import time
import random
from typing import Any, Callable, Dict, Optional
import requests
from requests.exceptions import InvalidSchema
from . import util
from .const import *

# MAX_FAIL = 256
SUCCESS_THREHOLD = 16


class ProxyPoolException(Exception):
    def __init__(self, message, retry_after=0.0):
        Exception.__init__(self, message)
        wait_for = 0.0 if retry_after is None else max(0.0, float(retry_after))
        self.retry_after = wait_for
        self.wait_for = wait_for


class ProxyPoolUnavailable(ProxyPoolException):
    def __init__(self, message="try to use proxy but no proxies avaliable"):
        ProxyPoolException.__init__(self, message, retry_after=0.0)


class ProxyPoolDepleted(ProxyPoolException):
    def __init__(self, message="proxy pool depleted", retry_after=0.0):
        ProxyPoolException.__init__(self, message, retry_after=retry_after)

@dataclass
class ProxyState:
    score: float = 1.0
    cooldown_until: float = 0
    disabled: bool = False
    last_update: float = 0
    
DEFAULT_HALF_LIFE = 600
DEFAULT_FAIL_PENALTY = 0.3
DEFAULT_SUCCESS_REWARD = 0.05
DEFAULT_DISABLE_THRESHOLD = 0.15

class ProxyControl(object):    
    def __init__(
            self,
            handle: Callable[[requests.Session], Callable[..., requests.Response]],
            addr: str = "",
            half_life: float = DEFAULT_HALF_LIFE,
            fail_penalty: float = DEFAULT_FAIL_PENALTY,
            success_reward: float = DEFAULT_SUCCESS_REWARD,
            disable_threshold: float = DEFAULT_DISABLE_THRESHOLD):
        
        def clamp(value: float) -> float:
            return min(max(float(value), 0.0), 1.0)
        
        self.handle = handle
        self.addr = addr
        self.half_life = max(1e-3, float(half_life))
        self.fail_penalty = clamp(fail_penalty)
        self.success_reward = clamp(success_reward)
        self.disable_threshold = clamp(disable_threshold)
        self.state = ProxyState(last_update=time.time())

    def import_state(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return

        state = data.get('state', {})

        def _to_float(value, default):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        def _to_bool(value, default):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() in ('1', 'true', 'yes', 'on')
            return default

        self.half_life = max(1e-3, _to_float(data.get('half_life'), self.half_life))
        self.fail_penalty = min(max(_to_float(data.get('fail_penalty'), self.fail_penalty), 0.0), 1.0)
        self.success_reward = min(max(_to_float(data.get('success_reward'), self.success_reward), 0.0), 1.0)
        self.disable_threshold = min(max(_to_float(data.get('disable_threshold'), self.disable_threshold), 0.0), 1.0)

        self.state.score = min(max(_to_float(state.get('score'), self.state.score), 0.0), 1.0)
        self.state.cooldown_until = max(0.0, _to_float(state.get('cooldown_until'), self.state.cooldown_until))
        self.state.disabled = _to_bool(state.get('disabled'), self.state.disabled)
        self.state.last_update = max(0.0, _to_float(state.get('last_update'), self.state.last_update))

    def export_state(self) -> Dict[str, Any]:
        return {
            'half_life': self.half_life,
            'fail_penalty': self.fail_penalty,
            'success_reward': self.success_reward,
            'disable_threshold': self.disable_threshold,
            'state': {
                'score': self.state.score,
                'cooldown_until': self.state.cooldown_until,
                'disabled': self.state.disabled,
                'last_update': self.state.last_update,
            },
        }

    def set_disable_after_failures(self, fail_count):
        fail_count = max(1.0, float(fail_count))
        self.disable_threshold = pow(1 - self.fail_penalty, fail_count)

    def set_good_threshold(self, threshold):
        threshold = max(1.0, float(threshold))
        self.success_reward = min(1.0, 1.0 / threshold)

    def _decay(self):
        now = time.time()
        dt = now - self.state.last_update
        self.state.last_update = now
        
        decay = math.exp(-dt / self.half_life)
        self.state.score = 1 - (1 - self.state.score) * decay
        
    def success(self):
        self._decay()
        self.state.score = min(
            1.0,
            self.state.score + self.success_reward,
        )

    def fail(self):
        self._decay()

        self.state.score *= (1 - self.fail_penalty)

        if self.state.score < self.disable_threshold:
            self.state.disabled = True

    def cooldown(self, seconds):
        self.state.cooldown_until = time.time() + seconds

    def available(self):
        now = time.time()

        if self.state.disabled:
            return False

        if now < self.state.cooldown_until:
            return False

        return True

    @property
    def health(self):
        self._decay()
        return self.state.score


class ProxyPool(object):
    def __init__(self, logger):
        self.proxies: Dict[str, ProxyControl] = {}
        self.logger = logger

    def _enabled_proxies(self):
        return [i for i in self.proxies.values() if not i.state.disabled]

    def _ready_proxies(self):
        now = time.time()
        return [i for i in self._enabled_proxies() if now >= i.state.cooldown_until]

    def next_available_after(self):
        enabled = self._enabled_proxies()
        if len(enabled) == 0:
            return None
        now = time.time()
        return max(0.0, min([i.state.cooldown_until for i in enabled]) - now)

    def wait_until_available(self, check_interval=1.0, exit_check=None):
        while True:
            if exit_check and exit_check():
                return False
            if self.has_available_proxies():
                return True
            wait_for = self.next_available_after()
            if wait_for is None:
                time.sleep(check_interval)
                continue
            # Use short waits so callers can stop promptly.
            time.sleep(min(check_interval, max(wait_for, 0.0)))

    def proxied_request(self, session: requests.Session, wait=True):
        proxies = self._enabled_proxies()
        if len(proxies) == 0:
            raise ProxyPoolUnavailable()

        while True:
            ready = self._ready_proxies()
            if ready:
                weights = [max(0.0, p.health) for p in ready]
                if any(weights):
                    target_proxy = random.choices(ready, weights=weights, k=1)[0]
                else:
                    target_proxy = random.choice(ready)
                return target_proxy.handle(session), target_proxy

            wait_for = self.next_available_after()
            wait_for = 0 if wait_for is None else max(wait_for, 0.0)
            if not wait:
                raise ProxyPoolDepleted(retry_after=wait_for)

            self.logger.info("Proxy pool depleted, wait for %s" % wait_for)
            time.sleep(wait_for if wait_for > 0 else 0.5)

    def has_available_proxies(self):
        return len(self._ready_proxies()) > 0

    def add_proxy(self, addr, state: Optional[Dict[str, Any]] = None):
        if re.match(r"socks[45][ah]*://([^:^/]+)(\:\d{1,5})*/*$", addr):
            p = socks_proxy(addr)
        elif re.match(r"https*://([^:^/]+)(\:\d{1,5})*/*$", addr):
            p = http_proxy(addr)
        else:
            raise ValueError("%s is not an acceptable proxy address" % addr)
        proxy_control = ProxyControl(p, addr=addr)
        proxy_control.import_state(state or {})
        self.proxies[addr] = proxy_control

    def export_store(self) -> Dict[str, Dict[str, Any]]:
        return {addr: control.export_state() for addr, control in self.proxies.items()}


def socks_proxy(addr):
    proxy_info = {
        'http': addr,
        'https': addr
    }

    def handle(session: requests.Session):
        def f(*args, **kwargs):
            kwargs.update({'proxies': proxy_info})
            return session.request(*args, **kwargs)
        return f
    return handle


def http_proxy(addr):
    proxy_info = {
        'http': addr,
        'https': addr
    }

    def handle(session: requests.Session):
        def f(*args, **kwargs):
            kwargs.update({'proxies': proxy_info})
            return session.request(*args, **kwargs)
        return f
    return handle


if __name__ == '__main__':
    import requests
    p = ProxyPool()
    p.add_proxy("sock5://127.0.0.1:16961")
    print(p.proxied_request(requests.Session())(
        "GET", "http://ipip.tk", headers={}, timeout=2).headers)

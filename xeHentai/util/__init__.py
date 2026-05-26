#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import os
import re
import sys
import uuid
import random
import html

from ..const import *

if os.name == 'nt':
    filename_filter = re.compile(r"[|:?\\/*'\"<>]")
else:# assume posix
    filename_filter = re.compile(r"[\/:]")

unichr = chr

def parse_cookie(coostr):
    ret = {}
    for coo in coostr.split(";"):
        coo = coo.strip()
        if coo.lower() in ('secure', 'httponly'):
            continue
        _ = coo.split("=")
        k = _[0]
        v = "=".join(_[1:])
        if k.lower() in ('path', 'expires', 'domain', 'max-age', 'comment'):
            continue
        ret[k] = v
    return ret

def make_cookie(coodict):
    return ";".join(map("=".join, coodict.items()))

def make_ua():
    rrange = lambda a, b, c = 1: c == 1 and random.randrange(a, b) or int(1.0 * random.randrange(a * c, b * c) / c)
    ua = 'Mozilla/%d.0 (Windows NT %d.%d) AppleWebKit/%d (KHTML, like Gecko) Chrome/%d.%d Safari/%d' % (
        rrange(4, 7, 10), rrange(5, 7), rrange(0, 3), rrange(535, 538, 10),
        rrange(21, 27, 10), rrange(0, 9999, 10), rrange(535, 538, 10)
    )
    return ua

def get_proxy_policy(cfg):
    if cfg['proxy_image_only']:
        return RE_URL_IMAGE
    if cfg['proxy_image']:
        return RE_URL_ALL
    return RE_URL_WEBPAGE

def parse_human_time(s):
    rt = 0
    day = re.findall(r'(\d+)\sdays*', s)
    if day:
        rt += 86400 * int(day[0])
    hour = re.findall(r'(\d+)\shours*', s)
    if hour:
        rt += 3600 * int(hour[0])
    minute = re.findall(r'(\d+)\sminutes*', s)
    if minute:
        rt += 60 * int(minute[0])
    else:
        rt += 60
    return rt

def htmlunescape(s:str) -> str:
    return html.unescape(s)

def legalpath(s:str) -> str:
    ret = filename_filter.sub(lambda x:"", s)
    if ret.endswith(".") or ret.endswith(" "):
        ret = ret[:-1] + "_"
    return ret

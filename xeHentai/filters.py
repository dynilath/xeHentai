#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import re

import requests

from xeHentai.request_wrapper import HttpRequestResult
from .util.checkfile import extract_img_url_info
from . import util
from .const import *
from .exceptions import DownloadConnectionException, DownloadLengthMismatchException, GalleryDetailPageParseException, ImageFileException, ImageFileNotFoundException, ImagePageInfoParseException, ImagePageInvalidException, KeyExpiredException, QuotaExceededException
from typing import Callable, List, ParamSpec, Tuple, TypeVar

SUC = 0
FAIL = 1

P = ParamSpec("P")
R = TypeVar("R")

def login_exhentai(r, suc, fail):
    # input login response
    # add cookies if suc; log error fail
    try:
        coo = r.headers.get('set-cookie')
        cooid = re.findall('ipb_member_id=(.*?);', coo)[0]
        coopw = re.findall('ipb_pass_hash=(.*?);', coo)[0]
    except (IndexError, ) as ex:
        errmsg = re.findall('<span class="postcolor">([^<]+)</span>', r.text)
        if errmsg:
            fail(errmsg[0])
        else:
            fail("ex: %s" % ex)
        return FAIL
    else:
        suc({'ipb_member_id': cooid, 'ipb_pass_hash': coopw})
        return SUC


def flt_metadata(r:HttpRequestResult, suc, fail):
    # input index response
    # add gallery meta if suc; return errorcode if fail
    # TODO: catch re exceptions
    if r.response.status_code == 404:
        return fail(ERR_GALLERY_REMOVED)
    if re.match("Gallery not found", r.response.text):
        return fail(ERR_GALLERY_NOT_FOUND)
    if re.match("This gallery is pining for the fjords", r.response.text):
        return fail(ERR_ONLY_VISIBLE_EXH)
    elif re.match("This IP address has been temporarily banned", r.response.text):
        fail(ERR_IP_BANNED)
        return re.findall("The ban expires in (.+)", r.response.text)[0]
    meta = {}

    try:
        title_japanese = util.htmlunescape(
            re.findall('="gj">(.*?)</h1>', r.response.text)[0])
        title_primary = util.htmlunescape(
            re.findall('="gn">(.*?)</h1>', r.response.text)[0])

        # preferred readable naming
        meta['title_japanese'] = title_japanese
        meta['title_primary'] = title_primary

        # backward-compatible aliases
        meta['gjname'] = title_japanese
        meta['gnname'] = title_primary
        # don't assign title now, select by cfg['jpn_title']
        meta['total'] = int(re.findall(
            'Length:</td><td class="gdt2">(\\d+)\\s+page', r.response.text)[0])
        meta['finished'] = 0
        meta['tags'] = re.findall("toggle_tagmenu\\([^)']+'([^']+)'", r.response.text)

        # TODO: parse cookie to calc thumbnail_cnt (tr_2, ts_m)
        _ = re.findall("Showing (\\d+) \\- (\\d+) of ([\\d,]+) images", r.response.text)[0]
        meta['thumbnail_cnt'] = int(_[1]) - int(_[0]) + 1

        meta['newer_versions'] = []
        gnd_block = re.search(r'<div id="gnd">(.+?)</div>', r.response.text, re.DOTALL)
        if gnd_block:
            for _u, _gid, _sethash, _title, _added in re.findall(
                    r'<a href="(https?://(?:e-|ex)hentai\.org/g/(\d+)/([^/"]+)/?)">([^<]+)</a>,\s*added\s*([^<]+)',
                    gnd_block.group(1)):
                meta['newer_versions'].append({
                    'url': util.htmlunescape(_u),
                    'gid': str(_gid),
                    'sethash': str(_sethash),
                    'title': util.htmlunescape(_title),
                    'added': _added.strip(),
                })

        suc(meta)
    except IndexError as e:
        print(r.response.text)
        # return fail(ERR_CONNECTION_ERROR)


def flt_pageurl(r:HttpRequestResult, suc:Callable[[Tuple[str,str,str]], R]):
    # input gallery response
    # result[0]: page url; 
    # result[1]: page id; 
    # result[2]: original file name (may be empty)
    picpage: list[tuple[str, str, str]] = re.findall(
        '<a href="(%s\\/.\\/[a-f0-9]{10}\\/\\d+\\-\\d+)"><div title="Page (\\d+): ([^"]*)"' % RESTR_SITE,
        r.response.text)
    # (page url, page id, original file name)
    if not picpage:
        raise GalleryDetailPageParseException(r.final_url, "can't find image page urls in gallery page")
    for p in picpage:
        suc(p)


def flt_quota_check(func:Callable[[HttpRequestResult, Callable[P,R]], R]):
    def _(r: HttpRequestResult, suc:Callable[P,R]) -> R:
        content_type = r.response.headers.get('content-type', '')
        if r.response.status_code == 403:
            raise KeyExpiredException(r.final_url)
        elif r.response.status_code == 509:
            raise QuotaExceededException(r.final_url, "HTTP 509 bandwidth limit exceeded")
        elif r.content_length in [925, 144, 210, 1009]:
            raise QuotaExceededException(r.final_url, f"quota page content-length fingerprint ({r.content_length} bytes)")
        elif 'hentai.org/img/509.gif' in r.final_url:
            raise QuotaExceededException(r.final_url, "509.gif detected in response URL")
        elif r.content_length < 200 and content_type.startswith('text') and re.search("exceeded your image viewing limits", r.response.text):
                raise QuotaExceededException(r.final_url, "image viewing limits exceeded (text match)")
        else:
            return func(r, suc)
    return _


def flt_imgurl_wrapper(ori:bool):
    
    @flt_quota_check
    def flt_imgurl(r: HttpRequestResult, suc:Callable[[Tuple[str,str,str,str,str]], R], ori:bool=ori) -> R:
        # input per image page response
        # add (image url, reload url, filename) to queue if suc
        # return (errorcode, page_url) if fail
        if re.match('Invalid page', r.response.text):
            raise ImagePageInvalidException(r.final_url)
        
        _ = re.findall(r'src="([^"]+keystamp[^"]+)"', r.response.text)
        if not _:
            _ = re.findall(r'src="([^"]+)"\s+style="', r.response.text)
        if not _:
            raise ImagePageInfoParseException(r.final_url, "can't find image url in page")
        page_img_url = util.htmlunescape(_[0])
        
        page_img_url_info = extract_img_url_info(page_img_url)
        if not page_img_url_info:
            raise ImagePageInfoParseException(r.final_url, "can't parse image url info")
        
        _ = re.findall(
            r'<\/a><\/div><div>(.*?) :: ?\d+ x \d+[ <]', r.response.text)
        if not _:
            raise ImagePageInfoParseException(r.final_url, "can't find original_file_name in page")  
        original_file_name = _[0].strip()
            
        if 'image.php' in original_file_name:
            raise ImagePageInfoParseException(r.final_url, "filename is image.php, can't parse original filename")
            
        _ = re.findall(r'\/(\w+)\/(\d+)-(\d*)', r.final_url)
        if not _:
            raise ImagePageInfoParseException(r.final_url, "can't parse page id from url")
        orignal_hash, gid, unpad_fid = _[0]
        
        # original url example: https://exhentai.org/fullimg/92997/9/77hogvralgb/009.jpg
        
        original_img_url = re.findall(
            r'class="mr".+<a href="(.+)"\s*>Download original', r.response.text)
        original_img_url = util.htmlunescape(original_img_url[0]) if original_img_url else page_img_url
        original_file_name = os.path.basename(original_img_url)
        original_ext = os.path.splitext(original_file_name)[1]
        
            
        _ = re.findall(r"return nl\('([a-zA-Z\d\-]+)'\)", r.response.text)
        if not _:
            raise ImagePageInfoParseException(r.final_url, "can't find js nl value in page")
        js_nl = _[0]
        
        reload_url = "%s%snl=%s" % (
            r.final_url, "&" if "?" in r.final_url else "?", js_nl)
        
        img_url = original_img_url if ori else page_img_url
        file_hash = orignal_hash if ori else page_img_url_info.sha1[:10]
        file_ext = original_ext if ori else page_img_url_info.format
        
        return suc((unpad_fid, file_hash, file_ext, img_url, reload_url))
        
    return flt_imgurl


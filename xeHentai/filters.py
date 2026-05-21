#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import os
import re

import requests
from . import util
from .const import *
from .exceptions import ConnectionFilterException, ImageFileException, ImagePageInfoParseException, KeyExpiredException, QuotaExceededException
from typing import Callable, Any

SUC = 0
FAIL = 1


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


def flt_metadata(r, suc, fail):
    # input index response
    # add gallery meta if suc; return errorcode if fail
    # TODO: catch re exceptions
    if r.status_code == 600:
        return fail(ERR_CONNECTION_ERROR)
    if r.status_code == 404:
        return fail(ERR_GALLERY_REMOVED)
    if re.match("Gallery not found", r.text):
        return fail(ERR_GALLERY_NOT_FOUND)
    if re.match("This gallery is pining for the fjords", r.text):
        return fail(ERR_ONLY_VISIBLE_EXH)
    elif re.match("This IP address has been temporarily banned", r.text):
        fail(ERR_IP_BANNED)
        return re.findall("The ban expires in (.+)", r.text)[0]
    meta = {}
    # print(r.text)
    # sample_hash = re.findall('<a href="%s/./([a-f0-9]{10})/\d+\-\d+"><img' % RESTR_SITE, r.text)
    # meta['sample_hash'] = sample_hash
    # meta['resampled'] = {}

    try:
        title_japanese = util.htmlescape(
            re.findall('="gj">(.*?)</h1>', r.text)[0])
        title_primary = util.htmlescape(
            re.findall('="gn">(.*?)</h1>', r.text)[0])

        # preferred readable naming
        meta['title_japanese'] = title_japanese
        meta['title_primary'] = title_primary

        # backward-compatible aliases
        meta['gjname'] = title_japanese
        meta['gnname'] = title_primary
        # don't assign title now, select by cfg['jpn_title']
        meta['total'] = int(re.findall(
            'Length:</td><td class="gdt2">(\\d+)\\s+page', r.text)[0])
        meta['finished'] = 0
        meta['tags'] = re.findall("toggle_tagmenu\\([^)']+'([^']+)'", r.text)

        # TODO: parse cookie to calc thumbnail_cnt (tr_2, ts_m)
        _ = re.findall("Showing (\\d+) \\- (\\d+) of ([\\d,]+) images", r.text)[0]
        meta['thumbnail_cnt'] = int(_[1]) - int(_[0]) + 1

        meta['newer_versions'] = []
        gnd_block = re.search(r'<div id="gnd">(.+?)</div>', r.text, re.DOTALL)
        if gnd_block:
            for _u, _gid, _sethash, _title, _added in re.findall(
                    r'<a href="(https?://(?:e-|ex)hentai\.org/g/(\d+)/([^/"]+)/?)">([^<]+)</a>,\s*added\s*([^<]+)',
                    gnd_block.group(1)):
                meta['newer_versions'].append({
                    'url': util.htmlescape(_u),
                    'gid': str(_gid),
                    'sethash': str(_sethash),
                    'title': util.htmlescape(_title),
                    'added': _added.strip(),
                })

        suc(meta)
    except IndexError as e:
        print(r.text)
        # return fail(ERR_CONNECTION_ERROR)

    # _ = re.findall(
    #    '%s/[^/]+/(\d+)/[^/]+/\?p=\d*" onclick="return false"(.*?)</a>' % RESTR_SITE,
    #    r.text)
    # meta['pagecount'] = 1 if len(_) <= 1 else int(pagecount[-2])


# def flt_hathdl(r, suc, fail):
#     # input hathdl response
#     # add gallery meta if suc; return errorcode if fail
#     if r.status_code == 404:
#         fail(ERR_HATHDL_NOTFOUND)
#         return ERR_HATHDL_NOTFOUND
#     try:
#         meta = {
#             'name': util.htmlescape(re.findall('TITLE (.+)', r.text)[0]),
#             #'gid': int(re.findall('GID (.+)', r.text)[0]),
#             'total': int(re.findall('FILES (.+)', r.text)[0]),
#             'finished': 0,
#             'title': re.findall('Title:\s+(.+)', r.text)[0],
#             #'upload_time': re.findall('Upload Time:\s+(.+)', r.text)[0], # invisible
#             #'upload_by': re.findall('Uploaded By:\s+(.+)', r.text)[0], # invisible
#             #'downloaded': re.findall('Downloaded:\s+(.+)', r.text)[0], # invisible
#             'tags': re.findall('Tags:\s+(.+)', r.text)[0].split(', '),
#         }
#         listtmp = re.findall('FILELIST\n(.+)\n+\nINFORMATION', r.text, re.DOTALL)[0].split('\n')
#         meta['filelist'] = {}
#         for l in listtmp:
#             # hash(full): id, hash_10, length, width, height, format, name
#             _ = re.findall('(\d+) ([a-z0-9]+)-(\d+)-(\d+)-(\d+)-([a-z]+) (.+)', l)[0]
#             meta['filelist'][_[1][:10]] = list(_)
#     except (IndexError, ValueError) as ex:
#         fail(ERR_MALFORMED_HATHDL)
#         return ERR_MALFORMED_HATHDL
#     suc(meta)

def flt_pageurl(r, suc, fail):
    # input gallery response
    # add per image urls if suc; finish task if fail
    # picpage = re.findall(
    #    '<a href="(%s/./[a-f0-9]{10}/\d+\-\d+)"><img alt="\d+" title="Page' % RESTR_SITE,
    #    r.text)

    # result[0]: page url; 
    # result[1]: page id; 
    # result[2]: original file name (may be empty)
    picpage: list[tuple[str, str, str]] = re.findall(
        '<a href="(%s\\/.\\/[a-f0-9]{10}\\/\\d+\\-\\d+)"><div title="Page (\\d+): ([^"]*)"' % RESTR_SITE,
        r.text)
    # (page url, page id, original file name)
    if not picpage:
        fail(ERR_NO_PAGEURL_FOUND)
    for p in picpage:
        suc(p)


def flt_quota_check(func:Callable[[requests.Response, Callable[...,None], Callable[...,None]], None]):
    def _(r: requests.Response, suc:Callable[...,None], fail:Callable[...,None]) -> None:
        if r.status_code == 600:  # tcp layer error
            raise ConnectionFilterException(r._real_url)
        elif r.status_code == 403:
            raise KeyExpiredException(r._real_url)
        elif r.status_code == 509:
            raise QuotaExceededException(r._real_url, "HTTP 509 bandwidth limit exceeded")
        elif r.content_length in [925, 144, 210, 1009]:
            raise QuotaExceededException(r._real_url, f"quota page content-length fingerprint ({r.content_length} bytes)")
        elif 'hentai.org/img/509.gif' in r.url:
            raise QuotaExceededException(r._real_url, "509.gif detected in response URL")
        elif r.content_length < 200 and \
                r.headers.get('content-type') and r.headers.get('content-type').startswith('text') and \
                re.findall("exceeded your image viewing limits", r.text):
            raise QuotaExceededException(r._real_url, "image viewing limits exceeded (text match)")
        elif r.status_code == 503:
            raise ConnectionFilterException(r._real_url)
        else:
            func(r, suc, fail)
    return _


def flt_imgurl_wrapper(ori:bool):
    @flt_quota_check
    def flt_imgurl(r: requests.Response, suc:Callable[...,None], fail:Callable[...,None], ori=ori):
        # input per image page response
        # add (image url, reload url, filename) to queue if suc
        # return (errorcode, page_url) if fail
        if re.match('Invalid page', r.text):
            return fail((ERR_IMAGE_RESAMPLED, r._real_url))
        
        _ = re.findall(r'src="([^"]+keystamp[^"]+)"', r.text)
        if not _:
            _ = re.findall(r'src="([^"]+)"\s+style="', r.text)
        if not _:
            raise ImagePageInfoParseException(r._real_url, "can't find image url in page")
        picurl = util.htmlescape(_[0])
        
        
        
        _ = re.findall(
            r'<\/a><\/div><div>(.*?) :: ?\d+ x \d+ ?(?::: ([0-9\/.]+ [M|K]?i?B))?<\/div>', r.text)
        if not _:
            raise ImagePageInfoParseException(r._real_url, "can't find filename and filesize in page")  
        filename, filesize = _[0]
        filesize = filesize or None
            
        if 'image.php' in filename:
            _ = re.findall(r'n=(.+)', picurl)
            if not _:
                raise ImagePageInfoParseException(r._real_url, "filename in image.php format but can't find n= in url")
            filename = _[0]
            
        _ = re.findall(r'.+\/(\d+)-(\d*)', r._real_url)
        if not _:
            raise ImagePageInfoParseException(r._real_url, "can't parse page id from url")
        index = _[0]
        
        # file base name is empty, use page id as file name to avoid error
        if filename[0] == '.':
            filename = index[1] + filename
        
        # full url example :
        # http://exhentai.org/fullimg.php?gid=577354&page=2&key=af594b7cf3
        
        
        fullurl = re.findall(
            r'class="mr".+<a href="(.+)"\s*>Download original', r.text)
        _ = re.findall(
            r'>Download original \d+ x \d+ ([\d.]+ [a-zA-Z]{2,})<\/a>', r.text)  # like 2.20MB
        ori_file_size = _[0] if _ else None
        
        if fullurl:
            fullurl = util.htmlescape(fullurl[0])
        else:
            fullurl = picurl
        _ = re.findall(r"return nl\('([a-zA-Z\d\-]+)'\)", r.text)
        if not _:
            raise ImagePageInfoParseException(r._real_url, "can't find js nl value in page")
        js_nl = _[0]
        reload_url = "%s%snl=%s" % (
            r._real_url, "&" if "?" in r._real_url else "?", js_nl)
        if ori:
            # we will parse the 302 url to get original filename
            return suc((fullurl, reload_url, filename, ori_file_size))
        else:
            return suc((picurl, reload_url, filename, filesize))
    return flt_imgurl


def download_file_wrapper(dirpath):

    @flt_quota_check
    def download_file(r:requests.Response, suc, fail, dirpath=dirpath):
        # input image/archive response
        # return (binary, url) if suc; return (errocode, url) if fail
        if r.status_code == 404:
            return fail((ERR_HATH_NOT_FOUND, r._real_url, r.url))
        p = RE_IMGHASH.findall(r.url)
        content_type = (r.headers.get('content-type') or '').split(';', 1)[0].strip().lower()
        original_hash = p[-1][0] if p and p[-1] else None
        # if multiple hash-size-h-w-type is found, use the last one
        # the first is original and the last is scaled
        # _FakeReponse will be filtered in flt_quota_check
        if not r.content_length:
            raise ImageFileException(r._real_url, "zero content-length")
        if p and p[-1] and int(p[-1][1]) != r.content_length:
            raise ImageFileException(r._real_url, f"content-length mismatch (expected {p[-1][1]}, got {r.content_length})")
        if not hasattr(r, 'iter_content_cb'):
            return fail((ERR_STREAM_NOT_IMPLEMENTED, r._real_url, r.url))

        # merge the iter_content iterator with our custom stream_cb
        def _yield(chunk_size=16384, _r=r):
            from requests.exceptions import ConnectionError
            from ssl import SSLWantReadError
            length_read = 0
            try:
                for _ in _r.iter_content(chunk_size):
                    length_read += len(_)
                    _r.iter_content_cb(_)
                    yield _
            except (ConnectionError, SSLWantReadError):  # read timeout
                fail((ERR_IMAGE_BROKEN, r._real_url, r.url))
                raise DownloadAbortedException()
            if length_read != r.content_length:
                fail((ERR_IMAGE_BROKEN, r._real_url, r.url))
                raise DownloadAbortedException()

        suc((_yield, r._real_url, r.url, content_type, original_hash))

    return download_file


def reset_quota(r, suc, fail):
    # reset quota response
    # reset quota if suc; finish task if fail
    pass

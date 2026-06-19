#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import re
import time
import json
import zipfile
import traceback
from hashlib import md5
from threading import Thread
from .const import *
from .const import __version__
from .i18n import i18n
from socketserver import ThreadingMixIn
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import IOBase
from io import BytesIO as StringIO
from urllib.parse import urlparse

cmdre = re.compile("([a-z])([A-Z])")
pathre = re.compile("/(?:jsonrpc|img/|zip/|static/|ui/$)")
staticre = re.compile("/static/")
imgpathre = re.compile("/img/")
zippathre = re.compile("/zip/")

class RPCServer(Thread):
    def __init__(self, xeH, bind_addr, secret = None, logger = None, exit_check = None):
        Thread.__init__(self, name = "rpc")
        Thread.setDaemon(self, True)
        self.xeH = xeH
        self.bind_addr = bind_addr
        self.secret = secret
        self.logger = logger
        self.server = None
        self._exit = exit_check if exit_check else lambda x:False

    def run(self):
        try:
            self.server = ThreadedHTTPServer(self.bind_addr, lambda *x: Handler(self.xeH, self.secret, *x))
        except Exception as ex:
            self.logger.error(i18n.RPC_CANNOT_BIND % traceback.format_exc())
        else:
            self.logger.info(i18n.RPC_STARTED % (self.bind_addr[0], self.bind_addr[1]))
            while not self._exit("rpc"):
                self.server.handle_request()

def is_readable_obj(obj):
    return hasattr(obj, "read")

def is_file_obj(obj):
    return isinstance(obj, IOBase)

def is_str_obj(obj):
    return isinstance(obj, str)

def hash_link(secret, url):
    _ = "%s-xehentai-%s" % (secret if secret else "", url)
    _ = _.encode('utf-8')
    return md5(_).hexdigest()[:8]

def gen_thumbnail(fh, args):
    # returns a new file handler if resized
    # and a boolean indicates there'e error
    try:
        from PIL import Image
    except:
        return fh, True
    if 'w' not in args and 'h' not in args:
        return fh, False
    size = (int(args['w']) if 'w' in args else int(args['h']),
            int(args['h']) if 'h' in args else int(args['w']))
    if not is_file_obj(fh):
        fh = StringIO(fh)
    if fh and Image.isImageType(fh):
        with Image.open(fh) as img:
            img.thumbnail(size)
            ret_fh = StringIO()
            img.save(ret_fh, format=img.format)
            ret = ret_fh.getvalue()
            ret_fh.close()
            fh.close()
            return ret, False
    else:
        return fh, False
    
def jsonrpc_resp(request, ret = None, error_code = None, error_msg = None):
    r = {
        "id":None if not request["id"] else request["id"],
        "jsonrpc":"2.0",
    }
    if error_code:
        r['error'] = {
            'code':error_code,
            "message":i18n.c(error_code) if not error_msg else error_msg
        }
    else:
        r['result'] = ret
    return json.dumps(r)

def path_filter(func):
    def f(self):
        if not pathre.match(self.path):
            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'\n')
            return
        func(self)
    return f

class Handler(BaseHTTPRequestHandler):

    def __init__(self, xeH, secret, *args):
        self.secret = secret
        self.args = args
        self.xeH = xeHentaiRPCExtended(xeH, secret)
        BaseHTTPRequestHandler.__init__(self, *args)

    def version_string(self):
        return "xeHentai/%s" % __version__
    
    def serve_file(self, f):
        f.seek(0, os.SEEK_END)
        size = f.tell()
        self.xeH.logger.debug("GET %s 200 %d %s" % (self.path, size, self.client_address[0]))
        self.send_header("Content-Length", size)
        f.seek(0, os.SEEK_SET)
        self.end_headers()
        while True:
            buf = f.read(51200)
            if not buf:
                break
            self.wfile.write(buf)
        return size

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Max-Age", "1728000")
        self.end_headers()
        self.wfile.write(b'\n')

    @path_filter
    def do_GET(self):
        code = 200
        rt = b''
        mime = "text/html"
        while True:
            if imgpathre.match(self.path):
                args = dict(q.split("=") for q in urlparse(self.path).query.split("&") if q)
                _ = urlparse(self.path).path.split("/")
                if len(_) < 5:
                    code = 400
                    break
                _, _, _hash, guid, fid = _[:5]
                right_hash = hash_link(self.secret, "%s/%s" % (guid, fid))
                if right_hash != _hash:
                    self.xeH.logger.warning("RPC: hash mismatch %s != %s" % (right_hash, _hash))
                    code = 403
                    break
                path, f, mime = self.xeH._get_image_path(guid, fid)
                if not f or not os.path.exists(os.path.join(path, f)):
                    zipf = "%s.zip" % path
                    if not os.path.exists(zipf):
                        self.xeH.logger.warning("RPC: can't find %s" % f)
                        code = 404
                        break
                    else:
                        with zipfile.ZipFile(zipf, 'r') as z:
                            try:
                                rt = z.read(f)
                            except Exception as ex:
                                self.xeH.logger.warning("RPC: can't find %s in zipfile: %s" % (f, ex))
                                code = 404
                                break
                else:
                    rt = open(os.path.join(path, f), 'rb')
                rt, _error = gen_thumbnail(rt, args)
                if _error:
                    self.xeH.logger.warning("RPC: PIL needed for generating thumbnail")
            elif zippathre.match(self.path):
                # args = urlparse(_).query
                _ = urlparse(self.path).path.split("/")
                if len(_) < 5:
                    code = 400
                    break
                _, _, _hash, guid, fname = _[:5]
                fname = fname.split('?')[0]
                right_hash = hash_link(self.secret, "%s" % guid)
                if right_hash != _hash:
                    self.xeH.logger.warning("RPC: hash mismatch %s != %s" % (right_hash, _hash))
                    code = 403
                    break
                f = self.xeH._get_archive_path(guid)
                mime = 'application/zip'
                if not f or not os.path.exists(f):
                    self.xeH.logger.warning("RPC: can't find %s" % f)
                    code = 404
                    break
                rt = open(f, 'rb')
            else:
                # fallback to rpc request
                rt = jsonrpc_resp({"id":None}, error_code = ERR_RPC_INVALID_REQUEST)
                mime = "application/json-rpc"
            break

        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", mime)
        try:
            if is_file_obj(rt):
                size = self.serve_file(rt)
                rt.close()
            else:
                self.xeH.logger.debug("GET %s 200 %d %s" % (self.path, len(rt), self.client_address[0]))
                self.send_header("Content-Length", len(rt))
                self.end_headers()
                self.wfile.write(rt)
            self.wfile.write(b'\n')
        except ConnectionError as e:
            self.xeH.logger.debug('Connection Error : %s' % e)
        return

    @path_filter
    def do_POST(self):
        _get_header = lambda h: self.headers.get_all(h)[0]
        d = self.rfile.read(int(_get_header('Content-Length')))
        code = 200
        rt = b''
        while True:
            try:
                d = d.decode('utf-8')
                j = json.loads(d)
                assert('method' in j and j['method'] != None and 'id' in j)
            except ValueError:
                code = 400
                rte = jsonrpc_resp({"id":None}, error_code = ERR_RPC_PARSE_ERROR)
                break
            except AssertionError:
                code = 400
                rt = jsonrpc_resp({"id":None}, error_code = ERR_RPC_INVALID_REQUEST)
                break
            cmd = re.findall(r"xeH\.(.+)", j['method'])
            if not cmd:
                code = 404
                rt = jsonrpc_resp({"id":j['id']}, error_code = ERR_RPC_METHOD_NOT_FOUND)
                break
            # let's make fooBar to foo_bar
            cmd_r = cmdre.sub(lambda m: "%s_%s" % (m.group(1), m.group(2).lower()), cmd[0])
            if not hasattr(self.xeH, cmd_r) or cmd_r.startswith("_"):
                code = 404
                rt = jsonrpc_resp({"id":j['id']}, error_code = ERR_RPC_METHOD_NOT_FOUND)
                break
            params = ([], {}) if 'params' not in j else j['params']
            if self.secret:
                authorized = False
                while True:
                    if len(params[0]) == 0:
                        break
                    secret = params[0][0]
                    if is_str_obj(secret) and re.findall("token:%s" % self.secret, secret):
                        params[0].pop(0)
                        authorized = True
                    break
                if not authorized:
                    code = 403
                    rt = jsonrpc_resp({"id":j['id']}, error_code = ERR_RPC_UNAUTHORIZED)
                    break
            self.xeH.logger.debug("RPC from: %s, cmd: %s, params: %s" % (self.client_address[0], cmd, params))
            try:
                cmd_rt = getattr(self.xeH, cmd_r)(*params[0], **params[1])
            except (ValueError, TypeError) as ex:
                self.xeH.logger.debug("RPC exec error:\n%s" % traceback.format_exc())
                code = 500
                rt = jsonrpc_resp({"id":j['id']}, error_code = ERR_RPC_EXEC_ERROR,
                error_msg = str(ex))
                break
            if cmd_rt[0] > 0:
                rt = jsonrpc_resp({"id":j['id']}, error_code = cmd_rt[0], error_msg = cmd_rt[1])
            else:
                rt = jsonrpc_resp({"id":j['id']}, ret = cmd_rt[1])
            break
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json-rpc")
        self.send_header("Content-Length", len(rt))
        self.end_headers()
        rt = rt.encode('utf-8')
        self.wfile.write(rt)
        self.wfile.write(b'\n')
        self.xeH.logger.debug("RPC post finished.")
        return


    def log_message(self, format, *args):
        return

# extend xeHentai class for rpc commands
class xeHentaiRPCExtended(object):
    def __init__(self, xeH, secret):
        self.xeH = xeH
        self.secret = secret
    
    def get_info(self):
        ret = {"version": self.verstr,
            "threads_zombie": 0, "threads_running": 0,
            "queue_pending": 0, "queue_finished": 0
        }
        return ERR_NO_ERROR, ret
    
    def get_config(self):
        rt = {k: v for k, v in self.cfg.items() if not k.startswith('rpc_') and k not in ('urls',)}
        return ERR_NO_ERROR, rt
    
    def update_config(self, **cfg_dict):
        cfg_dict = {k: v for k, v in cfg_dict.items() if not k.startswith('rpc_') and k not in ('urls',)}
        if cfg_dict:
            self.xeH.update_config(**cfg_dict)
        return self.get_config()
           
    def list_tasks(self, states=None, *, tags=None, gid=None, url=None,
                   offset=0, limit=100, order_by="updated_at", order_dir="DESC"):
        """List tasks with a simple, single state filter plus tag filtering.

        Parameters:
          states: a phase_state int, or a list of phase_state ints to match
                  (OR semantics — tasks in any of these states). A legacy
                  string level ("download", "finished", "!waiting", ...) is
                  still accepted for backward compatibility and translated to
                  the corresponding state(s). Omit to match all states.
          tags:   a tag string, or a list of tag strings. Tasks carrying ANY of
                  these tags are matched (OR semantics).
          gid/url: exact match.
          offset/limit/order_by/order_dir: pagination and ordering.

        Returns ``(ERR_NO_ERROR, {"total": N, "items": [...]})`` where each
        item is a lightweight dict (guid/gid/url/phase_state/title/total).
        Active tasks are enriched with their live ``done``/``phase_state``.
        """
        from . import session_store as _ss

        states = self._normalize_states(states)
        tags = self._normalize_tags(tags)

        total, rows = _ss.query_tasks(
            states=states, tags=tags, gid=gid, url=url,
            offset=offset, limit=limit,
            order_by=order_by, order_dir=order_dir,
        )
        items = []
        for row in rows:
            item = {
                "guid": row.get("guid", ""),
                "gid": row.get("gid", ""),
                "url": row.get("url", ""),
                "phase_state": int(row.get("phase_state", 0)),
                "title": row.get("title", "") or "",
                "total": int(row.get("total", 0) or 0),
                "done": 0,
            }
            # Enrich with live state for active tasks.
            active = self.xeH._get_active_task(item["guid"])
            if active is not None:
                item["done"] = len(active._flist_done)
                item["phase_state"] = active.state
                item["total"] = active.meta.total if active.meta else item["total"]
            items.append(item)
        return ERR_NO_ERROR, {"total": total, "items": items}

    @classmethod
    def _normalize_states(cls, states):
        """Coerce the ``states`` argument into a list of int phase_states.

        Accepts: an int, a list of ints, or a legacy string level (e.g.
        "download", "!finished"). Returns None to mean "no state filter".
        """
        if states is None:
            return None
        if isinstance(states, str):
            # Legacy level argument. "x" -> match state x; "!x" -> match all
            # states except x (we approximate by enumerating known states).
            return cls._level_to_states(states)
        if isinstance(states, int):
            return [states]
        if isinstance(states, (list, tuple)):
            out = [int(s) for s in states if s is not None]
            return out or None
        return None

    @staticmethod
    def _normalize_tags(tags):
        """Coerce tags into a non-empty list of strings, or None."""
        if tags is None:
            return None
        if isinstance(tags, str):
            return [tags] if tags else None
        if isinstance(tags, (list, tuple)):
            out = [str(t) for t in tags if t]
            return out or None
        return None

    @staticmethod
    def _level_to_states(level):
        """Translate a legacy string level into a list of phase_states.

        Forward form "download" -> [TASK_STATE_DOWNLOAD]. The reverse form
        ("!finished") is deliberately not supported here — callers should pass
        an explicit ``states`` array instead. Returns None for unknown levels.
        """
        if not isinstance(level, str) or not level:
            return None
        if level.startswith('!'):
            # Reverse mode is intentionally dropped; the states array is the
            # supported way to express "everything except X".
            return None
        key = "TASK_STATE_%s" % level.upper()
        const = globals().get(key)
        return [const] if isinstance(const, int) else None
    
    def _get_image_path(self, guid, fid):
        mime_map = {
            "jpg": "image/jpeg",
            "jepg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "bmp": "image/bmp",
            "webp": "image/webp"
        }
        t = self.xeH._hydrate_task(guid)
        if t is None:
            return None, None, None
        try:
            fid = str(fid)
            f = t.get_fid_filename(fid)

            ext = os.path.splitext(f)[1].lower()[1:] if f else ""
            if ext not in mime_map:
                mime = "application/octet-stream"
            else:
                mime = mime_map[ext]
            return t.get_task_dir(), f, mime
        finally:
            # Only evict if this RPC call hydrated a previously-cold task. If the
            # task is actively running, leave it in the active set.
            self._evict_if_cold(guid, t)

    def _get_archive_path(self, guid):
        t = self.xeH._hydrate_task(guid)
        if t is None:
            return None
        try:
            st = time.time()
            pth = t.make_archive(False)
            et = time.time()
            if et - st > 0.1:
                self.xeH.logger.warning('RPC: %.2fs taken to get archive' % (et - st))
            return pth
        finally:
            self._evict_if_cold(guid, t)

    def get_image(self, guid, request_range=None):
        t = self.xeH._hydrate_task(guid)
        if t is None:
            return ERR_TASK_NOT_FOUND, None
        try:
            start = 1
            end = t.meta.total + 1
            if request_range:
                request_range = str(request_range)
                _ = request_range.split(',')
                if len(_) == 1:
                    start = int(request_range)
                else:
                    start = int(_[0])
                end = int(_[0]) + 1
            rt = []
            for fid in range(start, end):
                fid_str = "%d" % fid
                f = t.get_fid_filename(fid_str)
                if not f:
                    # File not resolvable (e.g. not yet downloaded / missing in
                    # archive). Skip it rather than emitting a broken /None URL.
                    continue
                uri = "%s/%s" % (t.guid, fid)
                rt.append('/img/%s/%s/%s' % (hash_link(self.secret, uri), uri, f))
            return ERR_NO_ERROR, rt
        finally:
            self._evict_if_cold(guid, t)

    def _evict_if_cold(self, guid, task):
        """Dehydrate a task hydrated by an RPC call, unless it is currently
        being processed by the run loop (i.e. in the running set)."""
        tc = self.xeH._task_control
        if guid in tc._running_set:
            return
        self.xeH._dehydrate_task(guid)

    
    def __getattr__(self, k):
        # fallback attribute handler
        return getattr(self.xeH, k)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""
    pass

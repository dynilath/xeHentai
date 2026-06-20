#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import re
import json
import traceback
from threading import Thread
from .const import *
from .const import __version__
from .i18n import i18n
from socketserver import ThreadingMixIn
from http.server import HTTPServer, BaseHTTPRequestHandler

cmdre = re.compile("([a-z])([A-Z])")
pathre = re.compile("/(?:jsonrpc|img/|zip/|static/|ui/$)")

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

def is_str_obj(obj):
    return isinstance(obj, str)

# ═══════════════════════════════════════════════════════════════════════════════
# NOTE: hash_link, gen_thumbnail, _get_image_path, _get_archive_path, and the
# image/zip GET handlers have been removed. The new REST API serves images via
# content-addressed URLs at /api/img/{gid}/{fid}-{file_hash}.{ext} and archives
# at /api/archive/{gid}. This module is kept for legacy JSON-RPC POST support
# behind --legacy-rpc only.
# ═══════════════════════════════════════════════════════════════════════════════

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
        # Image/zip serving removed — use the new REST API (/api/img/... and /api/archive/...)
        # Fallback: return JSON-RPC error for unrecognized paths
        rt = jsonrpc_resp({"id":None}, error_code = ERR_RPC_INVALID_REQUEST)
        mime = "application/json-rpc"

        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", mime)
        try:
            self.xeH.logger.debug("GET %s 200 %d %s" % (self.path, len(rt), self.client_address[0]))
            self.send_header("Content-Length", len(rt))
            self.end_headers()
            self.wfile.write(rt if isinstance(rt, bytes) else rt.encode('utf-8'))
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

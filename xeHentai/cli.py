#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import os
import time
import argparse
import traceback
from threading import Thread
from .i18n import i18n
from .core import xeHentai
from .web import WebServer
from .const import *
from .const import __version__
from .util import logger

from . import config as default_config
sys.path.insert(1, FILEPATH)
try:
    import config
except ImportError:
    config = default_config
sys.path.pop(1)

def start():
    opt = parse_opt()
    xeH = xeHentai()
    if opt.daemon:
        if os.name == "posix":
            pid = os.fork()
            if pid == 0:
                sys.stdin.close()
                sys.stdout = open("/dev/null", "w")
                sys.stderr = open("/dev/null", "w")
                return main(xeH, opt)
        elif os.name == "nt":
            return xeH.logger.error(i18n.XEH_PLATFORM_NO_DAEMON % os.name)
        else:
            return xeH.logger.error(i18n.XEH_PLATFORM_NO_DAEMON % os.name)
        xeH.logger.info(i18n.XEH_DAEMON_START % pid)
    else:
        main(xeH, opt)

def main(xeH, opt):
    xeH.update_config(**vars(opt))
    log = xeH.logger
    log.info(i18n.XEH_STARTED % xeH.verstr)
    if opt.cookie:
        xeH.set_cookie(opt.cookie)
    if opt.username and opt.key and not xeH.has_login:
        xeH.login_exhentai(opt.username, opt.key)

    # ── WebServer lifecycle (owned by the composition root, not xeHentai) ──
    web_server = None
    if xeH.config["webui_port"] and xeH.config["webui_host"]:
        web_server = WebServer(
            xeH,
            xeH.config["webui_host"],
            int(xeH.config["webui_port"]),
        )
        web_server.start()

    try:
        Thread(target = xeH._task_loop, name = "main").start()
        while xeH._exit < XEH_STATE_CLEAN:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info(i18n.XEH_CLEANUP)
        xeH._term_threads()
    except Exception as ex:
        log.error(i18n.XEH_CRITICAL_ERROR % traceback.format_exc())
        xeH._term_threads()
    try:
        xeH._cleanup()
    except KeyboardInterrupt:
        pass
    finally:
        if web_server:
            web_server.stop()
    os._exit(0)

def parse_opt():
    _def = {k:v for k,v in default_config.__dict__.items() if not k.startswith("_")}
    _def.update({k:v for k,v in config.__dict__.items() if not k.startswith("_")})
    parser = argparse.ArgumentParser(description = i18n.XEH_OPT_DESC, epilog = i18n.XEH_OPT_EPILOG, add_help = False)
    parser.add_argument('-u', '--username', help = i18n.XEH_OPT_u)
    parser.add_argument('-k', '--key', help = i18n.XEH_OPT_k)
    parser.add_argument('-c', '--cookie', help = i18n.XEH_OPT_c)
    parser.add_argument('--daemon', action = 'store_true', default = _def['daemon'],
                        help = i18n.XEH_OPT_daemon)
    parser.add_argument('-d', '--dir', default = os.path.abspath(_def['dir']),
                        help = i18n.XEH_OPT_d)
    parser.add_argument('-p', '--proxy', action = 'append', default = _def['proxy'],
                        help = i18n.XEH_OPT_p)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--proxy-image', action = 'store_true', default = _def['proxy_image'],
                        help = i18n.XEH_OPT_proxy_image)
    group.add_argument('--proxy-image-only', action = 'store_true', default = _def['proxy_image_only'],
                        help = i18n.XEH_OPT_proxy_image_only)
    parser.add_argument('--webui-host', metavar = "ADDR", default = _def['webui_host'],
                        help = i18n.XEH_OPT_webui_host)
    parser.add_argument('--webui-port', type = int, metavar = "PORT", default = _def['webui_port'],
                        help = i18n.XEH_OPT_webui_port)
    parser.add_argument('-l', '--logpath', metavar = '/path/to/eh.log',
                        default = os.path.abspath(_def['log_path']), help = i18n.XEH_OPT_l)
    parser.add_argument('--log-level-console',
                        choices = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        type = str.upper,
                        default = _def['log_level_console'],
                        dest = 'log_level_console',
                        help = i18n.XEH_OPT_log_level_console)
    parser.add_argument('--log-level-file',
                        choices = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        type = str.upper,
                        default = _def['log_level_file'],
                        dest = 'log_level_file',
                        help = i18n.XEH_OPT_log_level_file)
    parser.add_argument('-h','--help', action = 'help', help = i18n.XEH_OPT_h)
    parser.add_argument('--version', action = 'version', version = f"{SCRIPT_NAME} {__version__} {"_dev" if DEVELOPMENT else ""}",
                        help = i18n.XEH_OPT_version)

    args = parser.parse_args()
    return args

#!/usr/bin/env python
# coding:utf-8
"""xeHentai WebUI entry point.

Starts the xeHentai core and the built-in Web UI server. All configuration
is read from ``config.yml`` in the current directory (or repo root).

On first run, if no ``config.yml`` exists, a default one is generated and
the program exits so you can review and edit it before starting.
"""

import signal
import sys
import time
from threading import Thread

from xeHentai.config_loader import bootstrap_config
from xeHentai.core import xeHentai
from xeHentai.util.logger import Logger
from xeHentai.web import WebServer


def main():
    # ── First-run bootstrap ─────────────────────────────────────────────
    _yaml_config, was_created = bootstrap_config()
    if was_created:
        print("config.yml has been generated from the default template.")
        print("Please review and edit it, then run this program again.")
        sys.exit(0)

    _cfg = _yaml_config.to_flat_dict()

    # ── Logger (created before core so we can log "starting") ───────────
    log = Logger()
    log.set_console_level(_cfg.get("log_level_console", "DEBUG"))
    log.set_file_level(_cfg.get("log_level_file", "DEBUG"))
    log.set_log_path(_cfg.get("log_path", "eh.log"))

    log.info("xeHentai — starting...")

    # ── Bootstrap core ──────────────────────────────────────────────────
    xeH = xeHentai(config=_cfg, log=log)
    log.info("xeHentai %s started.", xeH.verstr)

    # ── Start Web UI ────────────────────────────────────────────────────
    webui_host = xeH.config.get("webui_host", "localhost")
    webui_port = int(xeH.config.get("webui_port", 8010))

    web_server = WebServer(xeH, str(webui_host), webui_port)
    web_server.start()

    # ── Start task loop ─────────────────────────────────────────────────
    task_thread = Thread(target=xeH._task_loop, name="task-loop", daemon=True)
    task_thread.start()

    # ── Shutdown handling ───────────────────────────────────────────────
    shutdown = [False]

    def _handle_signal(signum, frame):
        log.info("Received signal %d, shutting down...", signum)
        shutdown[0] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while not shutdown[0]:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass

    # ── Cleanup ─────────────────────────────────────────────────────────
    log.info("Cleaning up...")
    xeH._term_threads()
    try:
        xeH._cleanup()
    except KeyboardInterrupt:
        pass
    finally:
        if web_server:
            web_server.stop()

    log.info("Goodbye.")
    sys.exit(0)


if __name__ == "__main__":
    main()

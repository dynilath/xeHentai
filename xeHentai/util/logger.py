#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

import logging
from typing import Optional, Union

def safestr(s):
    if isinstance(s, bytes):
        s = s.decode("utf-8")
    return s

class Logger(object):
    def __init__(self, name: str = "xeHentai"):
        self._logger = logging.getLogger(name)
        self._logger.propagate = False
        self._logger.handlers.clear()
        self._logger.setLevel(logging.INFO)

        self._console_level = logging.INFO
        self._file_level = logging.INFO

        self._console_handler = self._build_console_handler()
        self._logger.addHandler(self._console_handler)
        self._file_handler: Optional[logging.FileHandler] = None
        self._sync_logger_level()

    def _build_console_handler(self) -> logging.Handler:
        try:
            from rich.logging import RichHandler

            handler = RichHandler(
                show_time=False,
                show_level=True,
                show_path=False,
                rich_tracebacks=True,
                markup=False,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            handler.setLevel(self._console_level)
            return handler
        except Exception:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter(
                    "%(levelname)-8s %(message)s"
                )
            )
            handler.setLevel(self._console_level)
            return handler

    def _sync_logger_level(self):
        level = self._console_level
        if self._file_handler is not None:
            level = min(level, self._file_level)
        self._logger.setLevel(level)

    @staticmethod
    def _normalize_level(level: Union[int, str]):
        if isinstance(level, int):
            return level
        if isinstance(level, str):
            mapped = logging.getLevelName(level.upper())
            if isinstance(mapped, int):
                return mapped
        raise ValueError("Invalid log level: %s" % level)

    def cleanup(self):
        if self._file_handler:
            self._logger.removeHandler(self._file_handler)
            self._file_handler.close()
            self._file_handler = None
            self._sync_logger_level()

    def setLevel(self, level: Union[int, str]):
        normalized = self._normalize_level(level)
        self._console_level = normalized
        self._file_level = normalized
        self._console_handler.setLevel(normalized)
        if self._file_handler:
            self._file_handler.setLevel(normalized)
        self._sync_logger_level()

    def set_console_level(self, level: Union[int, str]):
        normalized = self._normalize_level(level)
        self._console_level = normalized
        self._console_handler.setLevel(normalized)
        self._sync_logger_level()

    def set_file_level(self, level: Union[int, str]):
        normalized = self._normalize_level(level)
        self._file_level = normalized
        if self._file_handler:
            self._file_handler.setLevel(normalized)
        self._sync_logger_level()

    def set_log_path(self, fpath: str):
        if self._file_handler:
            self._logger.removeHandler(self._file_handler)
            self._file_handler.close()

        handler = logging.FileHandler(fpath, mode="a", encoding="utf-8")
        handler.setLevel(self._file_level)
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(levelname)s %(message)s", datefmt="%b %d %H:%M:%S"
            )
        )
        self._logger.addHandler(handler)
        self._file_handler = handler
        self._sync_logger_level()

    def debug(self, msg, *args, **kwargs):
        self._logger.debug(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self._logger.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self._logger.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self._logger.error(msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        self._logger.exception(msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self._logger.critical(msg, *args, **kwargs)

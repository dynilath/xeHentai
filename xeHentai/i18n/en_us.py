# coding:utf-8

from ..const import *

err_msg = {
    ERR_URL_NOT_RECOGNIZED: "url not recognized",
    ERR_CANT_DOWNLOAD_EXH: "can't download exhentai.org without login",
    ERR_ONLY_VISIBLE_EXH: "this gallery is only visible in exhentai.org",
    ERR_MALFORMED_HATHDL: "malformed .hathdl, can't parse",
    ERR_GALLERY_REMOVED: "this gallery has been removed, may be visible in exhentai",
    ERR_GALLERY_NOT_FOUND: "Gallery not found. If you just added this gallery, you may have to wait a short while "
                           "before it becomes available.",
    ERR_KEY_EXPIRED: "image url is expired",
    ERR_NO_PAGEURL_FOUND: "no page url found, change of site structure?",
    ERR_CONNECTION_ERROR: "a connection problem occurs",
    ERR_IP_BANNED: "IP has been banned, retry in %s",
    ERR_IMAGE_BROKEN: "downloaded image is broken",
    ERR_QUOTA_EXCEEDED: "quota exceeded",
    ERR_TASK_NOT_FOUND: "no such task guid",
    ERR_TASK_LEVEL_UNDEF: "task filter level unknown",
    ERR_DELETE_RUNNING_TASK: "can't delete a running task",
    ERR_TASK_CANNOT_PAUSE: "this task can't be paused",
    ERR_TASK_CANNOT_RESUME: "this task can't be resumed",
    ERR_CANNOT_CREATE_DIR: "can't create directory %s",
    ERR_CANNOT_MAKE_ARCHIVE: "can't make archive %s",
    ERR_NOT_RANGE_FORMAT: "'%s' is not a range format, expecting '1-2' or '3'",
#    ERR_HATHDL_NOTFOUND: "hathdl not found",
    ERR_RPC_PARSE_ERROR: "Parse error.",
    ERR_RPC_INVALID_REQUEST: "Invalid request.",
    ERR_RPC_METHOD_NOT_FOUND: "Method not found.",
    ERR_RPC_INVALID_PARAMS: "Invalid method parameter(s).",
    ERR_RPC_UNAUTHORIZED: "Unauthorized",
    ERR_RPC_EXEC_ERROR: "",
    ERR_SAVE_SESSION_FAILED: "",
}

ERR_NOMSG = "undefined error message with code %d"

PROXY_CANDIDATE_CNT = "proxy pool has %d candidates"

TASK_PUT_INTO_WAIT = "task #%s already exists, put into waiting state"
TASK_ERROR = "task %s error: %s"
TASK_MIGRATE_EXH = "task %s migrate to exhentai.org"
TASK_TITLE = "[guid={guid}] task title {title}"
TASK_WILL_DOWNLOAD_CNT = "task [guid={guid} gid={gid}] will download {count}/{total} files"
TASK_START = "[guid={guid} gid={gid}] task start"
TASK_FINISHED = "[guid={guid} gid={gid}] task download finishd"
TASK_START_MAKE_ARCHIVE = "[guid={guid} gid={gid}] task start making archive"
TASK_MAKE_ARCHIVE_FINISHED = "[guid={guid} gid={gid}] task archive saved at: {path}, use {time:.1f}s"
TASK_STOP_QUOTA_EXCEEDED = "[guid={guid} gid={gid}] task quota exceeded"

XEH_STARTED = "xeHentai %s started."
XEH_LOOP_FINISHED = "application task loop finished"
XEH_LOGIN_EXHENTAI = "login exhentai"
XEH_LOGIN_OK = "login exhentai successfully"
XEH_LOGIN_FAILED = "can't login exhentai, check your credentials or try another account.\nIt's recommended to login in browser and use RPC to transfer cookie to xeHentai (see http://t.cn/Rctr4Pf)"
XEH_LOAD_TASKS_CNT = "load %d tasks from saved session"
XEH_LOAD_OLD_COOKIE = "load cookie from legacy cookie file"
XEH_CLEANUP = "cleaning up..."
XEH_CRITICAL_ERROR = "xeHentai throws critical error:\n%s"
XEH_DOWNLOAD_ORI_NEED_LOGIN = "haven't login, so I won't download original images"
XEH_FILE_DOWNLOADED = "[guid={guid}] file downloaded fid={fid} {fname}"
XEH_RENAME_HAS_ERRORS = "some files are not renamed:\n%s"
XEH_DOWNLOAD_HAS_ERROR = "#%s retry because of error: %s, will retry url %s later"

WEBUI_STARTED = "WebUI server listening on %s:%d"
WEBUI_CANNOT_BIND = "WebUI server can't listen on requested address: %s"

SESSION_LOAD_EXCEPTION = "exception occurs when loading saved session: %s"
SESSION_WRITE_EXCEPTION = "exception occurs when writing saved session: %s"

THREAD = "thread"
THREAD_UNCAUGHT_EXCEPTION = "thread-%s uncaught exception\n%s"
THREAD_MAY_BECOME_ZOMBIE = "thread-%s may became zombie"
THREAD_SWEEP_OUT = "thread-%s is dead, deref it"

QUEUE = "queue"

PROXY_DISABLE_BANNED = "disable a banned proxy, expire in about %ss"

# forked i18n items
DF_FULLY_MATCHED = "[guid={guid}] task found fully-matched zip (same gid/hash) at {path}"
DF_FULLY_MATCHED_UP_TO_DATE = "[guid={guid}] task archive is up to date, no need to update, at {path}"
DF_FULLY_MATCHED_UPDATED = "[guid={guid}] task archive metadata updated, new archive at {path}"
DF_STATE_START_SCAN_PAGE = "[guid={guid} gid={gid}] task start scanning pages"
DF_FILE_DOWNLOADED_SKIPPED = "[guid={guid}] skipped downloading image fid={fid} {reason}"
DF_MIGRATE_NEW_VERSION = "[guid={guid} gid={gid}] gallery migrate to new version, new task url: {url}"
DF_MIGRATE_NEW_VERSION_FAIL = "[guid={guid} gid={gid}] gallery migrate to new version failed [result={ret}] url: {url}"

# control flows
CF_SCANDOWNLOADSKIP_DUPLICATE = "duplicate file found, skip downloading"
CF_SCANDOWNLOADSKIP_EXISTING = "file already exists, skip downloading"

# error messages
TS_ERR_GALLERY_REMOVED = "[guid={guid} gid={gid}] gallery has been removed, may need to change IP to access or wait until it's visible in exhentai"
TS_ERR_GALLERY_NOT_FOUND = "[guid={guid} gid={gid}] gallery not found, may be incorrect url or gallery not created"

# ── Gallery subscriptions ────────────────────────────────────────────────
SUB_STARTED = "subscription manager started"
SUB_STOPPED = "subscription manager stopped"
SUB_DISABLED = "subscription checks disabled by config"
SUB_NEW_VERSION = "[sub id={sid} gid={gid}] new version detected: {url} (added {added})"
SUB_LINK_REPLACED = "[sub id={sid}] subscription now tracks gid {new_gid} (was {old_gid}): {url}"
SUB_ADD_TASK_OK = "[sub id={sid}] new-version download task added [guid={guid}]: {url}"
SUB_ADD_TASK_FAIL = "[sub id={sid}] failed to add new-version download task [ret={ret}]: {url}"
SUB_CHECK_OK = "[sub id={sid} gid={gid}] up to date"
SUB_CHECK_ERROR = "[sub id={sid} gid={gid}] check failed: {error}"
SUB_ROUND_ABORT_BANNED = "subscription round aborted (IP banned), remaining checks deferred by {defer}s"

# ── Config template tags (replaced at first-run bootstrap) ──────────────

config_tags = {
    "header": "xeHentai configuration",
    "gateway_section": "Gateway (Web UI + REST API)",
    "gateway_host": 'Bind address, "0.0.0.0" to listen on all interfaces',
    "gateway_port": "Listen port",
    "download_section": "Download",
    "download_dir": "Download root directory",
    "download_ori": "Download original images (requires ExHentai login)",
    "jpn_title": "Prefer Japanese title",
    "delete_task_files": "Delete files when deleting a task",
    "proxy_section": "Proxy",
    "proxy_servers": 'Proxy server list, e.g. ["http://127.0.0.1:7890"]',
    "proxy_image": "Use proxy for image downloads",
    "proxy_image_only": "Only proxy image downloads, not pages",
    "proxy_heal_after": "Consecutive successes to fully recover a proxy",
    "performance_section": "Performance / Concurrency",
    "scan_thread_cnt": "Page-scan thread count",
    "download_thread_cnt": "Image-download thread count",
    "async_task_concurrency": "Max concurrent tasks",
    "page_interval": "Interval between page requests (seconds)",
    "page_retry": "Page request retry count",
    "page_timeout": "Page request timeout (seconds)",
    "download_retry": "Image download retry count",
    "download_timeout": "Image download timeout (seconds)",
    "logging_section": "Logging",
    "log_path": "Log file path",
    "log_level_console": "Console log level: DEBUG/INFO/WARNING/ERROR/CRITICAL",
    "log_level_file": "File log level: DEBUG/INFO/WARNING/ERROR/CRITICAL",
    "subscription_section": "Gallery Subscriptions",
    "subscription_enabled": "Enable periodic checks for subscribed galleries",
    "subscription_check_interval": "Hours between subscription checks",
    "subscription_check_pacing": "Seconds between individual gallery checks in one round",
}
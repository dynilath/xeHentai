# coding:utf-8

from ..const import *

err_msg = {
    ERR_URL_NOT_RECOGNIZED: "网址不够绅士",
    ERR_CANT_DOWNLOAD_EXH: "需要登录后才能下载里站",
    ERR_ONLY_VISIBLE_EXH: "这个本子只有里站能看到",
    ERR_MALFORMED_HATHDL: "hathdl文件有猫饼，解析失败",
    ERR_GALLERY_REMOVED: "这个本子被移除了，大概里站能看到",

    ERR_GALLERY_NOT_FOUND: "无法找到图集，如果是由你创建的图集，你需等待至图集可用。",

    ERR_KEY_EXPIRED: "下载链接不太正常",
    ERR_NO_PAGEURL_FOUND: "没有找到页面链接，网站改版了嘛？",
    ERR_CONNECTION_ERROR: "连接有问题？",
    ERR_IP_BANNED: "IP被ban了, 恢复时间: %s",
    ERR_IMAGE_BROKEN: "下载的图片有猫饼",
    ERR_QUOTA_EXCEEDED: "配额超限",
    ERR_TASK_NOT_FOUND: "没有该GUID对应的任务",
    ERR_TASK_LEVEL_UNDEF: "任务过滤等级不存在",
    ERR_DELETE_RUNNING_TASK: "无法删除运行中的任务",
    ERR_TASK_CANNOT_PAUSE: "这个任务无法被暂停",
    ERR_TASK_CANNOT_RESUME: "这个任务无法被恢复",
    ERR_CANNOT_CREATE_DIR: "无法创建文件夹 %s",
    ERR_CANNOT_MAKE_ARCHIVE: "无法制作压缩包 %s",
    ERR_NOT_RANGE_FORMAT: "'%s'不符合范围的格式, 正确的格式为 1-3 或者 5",
#    ERR_HATHDL_NOTFOUND: "hathdl文件未找到"
    ERR_RPC_PARSE_ERROR: "Parse error.",
    ERR_RPC_INVALID_REQUEST: "Invalid request.",
    ERR_RPC_METHOD_NOT_FOUND: "Method not found.",
    ERR_RPC_INVALID_PARAMS: "Invalid method parameter(s).",
    ERR_RPC_UNAUTHORIZED: "Unauthorized",
    ERR_RPC_EXEC_ERROR: "",
    ERR_SAVE_SESSION_FAILED: "",
}

ERR_NOMSG = "未指定的错误，错误号 %d"

PROXY_CANDIDATE_CNT = "代理池中有%d个代理"

TASK_PUT_INTO_WAIT = "任务 #%s 已存在, 加入等待队列"
TASK_ERROR = "任务 #%s 发生错误: %s"
TASK_MIGRATE_EXH = "任务 #%s 使用里站地址重新下载"
TASK_TITLE = "[guid={guid} gid={gid}] 任务标题 {title}"
TASK_WILL_DOWNLOAD_CNT = "[guid={guid} gid={gid}] 任务将下载 {count}/{total} 个文件"
TASK_START = "[guid={guid} gid={gid}] 任务开始"
TASK_FINISHED = "[guid={guid} gid={gid}] 任务下载完成"
TASK_START_MAKE_ARCHIVE = "[guid={guid} gid={gid}] 任务开始打包"
TASK_MAKE_ARCHIVE_FINISHED = "[guid={guid} gid={gid}] 任务打包完成，保存在: {path}, 用时{time:.1f}秒"
TASK_STOP_QUOTA_EXCEEDED = "[guid={guid} gid={gid}] 任务配额超限"

XEH_STARTED = "xeHentai %s 已启动"
XEH_LOOP_FINISHED = "程序循环已完成"
XEH_LOGIN_EXHENTAI = "登录绅士"
XEH_LOGIN_OK = "已成为绅士"
XEH_LOGIN_FAILED = "无法登录绅士；检查输入是否有误或者换一个帐号。\n推荐在浏览器登录后使用RPC复制cookie到xeHentai (教程: http://t.cn/Rctr4Pf)"
XEH_LOAD_TASKS_CNT = "从存档中读取了%d个任务"
XEH_LOAD_OLD_COOKIE = "从1.x版cookie文件从读取了登录信息"
XEH_CLEANUP = "擦干净..."
XEH_CRITICAL_ERROR = "xeHentai 抽风啦:\n%s"
XEH_DOWNLOAD_ORI_NEED_LOGIN = "下载原图需要登录"
XEH_FILE_DOWNLOADED = "[guid={guid}] 已下载图片 fid={fid} {fname}"
XEH_RENAME_HAS_ERRORS = "部分图片重命名失败:\n%s"
XEH_DOWNLOAD_HAS_ERROR = "[guid={guid}] 下载图片时出错: %s, 将在稍后重试链接 %s"

WEBUI_STARTED = "WebUI服务器监听在 %s:%d"
WEBUI_CANNOT_BIND = "WebUI服务器无法启动：%s"

SESSION_LOAD_EXCEPTION = "读取存档时遇到错误: %s"
SESSION_WRITE_EXCEPTION = "写入存档时遇到错误: %s"

THREAD = "绅士"
THREAD_UNCAUGHT_EXCEPTION = "绅士-%s 未捕获的异常\n%s"
THREAD_MAY_BECOME_ZOMBIE = "绅士-%s 可能变成了丧尸"
THREAD_SWEEP_OUT = "绅士-%s 挂了, 不再理它"

QUEUE = "队列"

PROXY_DISABLE_BANNED = "禁用了一个被ban的代理，将在约%s秒后恢复"

# forked i18n items
DF_FULLY_MATCHED = "[guid={guid}] 任务找到完全匹配的压缩包 (相同的 gid/hash)，位于 {path}"
DF_FULLY_MATCHED_UP_TO_DATE = "[guid={guid}] 任务压缩包已经是最新，无需更新，位于 {path}"
DF_FULLY_MATCHED_UPDATED = "[guid={guid}] 任务压缩包元数据已更新，新压缩包位于 {path}"
DF_STATE_START_SCAN_PAGE = "[guid={guid} gid={gid}] 任务开始扫描页面"
DF_FILE_DOWNLOADED_SKIPPED = "[guid={guid}] 跳过下载图片 fid={fid} {reason}"
DF_MIGRATE_NEW_VERSION = "[guid={guid} gid={gid}] 图集迁移到新版本，新的任务地址: {url}"
DF_MIGRATE_NEW_VERSION_FAIL = "[guid={guid} gid={gid}] 图集迁移到新版本失败 [result={ret}]，地址: {url}"

# control flows
CF_SCANDOWNLOADSKIP_DUPLICATE = "发现重复文件，跳过下载"
CF_SCANDOWNLOADSKIP_EXISTING = "文件已存在，跳过下载"

# error messages
TS_ERR_GALLERY_REMOVED = "[guid={guid} gid={gid}] 图集被移除，可能需要换个IP进行访问，或者等待里站可见"
TS_ERR_GALLERY_NOT_FOUND = "[guid={guid} gid={gid}] 无法找到图集，可能地址错误或者图集未创建"

# ── Gallery subscriptions ────────────────────────────────────────────────
SUB_STARTED = "订阅管理器已启动"
SUB_STOPPED = "订阅管理器已停止"
SUB_DISABLED = "订阅检查已在配置中关闭"
SUB_NEW_VERSION = "[sub id={sid} gid={gid}] 检测到新版本: {url} (added {added})"
SUB_LINK_REPLACED = "[sub id={sid}] 订阅已指向新版本 gid {new_gid} (原 gid {old_gid}): {url}"
SUB_ADD_TASK_OK = "[sub id={sid}] 已添加新版本下载任务 [guid={guid}]: {url}"
SUB_TASK_ADDED = "[sub id={sid} gid={gid}] 已为订阅的画廊创建任务 [guid={guid}]: {url}"
SUB_TASK_ADD_FAIL = "[sub id={sid} gid={gid}] 为订阅的画廊创建任务失败 [ret={ret}]: {url}"
SUB_ADD_TASK_FAIL = "[sub id={sid}] 添加新版本下载任务失败 [ret={ret}]: {url}"
SUB_CHECK_OK = "[sub id={sid} gid={gid}] 已是最新版本"
SUB_CHECK_ERROR = "[sub id={sid} gid={gid}] 检查失败: {error}"
SUB_ROUND_ABORT_BANNED = "订阅检查中止（IP 被封），剩余检查顺延 {defer} 秒"

# ── Config template tags (replaced at first-run bootstrap) ──────────────

config_tags = {
    "header": "xeHentai 配置文件",
    "gateway_section": "Gateway 设置 (Web UI + REST API)",
    "gateway_host": '监听地址，"0.0.0.0" 表示监听所有网络接口',
    "gateway_port": "监听端口",
    "download_section": "下载设置",
    "download_dir": "下载根目录",
    "download_ori": "是否下载原图（需要登录 ExHentai）",
    "jpn_title": "优先使用日文标题",
    "delete_task_files": "删除任务时同时删除已下载的文件",
    "proxy_section": "代理设置",
    "proxy_servers": '代理服务器列表，例如: ["http://127.0.0.1:7890"]',
    "proxy_image": "代理也用于图片下载",
    "proxy_image_only": "仅代理图片下载，不代理页面请求",
    "proxy_heal_after": "连续成功多少次后完全恢复代理",
    "performance_section": "性能 / 并发设置",
    "scan_thread_cnt": "扫描页面线程数",
    "download_thread_cnt": "下载图片线程数",
    "async_task_concurrency": "最大同时执行的任务数",
    "page_interval": "页面请求间隔（秒）",
    "page_retry": "页面请求重试次数",
    "page_timeout": "页面请求超时（秒）",
    "download_retry": "图片下载重试次数",
    "download_timeout": "图片下载超时（秒）",
    "logging_section": "日志设置",
    "log_path": "日志文件路径",
    "log_level_console": "控制台日志级别: DEBUG/INFO/WARNING/ERROR/CRITICAL",
    "log_level_file": "文件日志级别: DEBUG/INFO/WARNING/ERROR/CRITICAL",
    "subscription_section": "画廊订阅",
    "subscription_enabled": "启用订阅画廊的周期性更新检查",
    "subscription_check_interval": "订阅检查间隔（小时）",
    "subscription_check_pacing": "同一轮检查中相邻画廊之间的间隔（秒）",
}
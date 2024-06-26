# coding:utf-8
# DO NOT EDIT THIS FILE
# make a copy to your working directory
# and edit that file

# Daemon mode
daemon = False

# set download directory
dir = "."
# download original images, needs to login
download_ori = False
# Set if use Japanese title if available
jpn_title = True
# rename gallery image to original name, use sequence name if turned off
rename_ori = False

# set download proxies
# currenlty supported: socks5/4a, http(s), glype
# by default, proxy is only used on webpages
proxy = []
proxy_disable_threshold = 16
proxy_good_threshold = 16
# also use proxy to download images
proxy_image = True
# only use proxy on images, not webpages
# if set to True, the value of proxy_image will be ignored
proxy_image_only = False

# bind jsonrpc server to this address
rpc_interface = 'localhost'
# bind jsonrpc server to this port
rpc_port = None
# jsonrpc secret string
rpc_secret = None

# make an archive (.zip) after download and delete directory
make_archive = False
# specify ranges of images to be downloaded, in format
# start-end, or single index, use comma to concat
# multiple ranges, e.g.: 5-10,15,20-25, default to
# download all images
download_range = None
# scan threads count
scan_thread_cnt = 1
# download threads count
download_thread_cnt = 5
# set image download timeout
download_timeout = 10

# ignore these error codes, continue download
# to use predefined error codes, use:
# import const as __c
# ignored_errors = [__c.ERR_QUOTA_EXCEEDED]
ignored_errors = []

# define log path
log_path = "eh.log"
# set log level
log_verbose = 2

# save tasks to h.json
save_tasks = False

# delete files when deleting a task
delete_task_files = False


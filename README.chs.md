# xeHentai

E-Hentai / ExHentai 图库下载器，内置 Web 管理界面。

## 快速入门

推荐使用虚拟环境 (venv) 来隔离依赖：

```shell
# 创建并激活虚拟环境
python -m venv .venv

# Windows:
.venv\Scripts\activate

# Linux / macOS:
source .venv/bin/activate
```

venv 是 Python 自带的虚拟环境工具，在 venv 中安装的包不会影响系统 Python 或其他项目。

然后安装依赖并运行：

```shell
pip install -r requirements.txt
python xeH.py
```

首次运行时会自动生成默认的 `config.yml`，程序会提示你检查并编辑该文件后重新运行：

```shell
python xeH.py
```

根据控制台输出的地址（默认 `http://localhost:8010`）在浏览器中打开。

## 配置说明

所有配置都在项目根目录的 `config.yml` 中，文件内有详细的中文注释。大部分设置也可以在 Web UI 的 Config 页面修改。

| 配置项 | 说明 | 默认值 |
|---|---|---|
| `gateway.host` | 监听地址 | `localhost` |
| `gateway.port` | 监听端口 | `8010` |
| `download.dir` | 下载根目录 | `./download` |
| `download.download_ori` | 下载原图（需登录 ExHentai） | `false` |
| `download.jpn_title` | 优先使用日文标题 | `true` |
| `download.delete_task_files` | 删除任务时同时删除文件 | `false` |
| `proxy.servers` | 代理服务器列表 | `[]` |
| `proxy.image` | 代理也用于图片下载 | `true` |
| `proxy.image_only` | 仅代理图片，不代理页面 | `false` |
| `performance.scan_thread_cnt` | 扫描页面线程数 | `1` |
| `performance.download_thread_cnt` | 下载图片线程数 | `5` |
| `performance.async_task_concurrency` | 最大同时任务数 | `1` |
| `performance.page_interval` | 页面请求间隔（秒） | `0.5` |
| `logging.path` | 日志文件路径 | `eh.log` |

完整配置项见 `config.yml`。

### 代理

配置 HTTP 代理：

```yaml
proxy:
  servers:
    - "http://127.0.0.1:7890"
```

默认代理同时用于页面和图片。设置 `proxy.image_only` 为 `true` 可仅代理图片下载，
设置 `proxy.image` 为 `false` 可仅代理页面。

### API

Gateway 同时提供 REST API，访问 `/api/docs` 查看 Swagger 文档。

## License

GPLv3

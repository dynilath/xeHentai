# xeHentai

E-Hentai / ExHentai gallery downloader with a built-in Web UI.

## Quick Start

It's recommended to use a virtual environment (venv) to isolate dependencies:

```shell
# Create and activate a virtual environment
python -m venv .venv

# Windows:
.venv\Scripts\activate

# Linux / macOS:
source .venv/bin/activate
```

A venv is a self-contained Python environment — packages installed inside it
won't affect your system Python or other projects.

Then install dependencies and run:

```shell
pip install -r requirements.txt
python xeH.py
```

On first run, a default `config.yml` is generated. The program will exit and ask
you to review it — open the file, adjust settings, then run again:

```shell
python xeH.py
```

Check the console output for the gateway address (default: `http://localhost:8010`) and open it in your browser.

## Configuration

All settings are in `config.yml` at the project root. The file is
well-commented — open it in any text editor. Most settings can also be
changed from the Web UI's Config page.

| Key | Description | Default |
|---|---|---|
| `gateway.host` | Gateway bind address | `localhost` |
| `gateway.port` | Gateway listen port | `8010` |
| `download.dir` | Download root directory | `./download` |
| `download.download_ori` | Download original images (requires login) | `false` |
| `download.jpn_title` | Prefer Japanese title | `true` |
| `download.delete_task_files` | Delete files when deleting a task | `false` |
| `proxy.servers` | Proxy server list | `[]` |
| `proxy.image` | Use proxy for image downloads too | `true` |
| `proxy.image_only` | Only proxy images, not pages | `false` |
| `performance.scan_thread_cnt` | Page-scan thread count | `1` |
| `performance.download_thread_cnt` | Image-download thread count | `5` |
| `performance.async_task_concurrency` | Max concurrent tasks | `1` |
| `performance.page_interval` | Interval between page requests (s) | `0.5` |
| `logging.path` | Log file path | `eh.log` |
| `subscription.enabled` | Enable gallery subscription checks | `true` |
| `subscription.check_interval` | Hours between subscription checks | `24` |
| `subscription.check_pacing` | Seconds between gallery checks in one round | `5` |

See `config.yml` for the full list.

### Proxies

To route traffic through an HTTP proxy:

```yaml
proxy:
  servers:
    - "http://127.0.0.1:7890"
```

By default proxies are used for both pages and images. Set `proxy.image_only`
to `true` to proxy only image downloads, or `proxy.image` to `false` to proxy
only pages.

## API

The gateway serves REST API endpoints alongside the Web UI. See
`/api/docs` (Swagger) for the full reference.

## License

GPLv3

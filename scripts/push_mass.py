import requests
import sys
from typing import List, Optional

DEFAULT_BASE_URL = "http://192.168.0.90:8010"


def system_status():
    r = requests.get(f"{DEFAULT_BASE_URL}/api/system/status")
    print(r.text)


def add_task(url: str):
    print(f"Adding task for URL: {url}")
    r = requests.post(f"{DEFAULT_BASE_URL}/api/tasks", json={"url": url})
    print(r.text)


def add_tasks(urls: List[str]):
    print(f"Adding {len(urls)} tasks")
    r = requests.post(f"{DEFAULT_BASE_URL}/api/tasks/bulk", json={
        "urls": urls,
        "enqueue_existed": True,
    })
    print(r.text)
    print(f"Last Added = {urls[-1] if urls else 'None'}")


def push_src(path: str, wait_url: Optional[str] = None):
    start_processing = wait_url is None
    batch: List[str] = []
    with open(path, 'r') as f:
        while True:
            s = f.readline()

            if not s:
                break
            if not s.startswith("http"):
                continue

            url = s.strip()

            if not start_processing:
                if url == wait_url:
                    start_processing = True
                    print(f"Matched wait URL: {wait_url}")
                else:
                    print(f"Skipping: {url}")
                    continue

            batch.append(url)
            if len(batch) >= 256:
                add_tasks(batch)
                batch.clear()

    if batch:
        add_tasks(batch)


if __name__ == "__main__":
    target_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    wait_url = sys.argv[2] if len(sys.argv) > 2 else None
    push_src(target_dir, wait_url)

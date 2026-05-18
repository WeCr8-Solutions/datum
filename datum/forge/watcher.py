"""
FORGE — Filesystem Watcher
Watches the staging directory and repo for new/changed files.
Triggers FORGE processing automatically on file events.
Run alongside forge.py for real-time processing.

Usage:
  python3 watcher.py --path ./staging --notify
  pm2 start watcher.py --interpreter python3 --name forge-watcher
"""

import asyncio
import sys
import time
import argparse
from pathlib import Path
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileCreatedEvent, FileModifiedEvent

import yaml
from rich.console import Console

console = Console()


class ForgeWatcher(FileSystemEventHandler):
    def __init__(self, trigger_queue: asyncio.Queue, extensions: set):
        self.queue      = trigger_queue
        self.extensions = extensions
        self._cooldown  = {}   # path -> last event time (debounce)
        self._debounce  = 2.0  # seconds

    def on_created(self, event):
        if not event.is_directory:
            self._handle(event.src_path, "created")

    def on_modified(self, event):
        if not event.is_directory:
            self._handle(event.src_path, "modified")

    def _handle(self, path: str, event_type: str):
        p = Path(path)
        if p.suffix.lower() not in self.extensions:
            return

        # Debounce — ignore rapid repeated events
        now = time.time()
        if path in self._cooldown and now - self._cooldown[path] < self._debounce:
            return
        self._cooldown[path] = now

        console.print(f"[dim]{datetime.now().strftime('%H:%M:%S')}[/] "
                      f"[yellow]File {event_type}:[/] {p.name}")

        try:
            self.queue.put_nowait({"path": path, "event": event_type})
        except asyncio.QueueFull:
            pass


async def watch(watch_path: str, config: dict):
    extensions = set(
        config.get("filesystem", {}).get(
            "supported_extensions",
            [".md", ".txt", ".pdf", ".docx"]
        )
    )

    queue    = asyncio.Queue(maxsize=100)
    observer = Observer()
    handler  = ForgeWatcher(queue, extensions)
    observer.schedule(handler, watch_path, recursive=True)
    observer.start()

    console.print(f"[bold]FORGE Watcher[/] — watching [dim]{watch_path}[/]")
    console.print(f"Extensions: {', '.join(extensions)}")
    console.print("Drop files into the staging directory to trigger processing.\n")

    try:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                console.print(
                    f"[green]→[/] Queued for FORGE: {Path(event['path']).name}"
                )
                # In full deployment, this would signal the FORGE loop
                # For now, prints the trigger event
                queue.task_done()
            except asyncio.TimeoutError:
                pass  # No file events in 1 s — keep watching

    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


def main():
    parser = argparse.ArgumentParser(description="FORGE Filesystem Watcher")
    parser.add_argument("--path",   default="./staging")
    parser.add_argument("--config", default="./config/forge.yaml")
    args = parser.parse_args()

    config = {}
    if Path(args.config).exists():
        config = yaml.safe_load(Path(args.config).read_text()) or {}

    asyncio.run(watch(args.path, config))


if __name__ == "__main__":
    main()

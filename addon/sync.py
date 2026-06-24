#!/usr/bin/env python3
"""
Sync Frigate NVR recordings to S3 as 10-minute concatenated windows.

Uses inotify (watchdog) to detect when a new 10-minute window starts,
then processes the completed previous window.

Frigate layout:  /media/frigate/recordings/YYYY-MM-DD/HH/<camera>/MM.SS.mp4
S3 key:          {camera}_{start_time.isoformat()}.mp4
"""

import json
import logging
import queue
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

RECORDINGS_BASE = Path("/media/frigate/recordings")
OPTIONS_FILE = Path("/data/options.json")
PROCESSED_FILE = Path("/data/processed.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("camera-save")


def load_config() -> dict:
    opts = json.loads(OPTIONS_FILE.read_text())
    cameras = opts.get("cameras", [])
    if not isinstance(cameras, list):
        cameras = []
    return {
        "aws_access_key_id": opts["aws_access_key_id"],
        "aws_secret_access_key": opts["aws_secret_access_key"],
        "aws_region": opts.get("aws_region", "ap-southeast-2"),
        "s3_bucket": opts["s3_bucket"],
        "upload_delay_minutes": int(opts.get("upload_delay_minutes", 15)),
        "cameras": cameras,
    }


def load_processed() -> set:
    if not PROCESSED_FILE.exists():
        return set()
    try:
        data = json.loads(PROCESSED_FILE.read_text())
        keys = set()
        for camera, windows in data.get("windows", {}).items():
            for w in windows:
                keys.add(f"{camera}/{w}")
        return keys
    except (json.JSONDecodeError, KeyError):
        log.warning("Could not parse processed.json, starting fresh")
        return set()


def save_processed(processed: set) -> None:
    grouped: dict[str, list[str]] = {}
    for key in processed:
        camera, window = key.split("/", 1)
        grouped.setdefault(camera, []).append(window)
    tmp = PROCESSED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"windows": grouped}, indent=2))
    tmp.rename(PROCESSED_FILE)


def window_key(camera: str, date: str, hour: str, window_mm: str) -> str:
    return f"{camera}/{date}/{hour}/{window_mm}"


def previous_window(date_str: str, hour_str: str, seg_minute: int) -> dict:
    """Given a segment's minute, return the identity of the previous 10-min window."""
    current_window_start = (seg_minute // 10) * 10
    if current_window_start == 0:
        dt = datetime(*[int(x) for x in date_str.split("-")], int(hour_str), 0, 0, tzinfo=timezone.utc) - timedelta(hours=1)
        return {
            "date": dt.strftime("%Y-%m-%d"),
            "hour": f"{dt.hour:02d}",
            "window_mm": "50",
        }
    return {
        "date": date_str,
        "hour": hour_str,
        "window_mm": f"{current_window_start - 10:02d}",
    }


class RecordingHandler(FileSystemEventHandler):
    def __init__(self, pending_queue: queue.Queue, camera_filter: set):
        self._queue = pending_queue
        self._camera_filter = camera_filter

    def on_created(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix != ".mp4":
            return
        window = self._parse_previous_window(path)
        if window:
            self._queue.put(window)

    def _parse_previous_window(self, path: Path) -> dict | None:
        try:
            rel = path.relative_to(RECORDINGS_BASE)
            parts = rel.parts  # ('YYYY-MM-DD', 'HH', 'camera', 'MM.SS.mp4')
            if len(parts) != 4:
                return None
            date_str, hour_str, camera, filename = parts
            if self._camera_filter and camera not in self._camera_filter:
                return None
            datetime.strptime(date_str, "%Y-%m-%d")
            hour_int = int(hour_str)
            if not (0 <= hour_int <= 23):
                return None
            stem = Path(filename).stem.split(".")
            if len(stem) != 2:
                return None
            seg_minute = int(stem[0])
            if not (0 <= seg_minute <= 59):
                return None
        except (ValueError, IndexError, TypeError):
            return None

        win = previous_window(date_str, hour_str, seg_minute)
        return {"camera": camera, **win}


def collect_segments(camera: str, date: str, hour: str, window_mm: str) -> list[Path]:
    """Return sorted segment files for a given window."""
    camera_dir = RECORDINGS_BASE / date / hour / camera
    if not camera_dir.exists():
        return []
    start = int(window_mm)
    segments = []
    for seg in camera_dir.iterdir():
        if seg.suffix != ".mp4":
            continue
        parts = seg.stem.split(".")
        if len(parts) != 2:
            continue
        try:
            m = int(parts[0])
        except ValueError:
            continue
        if start <= m < start + 10:
            segments.append(seg)
    return sorted(segments)


def concatenate_segments(segments: list[Path], output_path: Path) -> bool:
    concat_list = output_path.with_suffix(".txt")
    try:
        concat_list.write_text(
            "\n".join(f"file '{seg.resolve()}'" for seg in segments) + "\n"
        )
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(output_path)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            log.error("ffmpeg failed: %s", result.stderr[-2000:])
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out for %s", output_path.name)
        return False
    except Exception as e:
        log.error("ffmpeg error: %s", e)
        return False
    finally:
        if concat_list.exists():
            concat_list.unlink()


def upload_to_s3(local_path: Path, s3_key: str, cfg: dict) -> bool:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    s3 = boto3.Session(
        aws_access_key_id=cfg["aws_access_key_id"],
        aws_secret_access_key=cfg["aws_secret_access_key"],
        region_name=cfg["aws_region"],
    ).client("s3")

    try:
        log.info("Uploading -> s3://%s/%s", cfg["s3_bucket"], s3_key)
        s3.upload_file(str(local_path), cfg["s3_bucket"], s3_key, ExtraArgs={"ContentType": "video/mp4", "StorageClass": "STANDARD_IA"})
        return True
    except (BotoCoreError, ClientError) as e:
        log.error("S3 upload failed for %s: %s", s3_key, e)
        return False


def process_window(camera: str, date: str, hour: str, window_mm: str, cfg: dict) -> bool:
    segments = collect_segments(camera, date, hour, window_mm)
    if not segments:
        log.info("No segments for %s, skipping", window_key(camera, date, hour, window_mm))
        return True  # nothing to upload; mark as done so we don't retry

    start_utc = datetime(
        *[int(x) for x in date.split("-")],
        int(hour),
        int(window_mm),
        0,
        tzinfo=timezone.utc,
    )
    s3_key = f"{camera}_{start_utc.astimezone().isoformat()}.mp4"
    log.info("Processing %s (%d segments) -> %s", window_key(camera, date, hour, window_mm), len(segments), s3_key)

    if len(segments) == 1:
        return upload_to_s3(segments[0], s3_key, cfg)

    with tempfile.TemporaryDirectory(prefix="camera_save_") as tmpdir:
        output_path = Path(tmpdir) / "window.mp4"
        if not concatenate_segments(segments, output_path):
            return False
        return upload_to_s3(output_path, s3_key, cfg)


def startup_scan(cfg: dict, processed: set, window_queue: queue.Queue) -> None:
    """Enqueue any windows that completed before startup but weren't processed."""
    if not RECORDINGS_BASE.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cfg["upload_delay_minutes"])
    camera_filter = set(cfg["cameras"])
    count = 0

    for date_dir in sorted(RECORDINGS_BASE.iterdir()):
        if not date_dir.is_dir():
            continue
        try:
            datetime.strptime(date_dir.name, "%Y-%m-%d")
        except ValueError:
            continue
        date_parts = [int(x) for x in date_dir.name.split("-")]

        for hour_dir in sorted(date_dir.iterdir()):
            if not hour_dir.is_dir():
                continue
            try:
                hour_int = int(hour_dir.name)
                if not (0 <= hour_int <= 23):
                    raise ValueError
            except ValueError:
                continue

            for camera_dir in sorted(hour_dir.iterdir()):
                if not camera_dir.is_dir():
                    continue
                camera = camera_dir.name
                if camera_filter and camera not in camera_filter:
                    continue

                windows_seen: set[int] = set()
                for seg in camera_dir.iterdir():
                    if seg.suffix != ".mp4":
                        continue
                    parts = seg.stem.split(".")
                    if len(parts) != 2:
                        continue
                    try:
                        m = int(parts[0])
                    except ValueError:
                        continue
                    windows_seen.add((m // 10) * 10)

                for w_start in windows_seen:
                    end_dt = datetime(*date_parts, hour_int, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=w_start + 10)
                    if end_dt > cutoff:
                        continue
                    key = window_key(camera, date_dir.name, hour_dir.name, f"{w_start:02d}")
                    if key not in processed:
                        window_queue.put({"camera": camera, "date": date_dir.name, "hour": hour_dir.name, "window_mm": f"{w_start:02d}"})
                        count += 1

    log.info("Startup scan enqueued %d unprocessed windows", count)


def main():
    log.info("camera-save starting")
    try:
        cfg = load_config()
    except (KeyError, FileNotFoundError) as e:
        log.error("Config error: %s", e)
        sys.exit(1)

    log.info(
        "Config: bucket=%s region=%s delay=%dm cameras=%s",
        cfg["s3_bucket"], cfg["aws_region"], cfg["upload_delay_minutes"],
        cfg["cameras"] or "all",
    )

    processed = load_processed()
    log.info("Loaded %d previously processed windows", len(processed))
    pending: set[str] = set()

    window_queue: queue.Queue = queue.Queue()
    startup_scan(cfg, processed, window_queue)

    handler = RecordingHandler(window_queue, set(cfg["cameras"]))
    observer = Observer()
    observer.schedule(handler, str(RECORDINGS_BASE), recursive=True)
    observer.start()
    log.info("Watching %s", RECORDINGS_BASE)

    try:
        while True:
            try:
                w = window_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            key = window_key(w["camera"], w["date"], w["hour"], w["window_mm"])
            if key in processed or key in pending:
                continue
            pending.add(key)

            try:
                success = process_window(w["camera"], w["date"], w["hour"], w["window_mm"], cfg)
            except Exception as e:
                log.exception("Unexpected error processing %s: %s", key, e)
                success = False

            pending.discard(key)
            if success:
                processed.add(key)
                save_processed(processed)
            else:
                log.warning("Failed to process %s — will not retry until next file event triggers it", key)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()

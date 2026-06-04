#!/usr/bin/env python3
"""
Sync Frigate NVR recordings to S3 as 10-minute concatenated windows.

Frigate layout:  /media/frigate/recordings/YYYY-MM-DD/HH/<camera>/MM.SS.mp4
S3 key:          {camera}_{start_time.isoformat()}.mp4
                 (start_time in local timezone, e.g. front_door_2024-01-15T14:30:00+11:00.mp4)
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

RECORDINGS_BASE = Path("/media/frigate/recordings")
PROCESSED_FILE = Path("/data/processed.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("camera-save")


def load_config() -> dict:
    cameras_json = os.environ.get("CAMERAS_JSON", "[]")
    try:
        cameras = json.loads(cameras_json)
        if not isinstance(cameras, list):
            cameras = []
    except json.JSONDecodeError:
        cameras = []

    return {
        "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
        "aws_region": os.environ.get("AWS_REGION", "ap-southeast-2"),
        "s3_bucket": os.environ["S3_BUCKET"],
        "upload_delay_minutes": int(os.environ.get("UPLOAD_DELAY_MINUTES", "15")),
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


def window_start_minute(minute: int) -> int:
    return (minute // 10) * 10


def find_eligible_windows(cfg: dict, processed: set, cutoff: datetime) -> list[dict]:
    camera_filter = set(cfg["cameras"])
    eligible = []

    if not RECORDINGS_BASE.exists():
        log.warning("Recordings base %s does not exist", RECORDINGS_BASE)
        return []

    for date_dir in sorted(RECORDINGS_BASE.iterdir()):
        if not date_dir.is_dir():
            continue
        date_str = date_dir.name
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

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
                camera_name = camera_dir.name

                if camera_filter and camera_name not in camera_filter:
                    continue

                windows_map: dict[int, list[Path]] = {}
                for seg in sorted(camera_dir.iterdir()):
                    if seg.suffix != ".mp4":
                        continue
                    parts = seg.stem.split(".")
                    if len(parts) != 2:
                        continue
                    try:
                        seg_minute = int(parts[0])
                        seg_second = int(parts[1])
                        if not (0 <= seg_minute <= 59 and 0 <= seg_second <= 59):
                            raise ValueError
                    except ValueError:
                        continue
                    windows_map.setdefault(window_start_minute(seg_minute), []).append(seg)

                date_parts = [int(x) for x in date_str.split("-")]
                for w_start, segments in windows_map.items():
                    end_minute = w_start + 10
                    end_hour = hour_int + end_minute // 60
                    end_minute = end_minute % 60
                    window_end = datetime(
                        date_parts[0], date_parts[1], date_parts[2],
                        end_hour % 24, end_minute, 0,
                        tzinfo=timezone.utc,
                    )
                    if window_end > cutoff:
                        continue

                    window_mm = f"{w_start:02d}"
                    key = f"{camera_name}/{date_str}/{hour_dir.name}/{window_mm}"
                    if key in processed:
                        continue

                    window_start_utc = datetime(
                        date_parts[0], date_parts[1], date_parts[2],
                        hour_int, w_start, 0,
                        tzinfo=timezone.utc,
                    )
                    eligible.append({
                        "camera": camera_name,
                        "date": date_str,
                        "hour": hour_dir.name,
                        "window_mm": window_mm,
                        "key": key,
                        "segments": sorted(segments),
                        "start_utc": window_start_utc,
                    })

    return eligible


def concatenate_segments(segments: list[Path], output_path: Path) -> bool:
    concat_list = output_path.with_suffix(".txt")
    try:
        concat_list.write_text(
            "\n".join(f"file '{seg.resolve()}'" for seg in segments) + "\n"
        )
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                str(output_path),
            ],
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
        s3.upload_file(
            str(local_path),
            cfg["s3_bucket"],
            s3_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )
        return True
    except (BotoCoreError, ClientError) as e:
        log.error("S3 upload failed for %s: %s", s3_key, e)
        return False


def process_window(window: dict, cfg: dict) -> bool:
    segments = window["segments"]
    camera = window["camera"]
    start_local = window["start_utc"].astimezone()
    s3_key = f"{camera}_{start_local.isoformat()}.mp4"

    log.info("Processing %s (%d segments) -> %s", window["key"], len(segments), s3_key)

    if len(segments) == 1:
        return upload_to_s3(segments[0], s3_key, cfg)

    with tempfile.TemporaryDirectory(prefix="camera_save_") as tmpdir:
        output_path = Path(tmpdir) / "window.mp4"
        if not concatenate_segments(segments, output_path):
            return False
        return upload_to_s3(output_path, s3_key, cfg)


def run_cycle(cfg: dict, processed: set) -> set:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cfg["upload_delay_minutes"])
    log.info("Scanning for windows before %s", cutoff.isoformat())

    windows = find_eligible_windows(cfg, processed, cutoff)
    log.info("Found %d eligible windows", len(windows))

    for window in windows:
        if process_window(window, cfg):
            processed.add(window["key"])
            save_processed(processed)
        else:
            log.warning("Failed to process %s, will retry next cycle", window["key"])

    return processed


def main():
    log.info("camera-save starting")
    try:
        cfg = load_config()
    except KeyError as e:
        log.error("Missing required config: %s", e)
        sys.exit(1)

    log.info(
        "Config: bucket=%s region=%s delay=%dm cameras=%s",
        cfg["s3_bucket"], cfg["aws_region"], cfg["upload_delay_minutes"],
        cfg["cameras"] or "all",
    )

    sleep_seconds = max(cfg["upload_delay_minutes"] * 30, 60)
    log.info("Checking every %ds", sleep_seconds)

    processed = load_processed()
    log.info("Loaded %d previously processed windows", len(processed))

    while True:
        try:
            processed = run_cycle(cfg, processed)
        except Exception as e:
            log.exception("Unhandled error in cycle: %s", e)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Monotonic Apple Books <-> BOOX reading-progress merger.

This synchronizes displayed percentage and read/finished status. Engine-specific
EPUB resume anchors are deliberately left untouched.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import boox_collections as bc
import sync_config as config


APP_DIR = config.APP_DIR
STATE_DIR = config.STATE_DIR
ANNOTATION_DB = config.ANNOTATION_DB
SYNTHETIC_TOTAL = 10_000


class UnionFind:
    def __init__(self):
        self.parent: dict[tuple[str, object], tuple[str, object]] = {}

    def find(self, item):
        self.parent.setdefault(item, item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left, right):
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def parse_boox_progress(value: str | None) -> tuple[int, int, float]:
    if not value or value == "NULL" or "/" not in value:
        return 0, 0, 0.0
    try:
        current, total = (int(part) for part in value.split("/", 1))
    except ValueError:
        return 0, 0, 0.0
    if total <= 0:
        return current, total, 0.0
    return current, total, max(0.0, min(1.0, current / total))


def clean_progress(value) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if 0.0 <= number <= 1.0 else 0.0


def apple_rows() -> dict[int, dict]:
    connection = sqlite3.connect(bc.BOOKS_DB)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT Z_PK, ZTITLE, COALESCE(ZAUTHOR,'') AS ZAUTHOR, ZPATH,
                   ZREADINGPROGRESS, ZBOOKHIGHWATERMARKPROGRESS,
                   COALESCE(ZISFINISHED,0) AS ZISFINISHED,
                   COALESCE(ZNOTFINISHED,0) AS ZNOTFINISHED, Z_OPT
            FROM ZBKLIBRARYASSET
            WHERE COALESCE(ZISHIDDEN,0)=0 AND COALESCE(ZISSAMPLE,0)=0
            """
        ).fetchall()
    finally:
        connection.close()
    return {int(row["Z_PK"]): dict(row) for row in rows}


def boox_rows() -> dict[str, dict]:
    _, rows = bc.content_query(
        "Metadata",
        (
            "id",
            "title",
            "nativeAbsolutePath",
            "type",
            "progress",
            "readingStatus",
            "lastAccess",
            "updatedAt",
            "status",
        ),
    )
    return {row["nativeAbsolutePath"]: row for row in rows if row["status"] == "0"}


def build_components() -> tuple[list[dict], dict[int, dict], dict[str, dict]]:
    catalog = bc.make_plan()
    apples = apple_rows()
    boox = boox_rows()
    union = UnionFind()

    for match in catalog["matches"]:
        union.union(("apple", match["apple_id"]), ("boox", match["path"]))

    # Add the reverse best match so aliases and duplicate formats form one
    # logical component instead of silently leaving two Apple assets isolated.
    for asset in catalog["apple_assets"]:
        choices = []
        for book in catalog["active_books"]:
            title = "" if book["title"] == "NULL" else book["title"]
            filename = Path(book["nativeAbsolutePath"]).stem
            score = max(bc.title_score(title, asset["title"]), bc.title_score(filename, asset["title"]))
            choices.append((score, book["nativeAbsolutePath"]))
        score, path = max(choices, key=lambda pair: pair[0])
        if score >= config.MATCH_THRESHOLD:
            union.union(("apple", asset["id"]), ("boox", path))

    groups: dict[tuple[str, object], dict[str, set]] = defaultdict(lambda: {"apple_ids": set(), "paths": set()})
    for node in union.parent:
        group = groups[union.find(node)]
        if node[0] == "apple":
            group["apple_ids"].add(int(node[1]))
        else:
            group["paths"].add(str(node[1]))

    components = []
    for group in groups.values():
        apple_ids = sorted(asset_id for asset_id in group["apple_ids"] if asset_id in apples)
        paths = sorted(path for path in group["paths"] if path in boox)
        if not apple_ids or not paths:
            continue
        signals = []
        explicit_finished = False
        for asset_id in apple_ids:
            row = apples[asset_id]
            signals.append(clean_progress(row["ZREADINGPROGRESS"]))
            explicit_finished = explicit_finished or bool(row["ZISFINISHED"])
        for path in paths:
            row = boox[path]
            _, _, ratio = parse_boox_progress(row["progress"])
            signals.append(ratio)
            explicit_finished = explicit_finished or row["readingStatus"] == "2"
        merged = 1.0 if explicit_finished else max(signals, default=0.0)
        finished = explicit_finished or merged >= 0.9999
        title = apples[apple_ids[0]]["ZTITLE"] or boox[paths[0]]["title"]
        components.append(
            {
                "apple_ids": apple_ids,
                "paths": paths,
                "title": title,
                "merged": merged,
                "finished": finished,
            }
        )
    return components, apples, boox


def component_stats(components: list[dict], apples: dict[int, dict], boox: dict[str, dict]) -> dict:
    stats = defaultdict(int)
    for component in components:
        apple_signal = max(
            [
                1.0 if apples[asset_id]["ZISFINISHED"] else clean_progress(apples[asset_id]["ZREADINGPROGRESS"])
                for asset_id in component["apple_ids"]
            ],
            default=0.0,
        )
        boox_signal = max(
            [
                1.0 if boox[path]["readingStatus"] == "2" else parse_boox_progress(boox[path]["progress"])[2]
                for path in component["paths"]
            ],
            default=0.0,
        )
        if apple_signal and boox_signal:
            stats["both"] += 1
        elif apple_signal:
            stats["apple_only"] += 1
        elif boox_signal:
            stats["boox_only"] += 1
        else:
            stats["neither"] += 1
        if abs(apple_signal - boox_signal) >= 0.03:
            stats["conflicts"] += 1
        if component["merged"] > 0:
            stats["with_signal"] += 1
    return dict(stats)


def serializable_plan(components: list[dict], apples: dict[int, dict], boox: dict[str, dict]) -> dict:
    return {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "stats": component_stats(components, apples, boox),
        "components": components,
        "apple": {str(key): value for key, value in apples.items()},
        "boox": boox,
    }


def create_backup(components: list[dict], apples: dict[int, dict], boox: dict[str, dict]) -> Path:
    directory = STATE_DIR / "backups" / f"reading-progress-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    directory.mkdir(parents=True, exist_ok=False)
    for source, name in ((bc.BOOKS_DB, "AppleBooks.sqlite"), (ANNOTATION_DB, "AppleAnnotations.sqlite")):
        source_connection = sqlite3.connect(source)
        destination_connection = sqlite3.connect(directory / name)
        try:
            source_connection.backup(destination_connection)
        finally:
            destination_connection.close()
            source_connection.close()
    (directory / "sync-plan-before.json").write_text(
        json.dumps(serializable_plan(components, apples, boox), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return directory


def pilot_components(components: list[dict], apples: dict[int, dict], boox: dict[str, dict]) -> list[dict]:
    candidates = []
    for component in components:
        if component["merged"] <= 0:
            continue
        apple_signal = max(
            [1.0 if apples[i]["ZISFINISHED"] else clean_progress(apples[i]["ZREADINGPROGRESS"]) for i in component["apple_ids"]],
            default=0.0,
        )
        boox_signal = max(
            [1.0 if boox[p]["readingStatus"] == "2" else parse_boox_progress(boox[p]["progress"])[2] for p in component["paths"]],
            default=0.0,
        )
        file_type = boox[component["paths"][0]]["type"]
        direction = "apple" if apple_signal > boox_signal else "boox"
        candidates.append((component, file_type, direction, abs(apple_signal - boox_signal)))
    selected = []
    desired = [("pdf", "apple"), ("pdf", "boox"), ("epub", "apple"), ("epub", "boox")]
    for file_type, direction in desired:
        matches = [item for item in candidates if item[1] == file_type and item[2] == direction and item[0] not in selected]
        if matches:
            selected.append(max(matches, key=lambda item: item[3])[0])
    remaining = [
        item
        for item in candidates
        if item[0] not in selected and item[1] == "epub" and not item[0]["finished"]
    ]
    if remaining:
        selected.append(max(remaining, key=lambda item: item[3])[0])
    return selected[:5]


def quit_readers() -> None:
    subprocess.run(["osascript", "-e", 'tell application "Books" to quit'], capture_output=True, text=True)
    for _ in range(20):
        result = subprocess.run(["pgrep", "-x", "Books"], capture_output=True)
        if result.returncode != 0:
            break
        time.sleep(0.25)
    subprocess.run(["adb", "shell", "am", "force-stop", "com.onyx.kreader"], check=True)


def reopen_readers() -> None:
    subprocess.run(["open", "-gj", "-a", "Books"], check=False)
    subprocess.run(
        ["adb", "shell", "am", "start", "--activity-clear-top", "-a", "com.onyx.action.LIBRARY", "-c", "android.intent.category.DEFAULT"],
        check=False,
        capture_output=True,
        text=True,
    )


def apply_apple(components: list[dict]) -> int:
    connection = sqlite3.connect(bc.BOOKS_DB)
    changed = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        for component in components:
            if component["merged"] <= 0:
                continue
            for asset_id in component["apple_ids"]:
                row = connection.execute(
                    "SELECT ZREADINGPROGRESS,ZBOOKHIGHWATERMARKPROGRESS,COALESCE(ZISFINISHED,0) FROM ZBKLIBRARYASSET WHERE Z_PK=?",
                    (asset_id,),
                ).fetchone()
                if row is None:
                    continue
                current, high, finished = clean_progress(row[0]), clean_progress(row[1]), bool(row[2])
                target = max(current, component["merged"])
                target_finished = finished or component["finished"]
                if abs(current - target) < 1e-7 and high >= target - 1e-7 and finished == target_finished:
                    continue
                connection.execute(
                    """
                    UPDATE ZBKLIBRARYASSET
                    SET ZREADINGPROGRESS=?, ZBOOKHIGHWATERMARKPROGRESS=?,
                        ZISFINISHED=?, ZNOTFINISHED=NULL, Z_OPT=COALESCE(Z_OPT,0)+1
                    WHERE Z_PK=?
                    """,
                    (target, max(high, target), 1 if target_finished else None, asset_id),
                )
                changed += 1
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return changed


def apply_boox(components: list[dict], original_boox: dict[str, dict]) -> int:
    operations = []
    for component in components:
        if component["merged"] <= 0:
            continue
        for path in component["paths"]:
            row = original_boox[path]
            current, total, ratio = parse_boox_progress(row["progress"])
            if total <= 0:
                total = SYNTHETIC_TOTAL
            target_current = total if component["finished"] else max(1, min(total - 1, round(component["merged"] * total)))
            target_progress = f"{target_current}/{total}"
            target_status = 2 if component["finished"] else 1
            if ratio >= component["merged"] - (1 / total) and row["readingStatus"] == str(target_status):
                continue
            operations.append((row["id"], target_progress, target_status, path))

    now = int(time.time() * 1000)
    for index, (row_id, progress, reading_status, path) in enumerate(operations, 1):
        bc.content_mutation(
            "update",
            "Metadata",
            bc.bind("progress", "s", progress)
            + bc.bind("readingStatus", "i", reading_status)
            + bc.bind("userDataSyncStatus", "i", 1)
            + bc.bind("updatedAt", "l", now),
            f"id={row_id}",
        )
        if index % 10 == 0 or index == len(operations):
            print(f"BOOX progress writes: {index}/{len(operations)}", flush=True)
    return len(operations)


def verify_components(expected: list[dict]) -> tuple[bool, list[str]]:
    apples = apple_rows()
    boox = boox_rows()
    failures = []
    for component in expected:
        if component["merged"] <= 0:
            continue
        for asset_id in component["apple_ids"]:
            row = apples[asset_id]
            value = clean_progress(row["ZREADINGPROGRESS"])
            if component["finished"]:
                value = 1.0 if row["ZISFINISHED"] else value
            if value + 1e-5 < component["merged"]:
                failures.append(f"Apple {asset_id} {row['ZTITLE']}: {value:.4f} < {component['merged']:.4f}")
        for path in component["paths"]:
            row = boox[path]
            _, total, value = parse_boox_progress(row["progress"])
            tolerance = 1 / max(total, 1)
            if value + tolerance + 1e-6 < component["merged"]:
                failures.append(f"BOOX {path}: {value:.4f} < {component['merged']:.4f}")
            if component["finished"] and row["readingStatus"] != "2":
                failures.append(f"BOOX {path}: finished status not set")
    return not failures, failures


def print_plan(components: list[dict], apples: dict[int, dict], boox: dict[str, dict]) -> None:
    stats = component_stats(components, apples, boox)
    print(f"Logical components: {len(components)}")
    for key in ("both", "apple_only", "boox_only", "neither", "conflicts", "with_signal"):
        print(f"  {key}: {stats.get(key, 0)}")
    pilot = pilot_components(components, apples, boox)
    print("Pilot:")
    for component in pilot:
        print(f"  {component['title']} -> {component['merged']:.1%} finished={component['finished']}")


def sync() -> int:
    components, apples, boox = build_components()
    print_plan(components, apples, boox)
    backup_dir = create_backup(components, apples, boox)
    print(f"Backup: {backup_dir}", flush=True)
    quit_readers()
    pilot = pilot_components(components, apples, boox)
    apple_changed = apply_apple(pilot)
    boox_changed = apply_boox(pilot, boox)
    ok, failures = verify_components(pilot)
    print(f"Pilot writes: Apple={apple_changed}, BOOX={boox_changed}, verified={ok}", flush=True)
    if not ok:
        print("Pilot verification failed; bulk sync was not started.")
        for failure in failures:
            print(f"  {failure}")
        reopen_readers()
        return 1

    # Re-read BOOX after the pilot so bulk application skips already-correct rows.
    _, _, current_boox = build_components()
    apple_changed += apply_apple(components)
    boox_changed += apply_boox(components, current_boox)
    ok, failures = verify_components(components)
    print(f"Bulk writes total: Apple={apple_changed}, BOOX={boox_changed}, verified={ok}", flush=True)
    report = {
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "backup": str(backup_dir),
        "apple_writes": apple_changed,
        "boox_writes": boox_changed,
        "verified_before_reopen": ok,
        "failures": failures,
    }
    (backup_dir / "sync-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not ok:
        for failure in failures[:30]:
            print(f"  {failure}")
        reopen_readers()
        return 1

    reopen_readers()
    time.sleep(5)
    ok_after, failures_after = verify_components(components)
    print(f"Post-reopen verification: {ok_after}", flush=True)
    if not ok_after:
        for failure in failures_after[:30]:
            print(f"  {failure}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "sync"))
    args = parser.parse_args()
    if args.command == "sync":
        return sync()
    components, apples, boox = build_components()
    print_plan(components, apples, boox)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

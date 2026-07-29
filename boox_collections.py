#!/usr/bin/env python3
"""Align BOOX Library collections with the current Apple Books collections."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sqlite3
import subprocess
import time
import unicodedata
import uuid
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import sync_config as config

APP_DIR = config.APP_DIR
STATE_DIR = config.STATE_DIR
BOOKS_DB = config.BOOKS_DB
AUTHORITY = "content://com.onyx.content.database.ContentProvider"
EBOOK_RE = re.compile(r"\.(?:epub|pdf|mobi|azw3?|djvu|fb2|cbz|cbr|rtf|docx?)$", re.I)


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def adb(*args: str, check: bool = True) -> str:
    result = run(["adb", *args], check=check)
    if check and result.stderr.strip():
        # Android's content command sometimes uses stderr for diagnostics.
        if "Warning:" not in result.stderr:
            raise RuntimeError(result.stderr.strip())
    return result.stdout


def content_query(table: str, fields: tuple[str, ...]) -> tuple[str, list[dict[str, str]]]:
    output = adb(
        "shell",
        "content",
        "query",
        "--uri",
        f"{AUTHORITY}/{table}",
        "--projection",
        ":".join(fields),
    )
    return output, parse_rows(output, fields)


def parse_rows(output: str, fields: tuple[str, ...]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.startswith("Row: "):
            continue
        body = re.sub(r"^Row: \d+ ", "", line)
        positions: list[tuple[int, str, int]] = []
        start = 0
        valid = True
        for index, field in enumerate(fields):
            marker = f"{field}=" if index == 0 else f", {field}="
            position = body.find(marker, start)
            if position < 0:
                valid = False
                break
            value_start = position + len(marker)
            positions.append((position, field, value_start))
            start = value_start
        if not valid:
            raise ValueError(f"Cannot parse provider row: {line}")
        row: dict[str, str] = {}
        for index, (_, field, value_start) in enumerate(positions):
            value_end = positions[index + 1][0] if index + 1 < len(positions) else len(body)
            row[field] = body[value_start:value_end]
        rows.append(row)
    return rows


def normalized(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = EBOOK_RE.sub("", value)
    return "".join(char for char in value if char.isalnum())


def core_title(value: str | None) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = EBOOK_RE.sub("", value)
    value = re.sub(r"[（(【\[].*?[）)】\]]", "", value)
    value = re.split(r"--|—{2,}", value)[0]
    value = re.sub(
        r"(z-library|anna.?s archive|translated|文字版合集含目录|无水印付费贴).*$",
        "",
        value,
        flags=re.I,
    )
    return "".join(char for char in value if char.isalnum())


def serial_issue(value: str | None):
    value = unicodedata.normalize("NFKC", value or "").lower().replace("｜", " ")
    if re.search(r"周刊|weekly|issue|\bvol\.?", value):
        match = re.search(r"(?:vol\.?|issue|no\.?|第)?\s*(\d+)", value)
        return ("serial", match.group(1) if match else normalized(value))
    if re.search(r"月刊|monthly", value):
        match = re.search(
            r"(20\d{2})[-./年 ]?(0?[1-9]|1[0-2])|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(\d{4})?",
            value,
        )
        return ("month", "".join(part or "" for part in match.groups()) if match else normalized(value))
    return None


def title_score(left: str | None, right: str | None) -> float:
    left_issue, right_issue = serial_issue(left), serial_issue(right)
    if bool(left_issue) != bool(right_issue):
        return 0.0
    if left_issue and right_issue and left_issue != right_issue:
        return 0.0
    left_full, right_full = normalized(left), normalized(right)
    left_core, right_core = core_title(left), core_title(right)
    if not left_full or not right_full:
        return 0.0
    if left_full == right_full:
        return 1.0
    values = [SequenceMatcher(None, left_full, right_full).ratio()]
    if left_core and right_core:
        values.append(SequenceMatcher(None, left_core, right_core).ratio())
        if left_core == right_core:
            values.append(0.98)
    for first, second in ((left_full, right_full), (left_core, right_core)):
        if not first or not second:
            continue
        shorter, longer = (first, second) if len(first) <= len(second) else (second, first)
        if shorter not in longer:
            continue
        ratio = len(shorter) / len(longer)
        if longer.startswith(shorter) or longer.endswith(shorter):
            values.append(0.94)
        elif len(shorter) >= 4 and ratio >= 0.35:
            values.append(0.90)
        elif len(shorter) >= 8:
            values.append(0.84)
    return max(values)


def apple_assets(categories: tuple[str, ...]) -> list[dict]:
    placeholders = ",".join("?" for _ in categories)
    category_join = (
        f"c.Z_PK=m.ZCOLLECTION AND c.ZTITLE IN ({placeholders})"
        if categories
        else "1=0"
    )
    connection = sqlite3.connect(BOOKS_DB)
    try:
        rows = connection.execute(
            f"""
            SELECT a.Z_PK, a.ZTITLE, COALESCE(a.ZAUTHOR,''), c.ZTITLE
            FROM ZBKLIBRARYASSET a
            LEFT JOIN ZBKCOLLECTIONMEMBER m ON a.Z_PK=m.ZASSET
            LEFT JOIN ZBKCOLLECTION c ON {category_join}
            WHERE COALESCE(a.ZISHIDDEN,0)=0
              AND COALESCE(a.ZISSAMPLE,0)=0
            ORDER BY a.Z_PK
            """,
            categories,
        ).fetchall()
    finally:
        connection.close()
    by_id: dict[int, dict] = {}
    for asset_id, title, author, category in rows:
        asset = by_id.setdefault(
            asset_id,
            {"id": asset_id, "title": title or "", "author": author or "", "categories": set()},
        )
        if category:
            asset["categories"].add(category)
    return list(by_id.values())


def current_ebook_paths() -> set[str]:
    command = (
        "find /storage/emulated/0 -type f \\( "
        '-iname "*.epub" -o -iname "*.pdf" -o -iname "*.mobi" -o '
        '-iname "*.azw" -o -iname "*.azw3" -o -iname "*.djvu" -o '
        '-iname "*.fb2" -o -iname "*.cbz" -o -iname "*.cbr" -o '
        '-iname "*.rtf" -o -iname "*.doc" -o -iname "*.docx" \\)'
    )
    return {line.strip() for line in adb("shell", command).splitlines() if line.strip()}


def provider_state() -> dict:
    library_fields = ("id", "name", "idString", "status", "createdAt", "updatedAt")
    metadata_fields = ("id", "title", "authors", "nativeAbsolutePath", "status", "type", "idString", "hashTag")
    member_fields = ("id", "documentUniqueId", "libraryUniqueId", "uuid", "status", "createdAt", "updatedAt")
    library_raw, libraries = content_query("Library", library_fields)
    metadata_raw, metadata = content_query("Metadata", metadata_fields)
    members_raw, members = content_query("MetadataCollection", member_fields)
    return {
        "libraries": libraries,
        "metadata": metadata,
        "members": members,
        "raw": {"Library": library_raw, "Metadata": metadata_raw, "MetadataCollection": members_raw},
    }


def make_plan() -> dict:
    categories = config.collection_names()
    state = provider_state()
    paths = current_ebook_paths()
    apples = apple_assets(categories)
    if not apples:
        raise RuntimeError("No visible Apple Books assets belong to the selected collections")
    active_books = [
        row
        for row in state["metadata"]
        if row["status"] == "0" and row["nativeAbsolutePath"] in paths and EBOOK_RE.search(row["nativeAbsolutePath"])
    ]
    matches = []
    for book in active_books:
        filename = Path(book["nativeAbsolutePath"]).stem
        candidates = []
        for asset in apples:
            score = max(title_score(book["title"] if book["title"] != "NULL" else "", asset["title"]), title_score(filename, asset["title"]))
            candidates.append((score, asset))
        score, asset = max(candidates, key=lambda pair: pair[0])
        matches.append(
            {
                "path": book["nativeAbsolutePath"],
                "boox_title": book["title"] if book["title"] != "NULL" else filename,
                "apple_id": asset["id"],
                "apple_title": asset["title"],
                "score": score,
                "categories": sorted(asset["categories"], key=categories.index),
            }
        )
    matched = [item for item in matches if item["score"] >= config.MATCH_THRESHOLD]
    unmatched = [item for item in matches if item["score"] < config.MATCH_THRESHOLD]
    apple_best = []
    for asset in apples:
        choices = [
            (max(title_score(book["title"] if book["title"] != "NULL" else "", asset["title"]), title_score(Path(book["nativeAbsolutePath"]).stem, asset["title"])), book)
            for book in active_books
        ]
        if choices:
            score, book = max(choices, key=lambda pair: pair[0])
            path = book["nativeAbsolutePath"]
        else:
            score, path = 0.0, ""
        apple_best.append({"id": asset["id"], "title": asset["title"], "score": score, "path": path})
    absent_apple = [item for item in apple_best if item["score"] < config.MATCH_THRESHOLD]
    active_paths = {row["nativeAbsolutePath"] for row in active_books}
    unindexed_paths = sorted(paths - active_paths)
    unindexed_matches = []
    still_absent = []
    for item in absent_apple:
        asset = next(asset for asset in apples if asset["id"] == item["id"])
        candidates = [(title_score(Path(path).stem, asset["title"]), path) for path in unindexed_paths]
        if candidates:
            score, path = max(candidates, key=lambda pair: pair[0])
        else:
            score, path = 0.0, ""
        if score >= config.MATCH_THRESHOLD:
            unindexed_matches.append(
                {
                    "path": path,
                    "apple_id": asset["id"],
                    "apple_title": asset["title"],
                    "apple_author": asset["author"],
                    "score": score,
                    "categories": sorted(asset["categories"], key=categories.index),
                }
            )
        else:
            still_absent.append(item)
    category_counts: dict[str, int] = defaultdict(int)
    for item in matched:
        for category in item["categories"]:
            category_counts[category] += 1
    return {
        "categories": categories,
        "state": state,
        "paths": paths,
        "apple_assets": apples,
        "active_books": active_books,
        "matches": matched,
        "unmatched": unmatched,
        "absent_apple": still_absent,
        "unindexed_matches": unindexed_matches,
        "category_counts": dict(category_counts),
    }


def print_plan(plan: dict) -> None:
    print(f"Apple assets: {len(plan['apple_assets'])}")
    print(f"BOOX ebook files: {len(plan['paths'])}")
    print(f"BOOX active catalog books: {len(plan['active_books'])}")
    print(f"Matched BOOX books: {len(plan['matches'])}")
    print(f"Unmatched BOOX books: {len(plan['unmatched'])}")
    print(f"Unindexed files matched to Apple assets: {len(plan['unindexed_matches'])}")
    print(f"Apple assets absent from BOOX files: {len(plan['absent_apple'])}")
    for category in plan["categories"]:
        print(f"  {category}: {plan['category_counts'].get(category, 0)}")
    for item in plan["unmatched"]:
        print(f"UNMATCHED BOOX {item['boox_title']} -> {item['apple_title']} ({item['score']:.3f})")
    for item in plan["absent_apple"]:
        print(f"ABSENT APPLE {item['title']} -> {item['path']} ({item['score']:.3f})")
    for item in plan["unindexed_matches"]:
        print(f"UNINDEXED {item['path']} -> {item['apple_title']} ({item['score']:.3f})")
    low = sorted((item for item in plan["matches"] if item["score"] < 0.90), key=lambda item: item["score"])
    for item in low:
        print(f"LOW MATCH {item['boox_title']} -> {item['apple_title']} ({item['score']:.3f})")


def bind(name: str, kind: str, value: str | int) -> list[str]:
    return ["--bind", f"{name}:{kind}:{value}"]


def content_mutation(action: str, table: str, bindings: list[str], where: str | None = None) -> str:
    command = ["content", action, "--uri", f"{AUTHORITY}/{table}"]
    command += bindings
    if where is not None:
        command += ["--where", where]
    return adb("shell", " ".join(shlex.quote(part) for part in command))


def backup(plan: dict) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    directory = STATE_DIR / "backups" / f"boox-library-backup-{timestamp}"
    directory.mkdir(parents=True, exist_ok=False)
    for table, raw in plan["state"]["raw"].items():
        (directory / f"{table}.txt").write_text(raw, encoding="utf-8")
    summary = {
        "created_at": timestamp,
        "device": adb("get-serialno").strip(),
        "library_rows": len(plan["state"]["libraries"]),
        "metadata_rows": len(plan["state"]["metadata"]),
        "member_rows": len(plan["state"]["members"]),
    }
    (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return directory


def remote_file_info(path: str) -> tuple[int, int, str]:
    quoted = shlex.quote(path)
    stat_output = adb("shell", f"stat -c '%s|%Y' {quoted}").strip()
    size_text, modified_text = stat_output.split("|", 1)
    digest = adb("shell", f"md5sum {quoted}").split()[0]
    return int(size_text), int(modified_text) * 1000, digest


def register_unindexed_books(plan: dict) -> int:
    inserted = 0
    for item in plan["unindexed_matches"]:
        path = item["path"]
        size, last_modified, digest = remote_file_info(path)
        now = int(time.time() * 1000)
        doc_uuid = uuid.uuid4().hex
        # Keep the initial command small: Android's remote shell has a short
        # command-line limit, and one imported title/path is exceptionally long.
        bindings = (
            bind("nativeAbsolutePath", "s", path)
            + bind("type", "s", Path(path).suffix.lower().lstrip("."))
            + bind("status", "i", 0)
            + bind("uuid", "s", doc_uuid)
            + bind("idString", "s", path)
            + bind("createdAt", "l", now)
            + bind("updatedAt", "l", now)
        )
        content_mutation("insert", "Metadata", bindings)
        _, inserted_rows = content_query("Metadata", ("id", "nativeAbsolutePath", "status"))
        inserted_row = next(
            row for row in reversed(inserted_rows) if row["nativeAbsolutePath"] == path and row["status"] == "0"
        )
        row_id = inserted_row["id"]
        content_mutation(
            "update",
            "Metadata",
            bind("name", "s", Path(path).name)
            + bind("title", "s", item["apple_title"])
            + bind("authors", "s", item["apple_author"]),
            f"id={row_id}",
        )
        content_mutation(
            "update",
            "Metadata",
            bind("location", "s", path) + bind("nocasePath", "s", path),
            f"id={row_id}",
        )
        content_mutation(
            "update",
            "Metadata",
            bind("size", "l", size)
            + bind("lastAccess", "l", now)
            + bind("lastModified", "l", last_modified)
            + bind("favorite", "i", 0)
            + bind("rating", "i", 0)
            + bind("readingStatus", "i", 0)
            + bind("hashTag", "s", digest),
            f"id={row_id}",
        )
        content_mutation(
            "update",
            "Metadata",
            bind("fetchSource", "i", 0)
            + bind("ordinal", "i", 0)
            + bind("encryptionType", "i", 0)
            + bind("drmType", "i", 1)
            + bind("fileOriginSize", "l", size)
            + bind("fileSyncStatus", "i", 0)
            + bind("userDataSyncStatus", "i", 1)
            + bind("extraInfo", "s", '{"autoSync":false,"embedPdfAfterSync":true,"syncStyle":true}'),
            f"id={row_id}",
        )
        inserted += 1
        print(f"Indexed {inserted}/{len(plan['unindexed_matches'])}: {Path(path).name}", flush=True)
    return inserted


def ensure_libraries(plan: dict) -> tuple[dict[str, str], int, int]:
    now = int(time.time() * 1000)
    active = {row["name"]: row for row in plan["state"]["libraries"] if row["status"] == "0"}
    created = 0
    ids: dict[str, str] = {}
    for category in plan["categories"]:
        if category in active:
            ids[category] = active[category]["idString"]
            continue
        unique_id = uuid.uuid4().hex
        bindings = (
            bind("name", "s", category)
            + bind("fetchSource", "i", 0)
            + bind("encryptionType", "i", 0)
            + bind("syncStatus", "i", 1)
            + bind("status", "i", 0)
            + bind("idString", "s", unique_id)
            + bind("createdAt", "l", now)
            + bind("updatedAt", "l", now)
        )
        content_mutation("insert", "Library", bindings)
        ids[category] = unique_id
        created += 1
    return ids, created, 0


def apply_plan(plan: dict) -> None:
    if plan["unmatched"] or plan["absent_apple"]:
        raise RuntimeError("Refusing to mutate: catalog matching is incomplete")
    backup_dir = backup(plan)
    print(f"Backup: {backup_dir}", flush=True)
    registered = register_unindexed_books(plan)
    if registered:
        plan = make_plan()
        if plan["unmatched"] or plan["absent_apple"] or plan["unindexed_matches"]:
            raise RuntimeError("Catalog registration did not converge; backup is available")
        print(f"Catalog books registered: {registered}", flush=True)
    library_ids, created, renamed = ensure_libraries(plan)
    print(f"Libraries: created={created}, renamed={renamed}", flush=True)

    _, current_members = content_query(
        "MetadataCollection",
        ("id", "documentUniqueId", "libraryUniqueId", "uuid", "status", "createdAt", "updatedAt"),
    )
    by_pair: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in current_members:
        by_pair[(row["documentUniqueId"], row["libraryUniqueId"])].append(row)

    inserted = reactivated = skipped = retired = 0
    operations = [(item["path"], category) for item in plan["matches"] for category in item["categories"]]
    expected_pairs = {(path, library_ids[category]) for path, category in operations}
    target_library_ids = set(library_ids.values())
    active_paths = {row["nativeAbsolutePath"] for row in plan["active_books"]}
    now = int(time.time() * 1000)
    for row in current_members:
        pair = (row["documentUniqueId"], row["libraryUniqueId"])
        if (
            row["status"] == "0"
            and row["documentUniqueId"] in active_paths
            and row["libraryUniqueId"] in target_library_ids
            and pair not in expected_pairs
        ):
            content_mutation(
                "update",
                "MetadataCollection",
                bind("status", "i", 1) + bind("updatedAt", "l", now),
                f"id={row['id']}",
            )
            retired += 1
    for index, (path, category) in enumerate(operations, 1):
        library_id = library_ids[category]
        existing = by_pair.get((path, library_id), [])
        if any(row["status"] == "0" for row in existing):
            skipped += 1
        elif existing:
            row = existing[-1]
            now = int(time.time() * 1000)
            content_mutation(
                "update",
                "MetadataCollection",
                bind("status", "i", 0) + bind("updatedAt", "l", now),
                f"id={row['id']}",
            )
            reactivated += 1
        else:
            now = int(time.time() * 1000)
            bindings = (
                bind("documentUniqueId", "s", path)
                + bind("libraryUniqueId", "s", library_id)
                + bind("uuid", "s", uuid.uuid4().hex)
                + bind("status", "i", 0)
                + bind("createdAt", "l", now)
                + bind("updatedAt", "l", now)
            )
            content_mutation("insert", "MetadataCollection", bindings)
            inserted += 1
        if index % 25 == 0 or index == len(operations):
            print(f"Membership progress: {index}/{len(operations)}", flush=True)
    print(
        f"Memberships: inserted={inserted}, reactivated={reactivated}, "
        f"existing={skipped}, retired={retired}"
    )


def verify() -> bool:
    plan = make_plan()
    categories = plan["categories"]
    state = plan["state"]
    libraries = {row["idString"]: row["name"] for row in state["libraries"] if row["status"] == "0"}
    active_paths = {row["nativeAbsolutePath"] for row in plan["active_books"]}
    counts: dict[str, int] = defaultdict(int)
    memberships: dict[str, set[str]] = defaultdict(set)
    for row in state["members"]:
        if row["status"] != "0" or row["documentUniqueId"] not in active_paths:
            continue
        category = libraries.get(row["libraryUniqueId"])
        if category in categories:
            counts[category] += 1
            memberships[row["documentUniqueId"]].add(category)
    expected = {item["path"]: set(item["categories"]) for item in plan["matches"]}
    missing = {path: sorted(categories - memberships.get(path, set())) for path, categories in expected.items() if categories - memberships.get(path, set())}
    print(f"Verified active BOOX books: {len(active_paths)}")
    for category in categories:
        print(f"  {category}: {counts.get(category, 0)}")
    print(f"Books missing expected memberships: {len(missing)}")
    for path, categories in missing.items():
        print(f"MISSING {path}: {', '.join(categories)}")
    extra_target = sum(len(memberships[path] - expected.get(path, set())) for path in active_paths)
    print(f"Unexpected aligned-category memberships: {extra_target}")
    return not missing and extra_target == 0 and not plan["unmatched"] and not plan["absent_apple"]


def cleanup_collections() -> None:
    plan = make_plan()
    categories = plan["categories"]
    backup_dir = backup(plan)
    print(f"Cleanup backup: {backup_dir}", flush=True)
    target_libraries: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in plan["state"]["libraries"]:
        if row["status"] == "0" and row["name"] in categories:
            target_libraries[row["name"]].append(row)

    now = int(time.time() * 1000)
    canonical: dict[str, dict[str, str]] = {}
    retired_libraries = 0
    for category in categories:
        rows = sorted(target_libraries[category], key=lambda row: int(row["id"]))
        if not rows:
            raise RuntimeError(f"Missing target library: {category}")
        canonical[category] = rows[-1]
        for row in rows[:-1]:
            content_mutation(
                "update",
                "MetadataCollection",
                bind("status", "i", 1) + bind("updatedAt", "l", now),
                f"libraryUniqueId='{row['idString']}' AND status=0",
            )
            content_mutation(
                "update",
                "Library",
                bind("status", "i", 1) + bind("updatedAt", "l", now),
                f"id={row['id']}",
            )
            retired_libraries += 1

    refreshed = provider_state()
    canonical_ids = {row["idString"] for row in canonical.values()}
    pairs: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in refreshed["members"]:
        if row["status"] == "0" and row["libraryUniqueId"] in canonical_ids:
            pairs[(row["documentUniqueId"], row["libraryUniqueId"])].append(row)
    duplicate_ids = []
    for rows in pairs.values():
        rows.sort(key=lambda row: int(row["id"]))
        duplicate_ids.extend(row["id"] for row in rows[1:])
    for start in range(0, len(duplicate_ids), 100):
        ids = duplicate_ids[start : start + 100]
        content_mutation(
            "update",
            "MetadataCollection",
            bind("status", "i", 1) + bind("updatedAt", "l", now),
            f"id IN ({','.join(ids)})",
        )
    print(f"Retired duplicate libraries: {retired_libraries}")
    print(f"Retired duplicate memberships: {len(duplicate_ids)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("plan", "apply", "cleanup", "verify"))
    args = parser.parse_args()
    if args.command == "verify":
        return 0 if verify() else 1
    if args.command == "cleanup":
        cleanup_collections()
        return 0
    plan = make_plan()
    print_plan(plan)
    if args.command == "apply":
        apply_plan(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

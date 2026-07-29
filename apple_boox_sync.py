#!/usr/bin/env python3
"""Safe, reusable Apple Books <-> BOOX synchronization CLI."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import book_sync as catalog
import boox_collections as collections
import reading_progress_sync as progress
import sync_config as config


APP_DIR = config.APP_DIR
STATE_DIR = config.STATE_DIR


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=check)


def doctor(*, verbose: bool = True, require_mtp: bool = True, require_progress: bool = True) -> bool:
    errors: list[str] = []
    warnings: list[str] = []
    required = ["adb", "open"]
    if require_mtp:
        required.extend(("mtp-files", "mtp-connect"))
    for executable in required:
        if not command_exists(executable):
            errors.append(f"缺少命令：{executable}")
    if not command_exists("ebook-convert"):
        warnings.append("未安装 Calibre；遇到 MOBI 时不能自动转为 EPUB")

    device = ""
    if command_exists("adb"):
        result = run(["adb", "devices", "-l"], check=False)
        devices = [line for line in result.stdout.splitlines()[1:] if "\tdevice" in line or " device " in line]
        if len(devices) != 1:
            errors.append(f"需要且只能连接 1 台已授权 BOOX，当前检测到 {len(devices)} 台")
        else:
            device = devices[0].strip()

    if not catalog.BOOKS_DB.is_file():
        errors.append(f"Apple Books 数据库不存在：{catalog.BOOKS_DB}")
    else:
        connection = sqlite3.connect(catalog.BOOKS_DB)
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(ZBKLIBRARYASSET)")}
        finally:
            connection.close()
        required_columns = {"ZTITLE", "ZPATH", "ZREADINGPROGRESS", "ZBOOKHIGHWATERMARKPROGRESS", "ZISFINISHED"}
        missing = sorted(required_columns - columns)
        if missing:
            errors.append(f"Apple Books 数据库结构不兼容，缺少字段：{', '.join(missing)}")
        else:
            try:
                categories = config.collection_names()
                if not categories:
                    warnings.append("没有发现 Apple Books 自建 Collection；书籍和进度仍可同步")
            except Exception as exc:
                errors.append(f"无法读取 Collection 配置：{exc}")

    if require_progress and not config.ANNOTATION_DB.is_file():
        errors.append(f"Apple Books Annotation 数据库不存在：{config.ANNOTATION_DB}")

    if not errors:
        try:
            _, rows = collections.content_query("Library", ("id", "name", "idString", "status"))
            if not rows:
                errors.append("BOOX ContentProvider 可访问，但 Library 表为空")
        except Exception as exc:
            errors.append(f"无法读取 BOOX ContentProvider：{exc}")

    if verbose:
        print("环境检查")
        print(f"  Apple Books DB: {catalog.BOOKS_DB}")
        if require_progress:
            print(f"  Annotation DB: {config.ANNOTATION_DB}")
        print(f"  配置文件: {config.CONFIG_PATH if config.CONFIG_PATH.is_file() else '使用默认配置'}")
        print(f"  状态与备份目录: {STATE_DIR}")
        print(f"  BOOX: {device or '未就绪'}")
        for warning in warnings:
            print(f"  警告: {warning}")
        for error in errors:
            print(f"  错误: {error}")
        print(f"  结果: {'通过' if not errors else '失败'}")
    return not errors


def uncategorized_assets() -> list[tuple[int, str, str]]:
    categories = config.collection_names()
    if not categories:
        return []
    placeholders = ",".join("?" for _ in categories)
    connection = sqlite3.connect(catalog.BOOKS_DB)
    try:
        return connection.execute(
            f"""
            SELECT a.Z_PK, COALESCE(a.ZTITLE,''), COALESCE(a.ZAUTHOR,'')
            FROM ZBKLIBRARYASSET a
            WHERE COALESCE(a.ZISHIDDEN,0)=0 AND COALESCE(a.ZISSAMPLE,0)=0
              AND NOT EXISTS (
                  SELECT 1
                  FROM ZBKCOLLECTIONMEMBER m
                  JOIN ZBKCOLLECTION c ON c.Z_PK=m.ZCOLLECTION
                  WHERE m.ZASSET=a.Z_PK AND c.ZTITLE IN ({placeholders})
              )
            ORDER BY a.ZTITLE
            """,
            categories,
        ).fetchall()
    finally:
        connection.close()


def catalog_status() -> tuple[list, list]:
    apple, boox_all, boox, apple_to_boox, boox_to_apple = catalog.differences()
    print(f"Apple Books: {len(apple)} 本")
    print(f"BOOX: {len(boox_all)} 个文件 / {len(boox)} 个逻辑条目")
    print(f"Apple → BOOX 待补: {len(apple_to_boox)}")
    print(f"BOOX → Apple 待补: {len(boox_to_apple)}")
    for item, _, _ in apple_to_boox:
        print(f"  A→B {item['title']}")
    for item, _, _ in boox_to_apple:
        print(f"  B→A {item['name']}")
    return apple_to_boox, boox_to_apple


def convert_staged_mobi(missing: list) -> None:
    mobi_items = [item for item, _, _ in missing if Path(item["name"]).suffix.lower() == ".mobi"]
    if not mobi_items:
        return
    if not command_exists("ebook-convert"):
        raise RuntimeError("发现 MOBI，但未安装 Calibre 的 ebook-convert")
    for item in mobi_items:
        source = catalog.PULL_DIR / item["name"]
        destination = catalog.IMPORT_DIR / f"{Path(item['name']).stem}.epub"
        subprocess.run(["ebook-convert", str(source), str(destination), "--output-profile", "tablet"], check=True)
        with destination.open("rb") as stream:
            if stream.read(2) != b"PK":
                raise RuntimeError(f"EPUB 转换校验失败：{destination}")
        print(f"已转换 MOBI → EPUB：{destination.name}")


def import_into_apple_books(files: list[Path], expected_growth: int) -> None:
    if not files:
        return
    before = len(catalog.apple_books())
    for index in range(0, len(files), 20):
        batch = files[index : index + 20]
        subprocess.run(["open", "-gj", "-a", "Books", *map(str, batch)], check=True)
    target = before + expected_growth
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        time.sleep(5)
        current = len(catalog.apple_books())
        print(f"等待 Apple Books 导入：{current}/{target}", flush=True)
        if current >= target:
            return
    raise RuntimeError(
        f"Apple Books 自动导入未在 180 秒内完成。文件已保留在：{catalog.IMPORT_DIR}"
    )


def sync_catalog(*, auto_import: bool) -> bool:
    _, _, _, apple_to_boox, boox_to_apple = catalog.differences()
    if not apple_to_boox and not boox_to_apple:
        print("书籍目录已经一致，无需复制。")
        return True

    if apple_to_boox:
        catalog.package_apple_missing()
        catalog.push_apple_missing()

    if boox_to_apple:
        catalog.pull_missing()
        catalog.prepare_import()
        convert_staged_mobi(boox_to_apple)
        staged = sorted(path for path in catalog.IMPORT_DIR.iterdir() if path.is_file())
        if not auto_import:
            subprocess.run(["open", str(catalog.IMPORT_DIR)], check=False)
            print(f"请将该目录中的文件导入 Apple Books，然后重新运行 sync：{catalog.IMPORT_DIR}")
            return False
        import_into_apple_books(staged, len(boox_to_apple))

    print("重新核对双向书目……", flush=True)
    _, _, _, remaining_a2b, remaining_b2a = catalog.differences()
    if remaining_a2b or remaining_b2a:
        print(f"书目仍未闭环：Apple→BOOX {len(remaining_a2b)}，BOOX→Apple {len(remaining_b2a)}")
        return False
    print("书籍目录双向一致。")
    return True


def sync_collections() -> bool:
    if not config.collection_names():
        print("没有发现需要同步的自建 Collection，已跳过 Collection 同步。")
        return True
    uncategorized = uncategorized_assets()
    if uncategorized and config.REQUIRE_COLLECTION_FOR_EVERY_BOOK:
        print(f"发现 {len(uncategorized)} 本尚未进入主 Collection，已停止分类写入：")
        for _, title, author in uncategorized:
            print(f"  {title} — {author}")
        print("请先在 Apple Books 中为它们选择主 Collection，再重新运行 collections 或 sync。")
        return False
    if uncategorized:
        print(f"提示：{len(uncategorized)} 本书不属于任何已选择的 Collection；只同步其余关系。")
    plan = collections.make_plan()
    collections.print_plan(plan)
    collections.apply_plan(plan)
    collections.cleanup_collections()
    return collections.verify()


def check_all() -> bool:
    if not doctor(require_mtp=True, require_progress=True):
        return False
    print("\n书目")
    apple_to_boox, boox_to_apple = catalog_status()
    uncategorized = uncategorized_assets()
    print(f"未分类 Apple Books: {len(uncategorized)}")
    print("\nCollection")
    if config.collection_names():
        collections_ok = collections.verify()
    else:
        print("没有自建 Collection，已跳过。")
        collections_ok = True
    print("\n阅读进度")
    components, apples, boox = progress.build_components()
    progress.print_plan(components, apples, boox)
    progress_stats = progress.component_stats(components, apples, boox)
    progress_ok = all(progress_stats.get(key, 0) == 0 for key in ("apple_only", "boox_only", "conflicts"))
    uncategorized_ok = not uncategorized or not config.REQUIRE_COLLECTION_FOR_EVERY_BOOK
    return not apple_to_boox and not boox_to_apple and uncategorized_ok and collections_ok and progress_ok


def confirm(command: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    print(f"即将执行 {command}，会先备份再写入两端。")
    try:
        answer = input("输入 SYNC 继续：").strip()
    except EOFError:
        return False
    return answer == "SYNC"


def list_backups() -> None:
    directory = STATE_DIR / "backups"
    if not directory.is_dir():
        print("暂无备份。")
        return
    for path in sorted(directory.iterdir(), reverse=True):
        if path.is_dir():
            print(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Apple Books 与 BOOX 安全双向同步")
    parser.add_argument("--version", action="version", version=f"%(prog)s {config.VERSION}")
    parser.add_argument("command", choices=("doctor", "check", "catalog", "collections", "progress", "sync", "backups"))
    parser.add_argument("--yes", action="store_true", help="跳过写入确认")
    parser.add_argument("--manual-apple-import", action="store_true", help="只准备 BOOX→Apple 导入目录，不自动调用 Books")
    args = parser.parse_args()

    if args.command == "doctor":
        return 0 if doctor(require_mtp=True, require_progress=True) else 1
    if args.command == "check":
        return 0 if check_all() else 2
    if args.command == "backups":
        list_backups()
        return 0
    require_mtp = args.command in ("catalog", "sync")
    require_progress = args.command in ("progress", "sync")
    if not doctor(require_mtp=require_mtp, require_progress=require_progress):
        return 1
    if not confirm(args.command, args.yes):
        print("已取消。")
        return 2

    if args.command == "catalog":
        return 0 if sync_catalog(auto_import=not args.manual_apple_import) else 3
    if args.command == "collections":
        return 0 if sync_collections() else 4
    if args.command == "progress":
        return progress.sync()

    if not sync_catalog(auto_import=not args.manual_apple_import):
        return 3
    if not sync_collections():
        return 4
    if progress.sync() != 0:
        return 5
    print("\n最终核对")
    return 0 if check_all() else 6


if __name__ == "__main__":
    raise SystemExit(main())

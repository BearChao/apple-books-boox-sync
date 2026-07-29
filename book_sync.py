#!/usr/bin/env python3
"""Apple Books <-> BOOX catalog scanner and file transfer helpers."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import unicodedata
import zipfile
from difflib import SequenceMatcher
from pathlib import Path
from xml.etree import ElementTree as ET

import sync_config as config

APP_DIR = config.APP_DIR
STATE_DIR = config.STATE_DIR
PULL_DIR = STATE_DIR / "boox-to-apple"
IMPORT_DIR = STATE_DIR / "boox-apple-import"
PUSH_DIR = STATE_DIR / "apple-to-boox"
BOOKS_DB = config.BOOKS_DB
EBOOK_RE = r"epub|pdf|mobi|azw3?|djvu|fb2|cbz|cbr|rtf|docx?"
EBOOK_EXT_RE = re.compile(rf"\.({EBOOK_RE})$", re.I)
BOOX_PROVIDER = "content://com.onyx.content.database.ContentProvider"


def boox_metadata_titles() -> dict[str, str]:
    result = subprocess.run(
        [
            "adb",
            "shell",
            "content",
            "query",
            "--uri",
            f"{BOOX_PROVIDER}/Metadata",
            "--projection",
            "title:nativeAbsolutePath:status",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    titles: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line.startswith("Row: ") or ", nativeAbsolutePath=" not in line or ", status=" not in line:
            continue
        body = re.sub(r"^Row: \d+ title=", "", line)
        title, remainder = body.split(", nativeAbsolutePath=", 1)
        path, status = remainder.rsplit(", status=", 1)
        if status == "0" and title not in ("", "NULL"):
            titles[Path(path).name] = title
    return titles


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(rf"\.({EBOOK_RE})$", "", value, flags=re.I)
    return "".join(char for char in value if char.isalnum())


def core_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(rf"\.({EBOOK_RE})$", "", value, flags=re.I)
    value = re.sub(r"[（(【\[].*?[）)】\]]", "", value)
    value = re.split(r"--|—{2,}", value)[0]
    value = re.sub(
        r"(z-library|anna.?s archive|translated|文字版合集含目录|无水印付费贴).*$",
        "",
        value,
        flags=re.I,
    )
    return "".join(char for char in value if char.isalnum())


def serial_issue(value: str):
    value = unicodedata.normalize("NFKC", value).lower().replace("｜", " ")
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


def title_score(left: str, right: str) -> float:
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


def epub_title(path: Path) -> str | None:
    if path.suffix.lower() != ".epub" or not path.is_file():
        return None
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            opf_name = None
            if "META-INF/container.xml" in names:
                root = ET.fromstring(archive.read("META-INF/container.xml"))
                rootfile = root.find(".//{*}rootfile")
                if rootfile is not None:
                    opf_name = rootfile.attrib.get("full-path")
            if not opf_name:
                opf_name = next((name for name in names if name.lower().endswith(".opf")), None)
            if not opf_name:
                return None
            root = ET.fromstring(archive.read(opf_name))
            title = root.find(".//{*}metadata/{*}title")
            if title is None:
                title = root.find(".//{*}title")
            return "".join(title.itertext()).strip() if title is not None else None
    except (OSError, KeyError, ET.ParseError, zipfile.BadZipFile):
        return None


def apple_books():
    connection = sqlite3.connect(BOOKS_DB)
    try:
        rows = connection.execute(
            "SELECT ZTITLE, ZAUTHOR, ZPATH FROM ZBKLIBRARYASSET "
            "WHERE COALESCE(ZISHIDDEN,0)=0 AND COALESCE(ZISSAMPLE,0)=0"
        )
        return [
            {"title": title, "author": author or "", "path": Path(path)}
            for title, author, path in rows
        ]
    finally:
        connection.close()


def boox_files():
    metadata_titles = boox_metadata_titles()
    result = subprocess.run(
        ["mtp-files"], capture_output=True, text=True, check=True
    )
    pattern = re.compile(
        r"File ID:\s*(\d+).*?Filename:\s*(.*?)\n.*?File size\s+(\d+)"
        r".*?Parent ID:\s*(\d+).*?Filetype:\s*(.*?)\n",
        re.S,
    )
    books = []
    for match in pattern.finditer(result.stdout):
        file_id, name, size, parent_id, file_type = match.groups()
        if not EBOOK_EXT_RE.search(name):
            continue
        item = {
            "id": int(file_id),
            "name": name,
            "size": int(size),
            "parent": int(parent_id),
            "type": file_type.strip(),
            "title": metadata_titles.get(name, name),
        }
        local = PULL_DIR / name
        if local.is_file() and local.stat().st_size == item["size"]:
            item["title"] = epub_title(local) or metadata_titles.get(name, name)
        books.append(item)
    return books


def boox_representatives(books):
    by_binary = {}
    for book in books:
        key = (book["size"], Path(book["name"]).suffix.lower())
        descriptive = not bool(
            re.fullmatch(r"[0-9a-f]{20,}\.[a-z0-9]+", book["name"], re.I)
        )
        preference = (book["parent"] in (13, 17), descriptive, -len(book["name"]))
        if key not in by_binary or preference > by_binary[key][0]:
            by_binary[key] = (preference, book)

    clusters = []
    candidates = [entry[1] for entry in by_binary.values()]
    candidates.sort(key=lambda item: (item["parent"] not in (17, 13), -item["size"]))
    for book in candidates:
        cluster = next(
            (
                cluster
                for cluster in clusters
                if title_score(book["title"], cluster[0]["title"]) >= 0.96
            ),
            None,
        )
        if cluster is None:
            clusters.append([book])
        else:
            cluster.append(book)
    return [cluster[0] for cluster in clusters]


def differences():
    apple = apple_books()
    boox_all = boox_files()
    boox = boox_representatives(boox_all)

    apple_to_boox = []
    for item in apple:
        if boox:
            score, match = max(
                ((title_score(item["title"], book["title"]), book) for book in boox),
                key=lambda pair: pair[0],
            )
        else:
            score, match = 0.0, None
        if score < config.MATCH_THRESHOLD:
            apple_to_boox.append((item, score, match))

    boox_to_apple = []
    for book in boox:
        if apple:
            score, match = max(
                ((title_score(book["title"], item["title"]), item) for item in apple),
                key=lambda pair: pair[0],
            )
        else:
            score, match = 0.0, None
        if score < config.MATCH_THRESHOLD:
            boox_to_apple.append((book, score, match))
    return apple, boox_all, boox, apple_to_boox, boox_to_apple


def print_scan():
    apple, boox_all, boox, apple_to_boox, boox_to_apple = differences()
    print(f"Apple Books files: {len(apple)}")
    print(f"BOOX ebook files: {len(boox_all)}")
    print(f"BOOX logical representatives: {len(boox)}")
    print(f"Apple -> BOOX: {len(apple_to_boox)}")
    for item, _, _ in sorted(apple_to_boox, key=lambda entry: entry[0]["title"]):
        print(f"  A2B {item['title']}")
    print(
        f"BOOX -> Apple: {len(boox_to_apple)} "
        f"({sum(item['size'] for item, _, _ in boox_to_apple) / 1024 / 1024:.1f} MiB)"
    )
    for item, _, _ in sorted(
        boox_to_apple, key=lambda entry: (entry[0]["parent"], entry[0]["name"])
    ):
        print(f"  B2A {item['id']} {item['name']}")


def chunked(items, max_files=10, max_bytes=120 * 1024 * 1024):
    chunk, total = [], 0
    for item in items:
        if chunk and (len(chunk) >= max_files or total + item["size"] > max_bytes):
            yield chunk
            chunk, total = [], 0
        chunk.append(item)
        total += item["size"]
    if chunk:
        yield chunk


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pull_missing():
    PULL_DIR.mkdir(parents=True, exist_ok=True)
    _, _, _, _, missing = differences()
    pending = []
    for item, _, _ in missing:
        destination = PULL_DIR / item["name"]
        if destination.is_file() and destination.stat().st_size == item["size"]:
            continue
        pending.append(item)
    print(f"Need to pull {len(pending)} of {len(missing)} missing BOOX books")
    batches = list(chunked(pending))
    for index, batch in enumerate(batches, 1):
        command = ["mtp-connect"]
        for item in batch:
            command.extend(["--getfile", str(item["id"]), str(PULL_DIR / item["name"])])
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        failures = []
        for item in batch:
            destination = PULL_DIR / item["name"]
            if not destination.is_file() or destination.stat().st_size != item["size"]:
                failures.append(item["name"])
        if failures:
            raise RuntimeError(f"Transfer verification failed: {failures}")
        transferred = sum(item["size"] for item in batch) / 1024 / 1024
        print(f"Pulled batch {index}/{len(batches)}: {len(batch)} files, {transferred:.1f} MiB", flush=True)


def safe_epub_from_package(source: Path, destination: Path):
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with zipfile.ZipFile(temporary, "w") as archive:
        mimetype = source / "mimetype"
        if mimetype.is_file():
            archive.write(mimetype, "mimetype", compress_type=zipfile.ZIP_STORED)
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path == mimetype:
                continue
            if path.name in {".DS_Store", "iTunesMetadata.plist", "iTunesMetadata-original.plist"}:
                continue
            archive.write(path, path.relative_to(source).as_posix(), compress_type=zipfile.ZIP_DEFLATED)
    temporary.replace(destination)


def package_apple_missing():
    PUSH_DIR.mkdir(parents=True, exist_ok=True)
    _, _, _, missing, _ = differences()
    for item, _, _ in missing:
        source = item["path"]
        destination = PUSH_DIR / source.name
        if source.is_dir() and source.suffix.lower() == ".epub":
            safe_epub_from_package(source, destination)
        elif source.is_file():
            shutil.copy2(source, destination)
        else:
            raise FileNotFoundError(source)
        print(f"Packaged {destination.name} ({destination.stat().st_size} bytes)")


def push_apple_missing():
    _, _, _, missing, _ = differences()
    pending = []
    for item, _, _ in missing:
        local = PUSH_DIR / item["path"].name
        if not local.is_file():
            raise FileNotFoundError(f"Run package first: {local}")
        pending.append(local)
    print(f"Need to push {len(pending)} Apple Books files")
    for index, local in enumerate(pending, 1):
        local_hash = file_sha256(local)
        subprocess.run(
            ["adb", "push", str(local), f"{config.BOOX_BOOKS_DIR}/"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        remote_path = f"{config.BOOX_BOOKS_DIR}/{local.name}"
        result = subprocess.run(
            ["adb", "shell", f"sha256sum {shlex.quote(remote_path)}"],
            capture_output=True,
            text=True,
            check=True,
        )
        remote_hash = result.stdout.split()[0] if result.stdout.split() else ""
        if remote_hash != local_hash:
            raise RuntimeError(f"Device SHA-256 verification failed: {local.name}")
        print(f"Pushed {index}/{len(pending)}: {local.name}", flush=True)


def prepare_import():
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    for path in IMPORT_DIR.iterdir():
        if path.is_file() or path.is_symlink():
            path.unlink()
    _, _, _, _, missing = differences()
    missing_names = {item["name"] for item, _, _ in missing}
    supported = []
    for source in PULL_DIR.iterdir():
        if source.name not in missing_names:
            continue
        if source.suffix.lower() not in {".epub", ".pdf"}:
            continue
        destination = IMPORT_DIR / source.name
        os.link(source, destination)
        supported.append(destination)
    print(f"Prepared {len(supported)} EPUB/PDF files in {IMPORT_DIR}")
    for source in PULL_DIR.glob("*.mobi"):
        if source.name in missing_names:
            print(f"MOBI_REQUIRES_CONVERSION {source}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=["scan", "pull", "package", "push", "prepare-import"]
    )
    args = parser.parse_args()
    {
        "scan": print_scan,
        "pull": pull_missing,
        "package": package_apple_missing,
        "push": push_apple_missing,
        "prepare-import": prepare_import,
    }[args.command]()


if __name__ == "__main__":
    main()

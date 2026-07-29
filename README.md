# Apple Books ↔ BOOX Sync

Safely reconcile a macOS Apple Books library with a USB-connected BOOX reader.

The tool can add missing DRM-free book files in either direction, mirror custom Apple Books collections to BOOX, and monotonically merge reading percentage and finished status. It creates backups before database writes and verifies the result after reopening both readers.

> [!WARNING]
> This is an experimental community tool that works with undocumented Apple Books and BOOX database interfaces. Read the safety notes, run `check` first, and keep independent backups of your library.

[简体中文说明](README.zh-CN.md)

## Features

- Additive two-way catalog reconciliation; it does not delete book files.
- Automatic discovery of custom Apple Books collections.
- Idempotent BOOX collection creation, membership alignment, and duplicate cleanup.
- Monotonic progress merge: higher current progress wins, finished wins, and 0% never overwrites progress.
- Automatic SQLite and BOOX ContentProvider snapshots before writes.
- Five-book progress pilot before bulk updates.
- Post-write application restart and read-back verification.
- Optional JSON configuration with no Python dependencies.

## Compatibility

- macOS with Apple Books and Python 3.10 or newer.
- A BOOX device with USB debugging enabled and an accessible ONYX ContentProvider.
- Tested on a BOOX Leaf2_P. Other BOOX firmware versions may use different schemas; `doctor` is designed to fail before writes when required interfaces are unavailable.
- DRM-free EPUB, PDF, MOBI, AZW/AZW3, DJVU, FB2, CBZ/CBR, RTF, DOC, and DOCX files. This project does not remove or bypass DRM.

## Install

```bash
git clone https://github.com/BearChao/apple-books-boox-sync.git
cd apple-books-boox-sync
brew install android-platform-tools libmtp
python3 apple_boox_sync.py doctor
```

Calibre is only needed when a BOOX-only MOBI file must be converted for Apple Books:

```bash
brew install --cask calibre
```

On the BOOX device, enable USB debugging, connect it by USB, and authorize the Mac. Exactly one authorized Android device should be connected while the tool runs.

## Quick start

Double-click `sync.command`, or use the terminal:

```bash
python3 apple_boox_sync.py check
python3 apple_boox_sync.py sync
```

Mutating commands require typing uppercase `SYNC`. For an explicitly unattended run:

```bash
python3 apple_boox_sync.py sync --yes
```

## Commands

| Command | Behavior |
| --- | --- |
| `doctor` | Validate local databases, dependencies, ADB authorization, and the BOOX provider. |
| `check` | Read-only catalog, collection, and progress comparison. |
| `catalog` | Add book files missing from either side. |
| `collections` | Back up and mirror custom Apple Books collections to BOOX. |
| `progress` | Back up, pilot, merge, reopen, and verify reading progress. |
| `sync` | Run catalog, collection, and progress synchronization in order. |
| `backups` | List backups created by this tool. |

If automatic import into Apple Books does not finish, stage and open the files manually:

```bash
python3 apple_boox_sync.py catalog --manual-apple-import
```

## Configuration

No configuration file is required. To customize behavior:

```bash
cp config.example.json config.json
```

```json
{
  "collections": null,
  "exclude_collections": [],
  "boox_books_dir": "/sdcard/Books",
  "match_threshold": 0.8,
  "require_collection_for_every_book": false
}
```

- `collections: null` discovers all custom Apple Books collections. Supply an array of names to select an explicit subset.
- `exclude_collections` removes named collections from automatic discovery.
- `boox_books_dir` controls where Apple-only files are pushed.
- `match_threshold` controls fuzzy title matching from 0 to 1. Review `check` output before lowering it.
- `require_collection_for_every_book` stops collection writes when any visible Apple Books asset is unclassified.

Environment variables override paths without putting machine-specific values in the repository:

```bash
export APPLE_BOOX_CONFIG=/path/to/config.json
export APPLE_BOOX_SYNC_STATE_DIR=/path/to/state
export APPLE_BOOKS_DB=/path/to/BKLibrary.sqlite
export APPLE_BOOKS_ANNOTATION_DB=/path/to/AEAnnotation_local.sqlite
```

## Merge and matching rules

Books are matched using normalized titles, EPUB metadata, BOOX metadata, filenames, and serial-publication safeguards. The default match threshold is `0.8`; catalog or collection mutation stops when matching does not converge.

Progress uses Apple Books `ZREADINGPROGRESS`, not the historical `ZBOOKHIGHWATERMARKPROGRESS`. BOOX progress uses its current/total field and `readingStatus`.

The synchronization deliberately does not translate Apple EPUB CFI anchors into BOOX NeoReader anchors. Percentage and completion state can converge, but opening a book on the other device is not guaranteed to land on the exact same paragraph.

## Safety and privacy

- All processing is local. The scripts do not send library metadata or book files to a network service.
- Book synchronization is additive. No book file is automatically deleted.
- Database and provider snapshots are written under `state/backups/` before mutation.
- Apple Books and NeoReader are stopped before progress writes and reopened before final verification, reducing the risk of cached state overwriting changes.
- BOOX collection rows are soft-retired rather than physically deleted.
- Direct database manipulation is unsupported by Apple and ONYX. Firmware or macOS updates can break compatibility.
- Keep a separate backup. The project is provided without warranty under the MIT License.

See [SECURITY.md](SECURITY.md) before reporting a vulnerability or sharing diagnostic logs.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
zsh -n sync.command
```

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)

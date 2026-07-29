# Contributing

Thanks for helping improve Apple Books ↔ BOOX Sync.

## Before opening a change

- Do not commit book files, database snapshots, device serial numbers, library metadata, or diagnostic logs containing personal titles and paths.
- Keep database changes additive or soft-retiring where possible.
- Preserve the backup-before-write, pilot-before-bulk, and reopen-then-read-back safety sequence.
- Treat Apple Books and BOOX schemas as version-specific undocumented interfaces.

## Development checks

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
zsh -n sync.command
```

Pull requests should explain the affected macOS version, BOOX model and firmware, schema differences observed, and how read-only and write behavior were verified. Use synthetic or redacted examples only.

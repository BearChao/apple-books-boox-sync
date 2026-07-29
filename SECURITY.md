# Security Policy

## Reporting a vulnerability

Please use GitHub private vulnerability reporting when available. Do not post Apple Books databases, BOOX provider dumps, book files, device serial numbers, or unredacted library paths in a public issue.

## Data-safety model

This tool directly reads and updates undocumented local databases. It reduces risk by checking dependencies and schemas, requiring explicit confirmation, taking snapshots, using a five-book progress pilot, stopping reader processes before progress writes, and verifying after reopening.

These controls cannot guarantee recovery across every macOS or BOOX firmware version. Keep an independent backup of both libraries before first use. Do not bypass a failed `doctor` check.

The project does not implement DRM removal, privilege escalation, or any remote service. All book and library processing is intended to remain local.

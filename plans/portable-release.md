# Portable release implementation plan

## Background and context

The Windows application already has a pinned PyInstaller recipe, embedded input
hashes, product diagnostics and offline help. Deployment currently requires local
knowledge, and there is no published GitHub Release. Existing uncommitted 0.9.4
branding changes belong in this delivery and must be preserved.

## Acceptance criteria

- Windows 10/11 x64 users can download an EXE or portable ZIP and run without Python.
- A new developer can prepare Python 3.12, launch, check and release using documented
  entry points with no dependency on the original machine or HexiPlan directories.
- Version 0.9.5 has matching application/resource identities, a clean source commit,
  source ZIP, binary checksums, build provenance and source/frozen diagnostic results.
- A checked-in Codex handoff explains architecture, commands, scientific safeguards,
  existing evidence and gaps. Master, the version tag and GitHub Release are published.

## Implementation details

1. Add Windows setup/start/release wrappers and a shared environment manager. Require
   x64 CPython 3.12.1+ in the 3.12 series; use exact existing dependency pins.
2. Add a reusable diagnostic runner and release orchestrator. Require a clean Git
   checkout, run product smoke scenarios in source and frozen modes, use the existing
   verified final build, and package binaries plus the exact Git source and SHA-256.
3. Add tag-driven GitHub release automation using the same commands.
4. Write deployment instructions, root AGENTS.md, CODEX_HANDOFF.md and release notes;
   bump application/license/resource identities and version.md before final build.
5. Verify setup and invalid-runtime handling, all three product smoke scenarios,
   source and resource hashes, frozen identity, archive contents and checksums.
   Commit on master, build from that clean commit, push master/tag and upload assets.

## Scope and risks

No scientific algorithm, archive contract, default analysis setting or dependency
version changes. The portable EXE is unsigned and targets Windows x64 only. Synthetic
checks do not establish real-device accuracy or clean-host/DPI coverage. Existing
license texts/source notices remain included. Historical external evidence is not
required to build and must not be presented as reproducible repository tests.

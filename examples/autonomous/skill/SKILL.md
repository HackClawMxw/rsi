---
name: loader-observer-cpu-demo
description: Build and validate a transparent loader observer on a local CPU fixture.
---

Read `notes.md` relative to this exact skill snapshot before implementing the observer.

The task workspace contains `protocol.py`. Create `observer.py` exposing
`wrap(loader)`, which returns a transparent callable: preserve every positional
argument, keyword argument, return value and exception. The observer may record
metadata but must never change what the loader receives or returns.

Run `python3 protocol.py` from the workspace. A completed task requires every
fixed protocol case to pass. If an actual failure occurs, inspect its evidence
and correct only `observer.py`; do not weaken checks or edit `protocol.py`.
Include `observer.py` and the actual `report.json` as execution evidence.
Keep this skill snapshot unchanged during execution.

Everything is local Python standard library work. Do not use NPU devices,
network clients, package installation, remote hosts or services.

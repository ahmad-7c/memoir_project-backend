"""
@file backend/_import_check.py
@description Imports every module under src/ and reports failures.

There is no test runner in this repository yet, so this is how a broken import
gets caught before a request does: every module is imported in one process,
which also surfaces circular-import problems that a lazy per-route import would
hide until the route is first called.

    python _import_check.py

Exits 0 when everything imports, 1 otherwise. Requires no database connection.
"""

import importlib
import pathlib
import sys

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent
SRC = BACKEND_ROOT / "src"

# Modules that were already broken before this check existed. Listed explicitly
# so the check stays green (a permanently red check is one people stop reading)
# without hiding them — the name is printed on every run.
#
# `src/integrations/user.py` is a stale duplicate of `src/db/models/user_account.py`
# that imports `integrations.database`, a module that does not exist (the real
# path is `src.integrations.supabase_client`). Nothing references it. It should
# be deleted; until then it stays listed here rather than silently ignored.
KNOWN_BROKEN = {
    "src.integrations.user": "stale duplicate of db/models/user_account.py; imports a module that does not exist",
}


def module_names() -> list[str]:
    names = []
    for path in sorted(SRC.rglob("*.py")):
        parts = list(path.relative_to(BACKEND_ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(parts))
    return names


def main() -> int:
    names = module_names()
    failures = []
    for name in names:
        if name in KNOWN_BROKEN:
            print(f"  SKIP {name} (pre-existing: {KNOWN_BROKEN[name]})")
            continue
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - the point is to report anything
            failures.append((name, type(exc).__name__, str(exc)[:200]))

    checked = len(names) - len(KNOWN_BROKEN)
    print(f"imported {checked - len(failures)}/{checked} modules")
    for name, kind, message in failures:
        print(f"  FAIL {name}: {kind}: {message}")

    if failures:
        print(f"\nFAIL: {len(failures)} module(s) did not import.")
        return 1

    print("PASS: every importable module imports cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

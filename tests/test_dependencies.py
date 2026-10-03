"""
@file tests/test_dependencies.py
@description Asserts that requirements.txt is complete.

WHY THIS IS A TEST AND NOT A LINT

Two real defects were found by this check, and both had the same shape: the code
imported a package directly, the package was present locally only as somebody
else's transitive dependency, and nothing noticed until a clean environment was
built.

    email-validator   `EmailStr` in schemas/auth.py needs it at import time.
                      `pip show` reports Required-by as empty, so nothing pulled
                      it in. The developer's virtualenv happened to have it.

    httpx             imported directly by integrations/stt/router.py. Present
                      only because `supabase` and `openai` happen to depend on
                      it.

Both are invisible locally and fatal in a container, because a container
installs exactly what requirements.txt says and nothing else. The failure is a
container that will not start, discovered during a deploy.

The check walks the actual imports in src/ rather than trusting a hand-written
list, so a new import is covered the day it is written.
"""

import ast
import pathlib
import sys
from importlib.metadata import distributions

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent
REQUIREMENTS = BACKEND_ROOT / "requirements.txt"

# Modules that are part of this repository, not third-party.
LOCAL_MODULES = {"src", "tests", "alembic", "scripts", "_ddl_parity_check", "_import_check"}


def _normalise(name: str) -> str:
    """PEP 503 normalisation, so `Pydantic_Settings` == `pydantic-settings`."""
    return name.strip().lower().replace("_", "-").replace(".", "-")


def _declared_distributions() -> set:
    """
    Distribution names listed in requirements.txt.

    Extras are stripped (`psycopg[binary]` -> `psycopg`) because the extra
    changes what is installed, not which distribution provides the module.
    """
    declared = set()

    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue

        # Version specifiers and extras may appear in either order
        # (`psycopg[binary]==3.2.10`, `foo>=1,<2`).
        name = line
        for stop in ("[", "=", "<", ">", "!", "~", ";", " "):
            name = name.split(stop, 1)[0]
        if name:
            declared.add(_normalise(name))

    return declared


def _module_to_distribution() -> dict:
    """Maps an importable top-level module name to the distribution providing it."""
    mapping = {}
    for dist in distributions():
        name = _normalise(dist.metadata["Name"] or "")
        try:
            top_level = dist.read_text("top_level.txt") or ""
        except Exception:
            top_level = ""
        for module in top_level.split():
            mapping.setdefault(module.lower(), name)
    return mapping


def _third_party_imports_in_src() -> dict:
    """
    Every non-stdlib, non-local module imported anywhere under src/.

    Parsed from the AST rather than grepped, so a mention in a comment or a
    docstring does not count as a dependency.
    """
    imports: dict = {}

    for path in sorted((BACKEND_ROOT / "src").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module.split(".")[0]]

            for module in modules:
                if module in LOCAL_MODULES or module in sys.stdlib_module_names:
                    continue
                imports.setdefault(module, set()).add(
                    str(path.relative_to(BACKEND_ROOT))
                )

    return imports


def test_requirements_file_is_parseable():
    """A malformed pin should fail here, not halfway through a container build."""
    declared = _declared_distributions()

    assert len(declared) > 10, "requirements.txt parsed to almost nothing"
    assert "fastapi" in declared
    assert "supabase" in declared


def test_every_direct_import_is_declared_in_requirements():
    """
    The check that found both real defects.

    Fails with the file list, because "module X is missing" is not actionable on
    its own -- the useful information is where it is imported from, which is
    where the requirement has to be added or the import changed.
    """
    declared = _declared_distributions()
    module_to_dist = _module_to_distribution()

    # `src/integrations/user.py` is a stale duplicate of
    # db/models/user_account.py that imports a module which no longer exists.
    # It is unreferenced, and _import_check.py already skips it by name.
    known_broken = {"integrations", "user"}

    missing = []
    for module, locations in sorted(_third_party_imports_in_src().items()):
        if module in known_broken:
            continue

        distribution = module_to_dist.get(module.lower())
        if distribution and _normalise(distribution) in declared:
            continue
        # Unknown distribution: fall back to matching the module name itself,
        # which covers single-module distributions.
        if _normalise(module) in declared:
            continue

        missing.append(
            f"{module} (distribution: {distribution or 'unknown'}) "
            f"imported by {sorted(locations)[0]}"
        )

    assert not missing, (
        "src/ imports packages that requirements.txt does not declare:\n  "
        + "\n  ".join(missing)
        + "\n\nEach was working locally only as somebody else's transitive "
        "dependency. A container installs exactly what this file lists, so this "
        "is an import error at startup in every deployed environment."
    )


def test_packages_imported_at_runtime_are_pinned_exactly():
    """
    Every requirement carries an exact version.

    `psycopg[binary]==3.2.10` is not pinned and resolves to whatever is newest
    on the day of the build, which makes an image unreproducible and makes a
    future failure impossible to attribute.
    """
    unpinned = []

    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if "==" not in line:
            unpinned.append(line)

    assert not unpinned, f"unpinned requirements: {unpinned}"


def test_email_validator_is_declared():
    """
    `EmailStr` is used in src/schemas/auth.py and Pydantic raises
    `ImportError("email-validator is not installed")` at import time without it.

    Kept as its own test, separate from the general audit, because the general
    audit reports it as "email-validator is undeclared" while the thing that
    actually breaks is that a *signup request* path cannot be imported -- and
    that is the kind of detail worth naming in a failure message.
    """
    assert "email-validator" in _declared_distributions()

    source = (BACKEND_ROOT / "src" / "schemas" / "auth.py").read_text(encoding="utf-8")
    assert "EmailStr" in source, (
        "EmailStr is no longer used, so email-validator may be removable -- "
        "remove it from requirements.txt in the same change"
    )
"""Architectural guard: no production bypass of the TranslationSequencer.

Repair item 3 renamed the legacy public commit to
`commit_translation_result_direct` and documented it as test-only. A
docstring alone is not enforcement — a future worker, API handler or script
could call it again and silently restore strict head-of-line commits that
skip durable staging and ordered release (D-034).

These tests parse the production packages with `ast` (not a text grep), so
docstrings and comments that merely mention the name do not mask a real
call site, and a real call site cannot hide behind formatting.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_PACKAGE = REPO_ROOT / "backend" / "classroom_interpreter"
SCRIPTS_DIR = REPO_ROOT / "scripts"

DIRECT_COMMIT = "commit_translation_result_direct"
# The one file allowed to contain the name, and only as the method definition.
DEFINING_MODULE = BACKEND_PACKAGE / "storage" / "repository.py"


def production_modules() -> list[Path]:
    modules = sorted(BACKEND_PACKAGE.rglob("*.py"))
    if SCRIPTS_DIR.is_dir():
        modules += sorted(SCRIPTS_DIR.rglob("*.py"))
    assert modules, "no production modules found; test would vacuously pass"
    return modules


def direct_commit_references(path: Path) -> list[int]:
    """Line numbers where the module *uses* the direct-commit name.

    Attribute access (`repository.commit_translation_result_direct`), bare
    names, and `getattr(x, "commit_translation_result_direct")` all count.
    The `def` in the defining module does not.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == DIRECT_COMMIT:
            hits.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id == DIRECT_COMMIT:
            hits.append(node.lineno)
        elif isinstance(node, ast.Constant) and node.value == DIRECT_COMMIT:
            # getattr(...)/string dispatch is still a bypass.
            hits.append(node.lineno)
    return hits


def test_no_production_module_calls_the_direct_translation_commit() -> None:
    """Mutation that turns this red: have any backend/ or scripts/ module
    call `repository.commit_translation_result_direct(...)`, including via
    `getattr`. Only the method definition itself is permitted to exist.
    """
    offenders: dict[str, list[int]] = {}
    for module in production_modules():
        hits = direct_commit_references(module)
        if hits:
            offenders[str(module.relative_to(REPO_ROOT))] = hits
    assert offenders == {}, (
        "production code must stage results and let TranslationSequencer "
        f"release them in order; direct-commit bypass found at: {offenders}"
    )


def test_direct_translation_commit_still_exists_only_as_a_definition() -> None:
    """Keeps the guard above honest.

    If the method were deleted or renamed, `test_no_production_module_calls...`
    would pass vacuously. This asserts the name still exists as a method
    definition in exactly the one module that is allowed to define it.
    """
    tree = ast.parse(DEFINING_MODULE.read_text(encoding="utf-8"))
    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == DIRECT_COMMIT
    ]
    assert len(definitions) == 1, (
        f"expected exactly one definition of {DIRECT_COMMIT} in "
        f"{DEFINING_MODULE.name}, found {len(definitions)}"
    )
    # And no other production module defines a same-named shim.
    for module in production_modules():
        if module == DEFINING_MODULE:
            continue
        other = [
            node
            for node in ast.walk(ast.parse(module.read_text(encoding="utf-8")))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == DIRECT_COMMIT
        ]
        assert not other, f"unexpected {DIRECT_COMMIT} definition in {module}"


def test_translation_sequencer_is_the_only_release_caller() -> None:
    """`release_next_translation_result` must be called from exactly one
    production class: `TranslationSequencer`.

    Mutation that turns this red: call the release method from
    `TranslationAgent._process_claim` (restoring per-worker publication) or
    from an API handler.
    """
    callers: dict[str, list[str]] = {}
    for module in production_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        for class_node in ast.walk(tree):
            if not isinstance(class_node, ast.ClassDef):
                continue
            for node in ast.walk(class_node):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "release_next_translation_result"
                ):
                    callers.setdefault(
                        str(module.relative_to(REPO_ROOT)), []
                    ).append(class_node.name)

    assert callers == {
        "backend/classroom_interpreter/agents/translation.py": ["TranslationSequencer"]
    }, f"unexpected ordered-release callers: {callers}"

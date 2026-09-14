from __future__ import annotations

import argparse
import json
import os
import re
import shlex
from collections import defaultdict, deque
from pathlib import Path, PurePosixPath

CODE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts")
RESOLVE_EXTENSIONS = CODE_EXTENSIONS + (".json",)
IGNORED_IMPORT_EXTENSIONS = {
    ".css", ".scss", ".sass", ".less", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
}
IMPORT_PATTERNS = (
    re.compile(r"(?:import|export)\s+(?:[^'\";]*?\s+from\s+)?['\"]([^'\"]+)['\"]"),
    re.compile(r"import\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
    re.compile(r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
)


def _normalize_repo_path(value: str) -> str:
    path = PurePosixPath(value.strip())
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid changed path: {value!r}")
    return path.as_posix()


def _candidate_paths(importer: Path, spec: str) -> list[Path]:
    raw = importer.parent / spec
    candidates: list[Path] = []
    suffix = raw.suffix.lower()
    if suffix:
        candidates.append(raw)
        # NodeNext tests commonly import the emitted .js name while the source is .ts.
        if suffix in {".js", ".mjs", ".cjs"}:
            stem = raw.with_suffix("")
            candidates.extend(stem.with_suffix(ext) for ext in CODE_EXTENSIONS)
    else:
        candidates.extend(raw.with_suffix(ext) for ext in RESOLVE_EXTENSIONS)
        candidates.extend(raw / f"index{ext}" for ext in RESOLVE_EXTENSIONS)
    # Preserve order while removing duplicates.
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve(strict=False)
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _imports_for(path: Path, project: Path) -> tuple[set[Path], bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set(), False
    deps: set[Path] = set()
    complete = True
    project_resolved = project.resolve()
    specs: list[str] = []
    for pattern in IMPORT_PATTERNS:
        specs.extend(pattern.findall(text))
    for spec in specs:
        if not spec.startswith("."):
            continue
        suffix = PurePosixPath(spec).suffix.lower()
        if suffix in IGNORED_IMPORT_EXTENSIONS:
            continue
        resolved = None
        for candidate in _candidate_paths(path, spec):
            try:
                candidate.relative_to(project_resolved)
            except ValueError:
                continue
            if candidate.is_file():
                resolved = candidate
                break
        if resolved is None:
            # A relative code import we cannot resolve makes selective execution unsafe.
            complete = False
            continue
        deps.add(resolved)
    return deps, complete


def select_tests(project: Path, prefix: str, changed_paths: list[str]) -> dict[str, object]:
    project = project.resolve()
    prefix = prefix.strip("/")
    relevant: list[str] = []
    for raw in changed_paths:
        normalized = _normalize_repo_path(raw)
        if normalized == prefix:
            return {"mode": "full", "tests": [], "reason": "project-root-changed"}
        marker = prefix + "/"
        if normalized.startswith(marker):
            relevant.append(normalized[len(marker) :])

    if not relevant:
        return {"mode": "none", "tests": [], "reason": "no-project-changes"}

    test_changes: list[Path] = []
    source_changes: list[Path] = []
    for rel in relevant:
        rel_path = PurePosixPath(rel)
        if rel_path.parts and rel_path.parts[0] == "tests" and rel_path.suffix in CODE_EXTENSIONS:
            path = (project / rel).resolve(strict=False)
            if not path.is_file():
                return {"mode": "full", "tests": [], "reason": f"deleted-test:{rel}"}
            test_changes.append(path)
            continue
        if rel_path.parts and rel_path.parts[0] == "src" and rel_path.suffix in CODE_EXTENSIONS:
            path = (project / rel).resolve(strict=False)
            if not path.is_file():
                return {"mode": "full", "tests": [], "reason": f"deleted-source:{rel}"}
            source_changes.append(path)
            continue
        # package.json, lockfile, tsconfig, scripts and unknown project files can have broad effects.
        return {"mode": "full", "tests": [], "reason": f"global-path:{rel}"}

    tests_dir = project / "tests"
    all_tests = sorted(
        path.resolve()
        for path in tests_dir.glob("*.test.*")
        if path.is_file() and path.suffix in CODE_EXTENSIONS
    )
    if not all_tests:
        return {"mode": "full", "tests": [], "reason": "no-tests-discovered"}

    if source_changes:
        graph_files = [
            path.resolve()
            for base in (project / "src", tests_dir)
            if base.is_dir()
            for path in base.rglob("*")
            if path.is_file() and path.suffix in CODE_EXTENSIONS
        ]
        reverse: dict[Path, set[Path]] = defaultdict(set)
        for importer in graph_files:
            deps, complete = _imports_for(importer, project)
            if not complete:
                return {"mode": "full", "tests": [], "reason": f"unresolved-import:{importer.relative_to(project).as_posix()}"}
            for dep in deps:
                reverse[dep].add(importer)

        selected: set[Path] = set(test_changes)
        test_set = set(all_tests)
        for source in source_changes:
            queue: deque[Path] = deque([source])
            visited = {source}
            source_tests: set[Path] = set()
            while queue:
                current = queue.popleft()
                for importer in reverse.get(current, ()):  # reverse dependency closure
                    if importer in visited:
                        continue
                    visited.add(importer)
                    queue.append(importer)
                    if importer in test_set:
                        source_tests.add(importer)
            if not source_tests:
                return {
                    "mode": "full",
                    "tests": [],
                    "reason": f"source-without-test:{source.relative_to(project).as_posix()}",
                }
            selected.update(source_tests)
    else:
        selected = set(test_changes)

    if not selected:
        return {"mode": "full", "tests": [], "reason": "empty-selection"}
    if len(selected) >= len(all_tests):
        return {"mode": "full", "tests": [], "reason": "selection-covers-all"}

    rel_tests = sorted(path.relative_to(project).as_posix() for path in selected)
    return {
        "mode": "selected",
        "tests": rel_tests,
        "reason": "dependency-graph" if source_changes else "changed-tests-only",
        "selected_count": len(rel_tests),
        "total_count": len(all_tests),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Select safe Node test files from the staged diff")
    parser.add_argument("--project", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--format", choices=("json", "shell"), default="json")
    args = parser.parse_args()

    changed = [line for line in (os.environ.get("CHANGED_FILES_RAW_INPUT") or "").splitlines() if line.strip()]
    plan = select_tests(Path(args.project), args.prefix, changed)
    if args.format == "json":
        print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
        return 0

    mode = str(plan["mode"])
    tests = [str(item) for item in plan.get("tests", [])]
    reason = str(plan.get("reason") or "")
    total = len([path for path in (Path(args.project) / "tests").glob("*.test.*") if path.is_file() and path.suffix in CODE_EXTENSIONS])
    if mode == "selected":
        command = "node --import tsx --test " + " ".join(shlex.quote(item) for item in tests)
    elif mode == "none":
        command = ":"
    else:
        command = "npm test"
    print(f"{mode}\t{len(tests)}\t{total}\t{reason}")
    print(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

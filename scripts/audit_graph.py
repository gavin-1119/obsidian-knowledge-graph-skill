#!/usr/bin/env python3
"""Audit Obsidian links and verify that changed domain notes join the graph."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


WIKILINK_RE = re.compile(r"!?\[\[([^\]]+)\]\]")
IGNORED_DIRS = {".git", ".obsidian", ".trash", "node_modules"}


def normalize_target(raw: str) -> str:
    # Obsidian table links escape their alias separator as ``\|``.
    unescaped = raw.replace("\\|", "|")
    target = unescaped.split("|", 1)[0].split("#", 1)[0].split("^", 1)[0].strip()
    if target.lower().endswith(".md"):
        target = target[:-3]
    return target.strip("/")


def markdown_files(vault: Path) -> list[Path]:
    return sorted(
        path
        for path in vault.rglob("*.md")
        if not any(part in IGNORED_DIRS for part in path.relative_to(vault).parts)
    )


def attachment_files(vault: Path) -> list[Path]:
    return sorted(
        path
        for path in vault.rglob("*")
        if path.is_file()
        and path.suffix.lower() != ".md"
        and not any(part in IGNORED_DIRS for part in path.relative_to(vault).parts)
    )


def relative_without_suffix(path: Path, vault: Path) -> str:
    return path.relative_to(vault).with_suffix("").as_posix()


def resolve_note(
    target: str,
    source: Path,
    vault: Path,
    by_relative: dict[str, Path],
    by_stem: dict[str, list[Path]],
) -> tuple[str, Path | list[Path] | None]:
    if not target:
        return "empty", None

    normalized = target.replace("\\", "/")
    if "/" in normalized:
        exact = by_relative.get(normalized)
        if exact:
            return "resolved", exact
        source_relative = source.parent.relative_to(vault)
        relative_candidate = (source_relative / normalized).as_posix()
        exact = by_relative.get(relative_candidate)
        if exact:
            return "resolved", exact
        return "unresolved", None

    matches = by_stem.get(Path(normalized).name, [])
    if len(matches) == 1:
        return "resolved", matches[0]
    if len(matches) > 1:
        return "ambiguous", matches
    return "unresolved", None


def resolve_focus(
    value: str,
    vault: Path,
    by_relative: dict[str, Path],
    by_stem: dict[str, list[Path]],
) -> tuple[Path | None, str | None]:
    normalized = value.replace("\\", "/").strip("/")
    if normalized.lower().endswith(".md"):
        normalized = normalized[:-3]
    if "/" in normalized:
        exact = by_relative.get(normalized)
        if exact:
            return exact, None
        return None, f"missing focus: {value}"
    matches = by_stem.get(Path(normalized).name, [])
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        return None, f"ambiguous focus: {value}"
    return None, f"missing focus: {value}"


def is_knowledge_note(path: Path, vault: Path, knowledge_root: str) -> bool:
    try:
        relative = path.relative_to(vault / knowledge_root)
    except ValueError:
        return False
    return bool(relative.parts) and path.stem != "Home"


def audit(
    vault: Path, focus_values: list[str], knowledge_root: str
) -> tuple[dict, bool]:
    notes = markdown_files(vault)
    attachments = attachment_files(vault)
    by_relative = {relative_without_suffix(path, vault): path for path in notes}
    by_stem: dict[str, list[Path]] = defaultdict(list)
    for path in notes:
        by_stem[path.stem].append(path)
    attachments_by_relative = {
        path.relative_to(vault).as_posix(): path for path in attachments
    }
    attachments_by_name: dict[str, list[Path]] = defaultdict(list)
    for path in attachments:
        attachments_by_name[path.name].append(path)

    outbound: dict[Path, set[Path]] = defaultdict(set)
    inbound: dict[Path, set[Path]] = defaultdict(set)
    unresolved: dict[Path, list[str]] = defaultdict(list)
    ambiguous: dict[Path, dict[str, list[str]]] = defaultdict(dict)
    asset_refs = 0

    for source in notes:
        text = source.read_text(encoding="utf-8")
        for raw_target in WIKILINK_RE.findall(text):
            target = normalize_target(raw_target)
            if not target:
                continue

            if Path(target).suffix:
                direct = attachments_by_relative.get(target)
                source_relative = (source.parent.relative_to(vault) / target).as_posix()
                local = attachments_by_relative.get(source_relative)
                named = attachments_by_name.get(Path(target).name, [])
                if direct or local or len(named) == 1:
                    asset_refs += 1
                    continue
                if len(named) > 1:
                    ambiguous[source][target] = [
                        path.relative_to(vault).as_posix() for path in named
                    ]
                    continue

            status, resolved = resolve_note(
                target, source, vault, by_relative, by_stem
            )
            if status == "resolved" and isinstance(resolved, Path):
                if resolved != source:
                    outbound[source].add(resolved)
                    inbound[resolved].add(source)
            elif status == "ambiguous" and isinstance(resolved, list):
                ambiguous[source][target] = [
                    path.relative_to(vault).as_posix() for path in resolved
                ]
            elif status == "unresolved":
                unresolved[source].append(target)

    focus_reports = []
    focus_failed = False
    for value in focus_values:
        path, error = resolve_focus(value, vault, by_relative, by_stem)
        if error:
            focus_reports.append({"requested": value, "problems": [error]})
            focus_failed = True
            continue

        assert path is not None
        problems = []
        outlinks = sorted(
            target.relative_to(vault).as_posix() for target in outbound[path]
        )
        backlinks = sorted(
            source.relative_to(vault).as_posix() for source in inbound[path]
        )
        unresolved_focus = sorted(set(unresolved[path]))
        ambiguous_focus = ambiguous[path]

        if is_knowledge_note(path, vault, knowledge_root):
            if not outlinks:
                problems.append("no outbound note links")
            if not backlinks:
                problems.append("no inbound note links")
        if unresolved_focus:
            problems.append("unresolved links")
        if ambiguous_focus:
            problems.append("ambiguous links")
        if problems:
            focus_failed = True

        focus_reports.append(
            {
                "requested": value,
                "path": path.relative_to(vault).as_posix(),
                "outbound": outlinks,
                "inbound": backlinks,
                "unresolved": unresolved_focus,
                "ambiguous": ambiguous_focus,
                "problems": problems,
            }
        )

    knowledge_notes = [
        path
        for path in notes
        if is_knowledge_note(path, vault, knowledge_root)
    ]
    isolated = sorted(
        path.relative_to(vault).as_posix()
        for path in knowledge_notes
        if not outbound[path] and not inbound[path]
    )

    edge_count = sum(len(targets) for targets in outbound.values())
    report = {
        "vault": str(vault),
        "knowledge_root": knowledge_root,
        "markdown_note_count": len(notes),
        "attachment_count": len(attachments),
        "directed_note_edge_count": edge_count,
        "asset_reference_count": asset_refs,
        "global_unresolved_link_count": sum(
            len(set(values)) for values in unresolved.values()
        ),
        "global_ambiguous_link_count": sum(
            len(values) for values in ambiguous.values()
        ),
        "isolated_knowledge_notes": isolated,
        "isolated_wiki_notes": isolated,
        "focus": focus_reports,
        "focus_passed": not focus_failed,
    }
    return report, focus_failed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit Obsidian wikilinks and changed-note graph connectivity."
    )
    parser.add_argument(
        "--vault",
        required=True,
        help="Absolute path to the Obsidian vault.",
    )
    parser.add_argument(
        "--focus",
        action="append",
        default=[],
        help="Changed note name or vault-relative path. Repeat for multiple notes.",
    )
    parser.add_argument(
        "--knowledge-root",
        default="10_领域",
        help="Vault-relative root containing classified domain notes.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of a concise text report.",
    )
    args = parser.parse_args()

    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        print(f"Vault not found: {vault}", file=sys.stderr)
        return 2

    knowledge_root_path = Path(args.knowledge_root.replace("\\", "/"))
    if knowledge_root_path.is_absolute() or ".." in knowledge_root_path.parts:
        print("Knowledge root must be a safe vault-relative path.", file=sys.stderr)
        return 2
    knowledge_root = knowledge_root_path.as_posix().strip("/")
    if not knowledge_root or not (vault / knowledge_root).is_dir():
        print(f"Knowledge root not found: {vault / knowledge_root}", file=sys.stderr)
        return 2

    report, focus_failed = audit(vault, args.focus, knowledge_root)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"vault: {report['vault']}")
        print(f"knowledge root: {report['knowledge_root']}")
        print(f"notes: {report['markdown_note_count']}")
        print(f"directed note edges: {report['directed_note_edge_count']}")
        print(f"asset references: {report['asset_reference_count']}")
        print(f"global unresolved links: {report['global_unresolved_link_count']}")
        print(f"global ambiguous links: {report['global_ambiguous_link_count']}")
        print(
            "isolated knowledge notes: "
            f"{len(report['isolated_knowledge_notes'])}"
        )
        for item in report["focus"]:
            status = "PASS" if not item["problems"] else "FAIL"
            print(f"{status} focus: {item.get('path', item['requested'])}")
            for problem in item["problems"]:
                print(f"  - {problem}")

    return 1 if focus_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Repo Requiem - write an obituary for a repository that has gone quiet.

Reads a local git repository's history, asks an open-weight model served by
Ollama for a eulogy, and renders the result as Markdown.

Python 3.9+ and git. No third-party packages.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_MODEL = os.environ.get("REQUIEM_MODEL", "gemma4:cloud")
DEFAULT_OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MAX_DELETED_SHOWN = 8
MAX_CONTRIBUTORS = 4
README_CHARS = 1200


class RequiemError(RuntimeError):
    """A problem the user can fix."""


@dataclass
class Facts:
    name: str
    path: str
    commits: int
    contributors: list[tuple[str, int]] = field(default_factory=list)
    first_commit: str = ""
    last_commit: str = ""
    lifespan_days: int = 0
    dormant_days: int = 0
    branches: int = 0
    tags: int = 0
    deleted_files: list[str] = field(default_factory=list)
    busiest_files: list[tuple[str, int]] = field(default_factory=list)
    readme: str = ""
    cause_of_death: str = ""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or f"git {' '.join(args)} failed"
        raise RequiemError(message)
    return proc.stdout


def _parse_date(value: str) -> datetime:
    return datetime.fromisoformat(value.strip())


def _read_readme(repo: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "readme.md", "Readme.md"):
        candidate = repo / name
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8", errors="replace")
            text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
            text = re.sub(r"\[!\[.*?\]\(.*?\)\]\(.*?\)", "", text)
            text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text.strip()[:README_CHARS]
    return ""


def _cause_of_death(facts: Facts) -> str:
    """A poetic-but-grounded guess, from the numbers alone."""
    if len(facts.contributors) == 1 and facts.dormant_days >= 300:
        return "maintainer exhaustion"
    if facts.dormant_days >= 365 * 5:
        return "the long silence"
    if len(facts.deleted_files) >= 20:
        return "a refactor that never ended"
    if facts.dormant_days >= 365 * 2:
        return "dependency rot"
    if facts.commits <= 3:
        return "complications at birth"
    if facts.lifespan_days <= 7 and facts.dormant_days >= 180:
        return "a weekend that got away"
    return "unknown causes"


def collect_facts(repo: Path, max_commits: int = 400) -> Facts:
    """Read the repository's vital signs. No network, no writes."""
    repo = repo.resolve()
    if not repo.is_dir():
        raise RequiemError(f"Not a directory: {repo}")
    if _git(repo, "rev-parse", "--is-inside-work-tree").strip() != "true":
        raise RequiemError(f"Not a git repository: {repo}")

    commit_dates = [
        _parse_date(line)
        for line in _git(repo, "log", "--format=%aI").splitlines()
        if line.strip()
    ]
    if not commit_dates:
        raise RequiemError("This repository has no commits to mourn.")

    first, last = min(commit_dates), max(commit_dates)
    now = datetime.now(timezone.utc)

    contributors: list[tuple[str, int]] = []
    for line in _git(repo, "shortlog", "-sne", "--no-merges", "HEAD").splitlines():
        match = re.match(r"\s*(\d+)\s+(.*?)\s*<[^>]*>$", line)
        if match:
            contributors.append((match.group(2).strip(), int(match.group(1))))
    contributors.sort(key=lambda item: item[1], reverse=True)

    deleted: list[str] = []
    seen: set[str] = set()
    deleted_log = _git(
        repo,
        "log",
        f"-n{max_commits}",
        "--diff-filter=D",
        "--pretty=format:",
        "--name-only",
    )
    for line in deleted_log.splitlines():
        rel = line.strip()
        if rel and rel not in seen and not (repo / rel).exists():
            seen.add(rel)
            deleted.append(rel)

    touches: Counter[str] = Counter()
    names_log = _git(
        repo, "log", f"-n{max_commits}", "--pretty=format:", "--name-only"
    )
    for line in names_log.splitlines():
        rel = line.strip()
        if rel:
            touches[rel] += 1

    branch_count = len(
        [b for b in _git(repo, "branch", "-a", "--format=%(refname:short)").splitlines() if b]
    )
    tag_count = len([t for t in _git(repo, "tag").splitlines() if t.strip()])

    facts = Facts(
        name=repo.name,
        path=str(repo),
        commits=int(_git(repo, "rev-list", "--count", "HEAD").strip()),
        contributors=contributors,
        first_commit=first.date().isoformat(),
        last_commit=last.date().isoformat(),
        lifespan_days=max(0, (last - first).days),
        dormant_days=max(0, (now - last).days),
        branches=branch_count,
        tags=tag_count,
        deleted_files=deleted,
        busiest_files=touches.most_common(5),
        readme=_read_readme(repo),
    )
    facts.cause_of_death = _cause_of_death(facts)
    return facts


SYSTEM_PROMPT = """You are the undertaker of open-source software. You write \
tender, slightly eerie eulogies for repositories that have gone quiet. You \
never mock maintainers, never invent facts, and never mention that you are an \
AI. Return JSON only."""


def build_prompt(facts: Facts) -> str:
    evidence = {
        "repository": facts.name,
        "first_commit": facts.first_commit,
        "last_commit": facts.last_commit,
        "lifespan_days": facts.lifespan_days,
        "quiet_for_days": facts.dormant_days,
        "commits": facts.commits,
        "contributors": [{"name": n, "commits": c} for n, c in facts.contributors[:MAX_CONTRIBUTORS]],
        "branches": facts.branches,
        "tags": facts.tags,
        "deleted_files": facts.deleted_files[:MAX_DELETED_SHOWN],
        "busiest_files": [{"path": p, "touches": c} for p, c in facts.busiest_files],
        "undertaker_note": facts.cause_of_death,
        "readme_excerpt": facts.readme,
    }
    return f"""Here is the evidence from the repository's life:

{json.dumps(evidence, indent=2)}

Write its obituary. Return ONLY a JSON object with exactly these keys:

- "epitaph": at most 12 words, carved on a tombstone. No quotation marks.
- "obituary": 110-170 words, two short paragraphs, third person, warm and a \
little haunted. Use the real numbers and names above. Do not invent people, \
dates, or events. Do not call it a failure.
- "cause_of_death": at most 6 words, poetic but grounded in the evidence.
- "last_words": at most 18 words, phrased as a final commit message whispered \
from the grave.
- "survived_by": at most 12 words, e.g. forks, stale branches, a README, or \
nobody at all.

No markdown inside the JSON values."""


def ask_ollama(model: str, prompt: str, base_url: str = DEFAULT_OLLAMA) -> dict:
    base = base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = f"http://{base}"
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "options": {"temperature": 0.8, "num_predict": 600},
    }
    request = urllib.request.Request(
        f"{base}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = json.load(response)
    except urllib.error.URLError as exc:
        raise RequiemError(
            f"Could not reach Ollama at {base} ({exc.reason}).\n"
            "Start it with `ollama serve`, then pull a model, e.g. "
            "`ollama pull gemma3:4b`, or pass --model <name>."
        ) from exc
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RequiemError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
    try:
        content = body["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise RequiemError(f"Unexpected response from Ollama: {body!r}") from exc
    return parse_json_object(content)


def parse_json_object(text: str) -> dict:
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise RequiemError("The model did not return a JSON object.")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise RequiemError(f"The model returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RequiemError("The model returned JSON that is not an object.")
    return data


def _field(eulogy: dict, key: str, fallback: str) -> str:
    value = eulogy.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def tombstone(name: str, years: str, epitaph: str, width: int = 40) -> str:
    rows: list[list[str]] = []

    def add(text: str = "") -> None:
        wrapped = textwrap.wrap(text, width - 4) or [""]
        rows.extend(wrapped)

    add(name)
    add(years)
    rows.append("")
    add(f'"{epitaph}"')
    rows.append("")

    lines = [" " * 4 + "/" + " " * width + "\\"]
    lines.extend(" " * 4 + "|" + row.center(width) + "|" for row in rows)
    lines.append(" " * 4 + "\\" + "_" * width + "/")
    return "\n".join(lines)


def render(facts: Facts, eulogy: dict, model: str) -> str:
    epitaph = _field(eulogy, "epitaph", "It ran here once.")
    obituary = _field(
        eulogy,
        "obituary",
        "The commits stopped, and the branches stayed open like doors.",
    )
    cause = _field(eulogy, "cause_of_death", facts.cause_of_death)
    last_words = _field(eulogy, "last_words", "chore: goodnight")
    survived = _field(eulogy, "survived_by", "a README and no tests")

    years = f"{facts.first_commit} - {facts.last_commit}"
    contributors = ", ".join(f"{name} ({count})" for name, count in facts.contributors[:MAX_CONTRIBUTORS])
    deleted = ", ".join(f"`{path}`" for path in facts.deleted_files[:MAX_DELETED_SHOWN])
    if len(facts.deleted_files) > MAX_DELETED_SHOWN:
        deleted += f" (+{len(facts.deleted_files) - MAX_DELETED_SHOWN} more)"
    busiest = ", ".join(f"`{path}` ({count})" for path, count in facts.busiest_files)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""# In Memoriam: {facts.name}

```text
{tombstone(facts.name, years, epitaph)}
```

> {epitaph}

## Obituary

{obituary}

## Cause of death

**{cause}**

## Last words

> {last_words}

## Survived by

{survived}

## The autopsy

- **Lived:** {facts.first_commit} to {facts.last_commit} ({facts.lifespan_days:,} days)
- **Quiet for:** {facts.dormant_days:,} days
- **Commits:** {facts.commits:,}
- **Contributors:** {contributors or "nobody on record"}
- **Branches / tags:** {facts.branches} / {facts.tags}
- **Deleted files:** {deleted or "none that stayed deleted"}
- **Busiest files:** {busiest or "nothing was touched twice"}
- **Undertaker's note:** {facts.cause_of_death}

---

*Eulogy written by `{model}` through Ollama on {stamp}. The history was read
locally; choose a local model tag to keep the whole ritual on your machine.*
"""


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="requiem",
        description="Write an obituary for a repository that has gone quiet.",
    )
    parser.add_argument("repo", nargs="?", default=".", help="path to a local git repository")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL})")
    parser.add_argument("--out", default="obituary.md", help='output file, or "-" for stdout')
    parser.add_argument("--commits", type=int, default=400, help="how many commits to autopsy (default: 400)")
    parser.add_argument("--dry-run", action="store_true", help="print the collected facts and stop")
    args = parser.parse_args(argv)

    try:
        facts = collect_facts(Path(args.repo), max_commits=args.commits)
        if args.dry_run:
            print(json.dumps(asdict(facts), indent=2, ensure_ascii=False))
            return 0

        print(f"Reading the remains of {facts.name}...", file=sys.stderr)
        eulogy = ask_ollama(args.model, build_prompt(facts))
        document = render(facts, eulogy, args.model)

        if args.out == "-":
            print(document)
        else:
            output = Path(args.out)
            output.write_text(document, encoding="utf-8")
            print(f"Requiem written to {output.resolve()}")
        return 0
    except RequiemError as exc:
        print(f"requiem: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nrequiem: the service was interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

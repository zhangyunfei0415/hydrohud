"""Assemble the docs site source tree under ``build/docs-site/docs``.

The repository keeps prose in one place: ``README.md`` at the root and the
topic guides in ``docs/``. A MkDocs site needs a single ``docs_dir`` whose
pages can reference each other with site-relative paths, so this script copies
those files into ``build/docs-site/docs`` and rewrites every relative link
that would otherwise point outside the site.

Usage::

    python tools/build_docs.py            # lay out the tree
    python tools/build_docs.py --strict   # fail on links that resolve nowhere

Afterwards ``python -m mkdocs build`` (root ``mkdocs.yml``) renders the site.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "build" / "docs-site" / "docs"

GITHUB = "https://github.com/zhangyunfei0415/hydrohud"

#: Repository file -> page path inside the site.
PAGES = {
    "README.md": "index.md",
    "README.en.md": "index.en.md",
    "ROADMAP.md": "roadmap.md",
    "AGENTS.md": "agents.md",
    "docs/LAYERS.md": "layers.md",
    "docs/AUTOMATION.md": "automation.md",
    "docs/PLUGINS.md": "plugins.md",
    "docs/LINUX.md": "linux.md",
    "docs/PROTOCOL.md": "protocol.md",
}

#: Directories copied verbatim so that images keep working.
ASSET_DIRS = ("assets", "preview")

#: Inline link syntax: ``[label](target)`` and ``![alt](target)``.
_LINK_RE = re.compile(r"(!?\[[^\]]*\])\(([^)\s]+)\)")


def _site_path(repo_path: str) -> str | None:
    """Map a repository path to its site path, or None when it is not published."""
    if repo_path in PAGES:
        return PAGES[repo_path]
    for directory in ASSET_DIRS:
        if repo_path.startswith(directory + "/"):
            return repo_path
    return None


def _github_url(repo_path: str) -> str:
    """Point unpublished repository paths at GitHub instead of a dead page."""
    return f"{GITHUB}/tree/main/{repo_path.rstrip('/')}"


def rewrite_links(text: str, source: str) -> str:
    """Rewrite relative links in ``source`` so they resolve inside the site.

    ``source`` is the published page path (for example ``layers.md``); its
    directory is the base every relative link is resolved against in the real
    repository, which this function re-expresses as a site path or a GitHub URL.
    """

    def replace(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return match.group(0)
        anchor = ""
        path, _, fragment = target.partition("#")
        if fragment:
            anchor = "#" + fragment
        if not path:
            return match.group(0)
        repo_path = _resolve(source, path)
        site_path = _site_path(repo_path)
        if site_path is not None:
            return f"{label}({site_path}{anchor})"
        return f"{label}({_github_url(repo_path)}{anchor})"

    return _LINK_RE.sub(replace, text)


def _resolve(source: str, target: str) -> str:
    """Resolve a repository-relative link into a normalised repository path."""
    # A page's links are written relative to the file it came from; index.md
    # comes from the repository root, every other page from docs/.
    origin = "README.md" if source == "index.md" else _origin_of(source)
    base = Path(origin).parent
    return Path(_normalize(base / target)).as_posix()


def _origin_of(page: str) -> str:
    for repo_path, site_path in PAGES.items():
        if site_path == page:
            return repo_path
    raise KeyError(page)


def _normalize(path: Path) -> Path:
    """Collapse ``..`` segments without touching the filesystem."""
    parts: list[str] = []
    for part in path.as_posix().split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return Path("/".join(parts))


def build(output: Path = OUTPUT, strict: bool = False) -> list[str]:
    """Write the site source tree and return a list of warnings."""
    warnings: list[str] = []
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for repo_path, page in PAGES.items():
        source = ROOT / repo_path
        if not source.is_file():
            raise SystemExit(f"missing source page: {repo_path}")
        text = rewrite_links(source.read_text(encoding="utf-8"), page)
        destination = output / page
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    for directory in ASSET_DIRS:
        source_dir = ROOT / directory
        if source_dir.is_dir():
            shutil.copytree(source_dir, output / directory)
    if strict:
        for page in PAGES.values():
            warnings.extend(_check_links((output / page).read_text(encoding="utf-8"), page, output))
    return warnings


def _check_links(text: str, page: str, output: Path) -> list[str]:
    problems = []
    for match in _LINK_RE.finditer(text):
        target = match.group(2)
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.partition("#")[0]
        if not path:
            continue
        if not (output / path).exists():
            problems.append(f"{page}: link target does not exist: {target}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建文档站的源目录")
    parser.add_argument("--strict", action="store_true", help="链接解析不到时报错")
    args = parser.parse_args(argv)
    warnings = build(strict=args.strict)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if warnings and args.strict:
        return 1
    pages = ", ".join(sorted(PAGES.values()))
    print(f"docs source tree ready: {OUTPUT} ({pages})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

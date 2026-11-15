"""文档站的构建脚本：链接改写与产出目录。

这些测试只读写临时目录，不调用 mkdocs，也不联网。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOLS = REPO_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import build_docs  # noqa: E402


def test_published_pages_map_to_site_paths() -> None:
    assert build_docs._site_path("README.md") == "index.md"
    assert build_docs._site_path("docs/LAYERS.md") == "layers.md"
    assert build_docs._site_path("docs/PROTOCOL.md") == "protocol.md"


def test_unpublished_paths_have_no_site_page() -> None:
    assert build_docs._site_path("ROADMAPX.md") is None
    assert build_docs._site_path("hydrohud/app.py") is None


def test_asset_directories_keep_their_relative_paths() -> None:
    assert build_docs._site_path("assets/screenshot.png") == "assets/screenshot.png"
    assert build_docs._site_path("preview/01-minimal-clock.png") == ("preview/01-minimal-clock.png")


def test_links_to_published_pages_are_rewritten() -> None:
    text = "见 [图层参考](docs/LAYERS.md) 与 ![截图](assets/screenshot.png)。"
    rewritten = build_docs.rewrite_links(text, "index.md")
    assert "[图层参考](layers.md)" in rewritten
    assert "![截图](assets/screenshot.png)" in rewritten


def test_links_from_a_docs_page_resolve_against_its_source_directory() -> None:
    text = "回到 [协议](PROTOCOL.md)，插件见 [自动化](AUTOMATION.md#图层插件)。"
    rewritten = build_docs.rewrite_links(text, "linux.md")
    assert "[协议](protocol.md)" in rewritten
    assert "[自动化](automation.md#图层插件)" in rewritten


def test_links_outside_the_site_point_at_github() -> None:
    text = "源码在 [app.py](hydrohud/app.py) 与 [说明](AGENTS.md)。"
    rewritten = build_docs.rewrite_links(text, "index.md")
    assert "https://github.com/zhangyunfei0415/hydrohud/tree/main/hydrohud/app.py" in rewritten
    assert "[说明](agents.md)" in rewritten


def test_links_from_a_guide_can_reach_the_repository_root() -> None:
    text = "测试在 [test_plugins.py](../tests/test_plugins.py)。"
    rewritten = build_docs.rewrite_links(text, "plugins.md")
    assert "tree/main/tests/test_plugins.py" in rewritten


def test_absolute_anchors_and_external_links_are_left_alone() -> None:
    text = (
        "跳到 [本页](#绘制契约)，外部看 [liquidctl](https://github.com/liquidctl/liquidctl)，"
        "邮件 [作者](mailto:a@b.c)。"
    )
    assert build_docs.rewrite_links(text, "plugins.md") == text


def test_anchor_suffixes_survive_rewriting() -> None:
    rewritten = build_docs.rewrite_links("[自动化](docs/AUTOMATION.md#sse-状态流)", "index.md")
    assert rewritten == "[自动化](automation.md#sse-状态流)"


def test_normalize_collapses_parent_segments() -> None:
    assert build_docs._normalize(Path("docs") / ".." / "tests" / "x.py").as_posix() == "tests/x.py"
    assert build_docs._normalize(Path("..") / ".." / "x.py").as_posix() == "x.py"


def test_build_writes_every_nav_page_and_copies_assets(tmp_path) -> None:
    output = tmp_path / "docs"
    warnings = build_docs.build(output=output, strict=True)
    assert warnings == []
    for page in build_docs.PAGES.values():
        assert (output / page).is_file(), page
    assert (output / "assets" / "app.png").is_file()
    assert (output / "preview" / "contact-sheet.png").is_file()
    index = (output / "index.md").read_text(encoding="utf-8")
    assert "](docs/" not in index
    assert "](layers.md)" in index


def test_the_nav_and_the_page_map_agree() -> None:
    """mkdocs.yml 的 nav 里出现的每一页，构建脚本都必须产出。"""

    config = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    nav_section = config.split("\nnav:", 1)[1].split("\n\n", 1)[0]
    listed = {
        line.rsplit(":", 1)[1].strip()
        for line in nav_section.splitlines()
        if ":" in line and line.strip().endswith(".md")
    }
    assert listed == set(build_docs.PAGES.values())


def test_mkdocs_docs_dir_matches_the_build_output() -> None:
    config = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "docs_dir: build/docs-site/docs" in config
    assert build_docs.OUTPUT == REPO_ROOT / "build" / "docs-site" / "docs"

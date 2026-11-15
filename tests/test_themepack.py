"""主题包导入/导出测试：全部使用临时目录，不触及用户真实 AppData。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from hydrohud import themepack


@pytest.fixture()
def temp_assets(tmp_path: Path) -> Path:
    """准备两张素材图（内容无关，只要是存在的文件即可）。"""
    png = tmp_path / "photo.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\nfake-body")
    gif = tmp_path / "anim.gif"
    gif.write_bytes(b"GIF89a-fake-body")
    return tmp_path


def _scene(path: Path, second: Path) -> dict:
    return {
        "name": "测试主题",
        "background": {"type": "image", "path": str(path), "fit": "crop"},
        "layers": [
            {"type": "sequence", "paths": [str(second)], "fps": 8},
            {"type": "text", "text": "hello", "x": 0.5, "y": 0.5, "size": 40},
        ],
    }


def test_export_writes_theme_and_assets(temp_assets: Path) -> None:
    scene = _scene(temp_assets / "photo.png", temp_assets / "anim.gif")
    target = temp_assets / "pack.hudpack"
    result = themepack.export_pack(scene, target)
    assert result["assets"] == 2
    assert result["missing"] == []
    assert target.is_file()
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    assert themepack.THEME_FILE in names
    assert any(name.startswith(themepack.ASSETS_DIR + "/") for name in names)


def test_export_rewrites_paths_inside_pack(temp_assets: Path) -> None:
    scene = _scene(temp_assets / "photo.png", temp_assets / "anim.gif")
    target = temp_assets / "pack.hudpack"
    themepack.export_pack(scene, target)
    with zipfile.ZipFile(target) as archive:
        raw = json.loads(archive.read(themepack.THEME_FILE))
    assert raw["background"]["path"].startswith(themepack.ASSETS_DIR + "/")
    assert raw["layers"][0]["paths"][0].startswith(themepack.ASSETS_DIR + "/")
    assert raw["format"] == themepack.FORMAT


def test_export_does_not_mutate_input_scene(temp_assets: Path) -> None:
    scene = _scene(temp_assets / "photo.png", temp_assets / "anim.gif")
    original = json.dumps(scene, sort_keys=True)
    themepack.export_pack(scene, temp_assets / "pack.hudpack")
    assert json.dumps(scene, sort_keys=True) == original


def test_export_reports_missing_assets(temp_assets: Path) -> None:
    scene = _scene(temp_assets / "nope.png", temp_assets / "anim.gif")
    result = themepack.export_pack(scene, temp_assets / "pack.hudpack")
    assert result["missing"] == [str(temp_assets / "nope.png")]


def test_export_accepts_name_override(temp_assets: Path) -> None:
    scene = _scene(temp_assets / "photo.png", temp_assets / "anim.gif")
    target = temp_assets / "pack.hudpack"
    themepack.export_pack(scene, target, name="新名字")
    with zipfile.ZipFile(target) as archive:
        raw = json.loads(archive.read(themepack.THEME_FILE))
    assert raw["name"] == "新名字"


def test_import_roundtrip_restores_absolute_paths(temp_assets: Path) -> None:
    scene = _scene(temp_assets / "photo.png", temp_assets / "anim.gif")
    target = temp_assets / "pack.hudpack"
    themepack.export_pack(scene, target)
    destination = temp_assets / "unpacked"
    result = themepack.import_pack(target, dest_dir=destination)
    imported = result["scene"]
    background = imported["background"]["path"]
    assert Path(background).is_file()
    assert Path(background).is_relative_to(destination)
    assert Path(imported["layers"][0]["paths"][0]).is_file()
    assert result["assets"] == 2


def test_import_without_dest_dir_uses_packs_dir(temp_assets: Path, monkeypatch) -> None:
    monkeypatch.setattr(themepack, "_app_data_dir", lambda: temp_assets / "appdata")
    scene = _scene(temp_assets / "photo.png", temp_assets / "anim.gif")
    target = temp_assets / "theme.hudpack"
    themepack.export_pack(scene, target)
    first = themepack.import_pack(target)
    second = themepack.import_pack(target)
    assert first["dir"] != second["dir"]
    assert Path(second["dir"]).name.endswith("-2")


def test_import_rejects_missing_theme_file(temp_assets: Path) -> None:
    broken = temp_assets / "broken.hudpack"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("assets/1.png", b"data")
    with pytest.raises(ValueError):
        themepack.import_pack(broken, dest_dir=temp_assets / "out")


def test_import_rejects_broken_json(temp_assets: Path) -> None:
    broken = temp_assets / "broken.hudpack"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr(themepack.THEME_FILE, b"{not json")
    with pytest.raises(ValueError):
        themepack.import_pack(broken, dest_dir=temp_assets / "out")


def test_import_skips_path_traversal_entries(temp_assets: Path) -> None:
    evil = temp_assets / "evil.hudpack"
    with zipfile.ZipFile(evil, "w") as archive:
        archive.writestr(themepack.THEME_FILE, json.dumps({"name": "evil", "layers": []}))
        archive.writestr("../escaped.txt", b"nope")
        archive.writestr("assets/../../escaped2.txt", b"nope")
    destination = temp_assets / "evil-out"
    themepack.import_pack(evil, dest_dir=destination)
    assert not (temp_assets / "escaped.txt").exists()
    assert not (temp_assets / "escaped2.txt").exists()
    assert list(destination.rglob("escaped*")) == []


def test_list_and_remove_packs(temp_assets: Path, monkeypatch) -> None:
    monkeypatch.setattr(themepack, "_app_data_dir", lambda: temp_assets / "appdata")
    scene = _scene(temp_assets / "photo.png", temp_assets / "anim.gif")
    themepack.export_pack(scene, themepack.packs_dir() / "demo.hudpack")
    listed = themepack.list_packs()
    assert [item["name"] for item in listed] == ["demo"]
    assert themepack.remove_pack("demo") is True
    assert themepack.list_packs() == []
    assert themepack.remove_pack("demo") is False


def test_asset_paths_dedupes_repeated_files(temp_assets: Path) -> None:
    path = str(temp_assets / "photo.png")
    scene = {
        "background": {"type": "image", "path": path},
        "layers": [{"type": "image", "path": path}],
    }
    assert themepack._asset_paths(scene) == [path]


def test_list_and_remove_packs_covers_extracted_dirs(temp_assets: Path, monkeypatch) -> None:
    """解压出来的 ``themes/<名称>/`` 目录也会被列出并能删除。"""
    monkeypatch.setattr(themepack, "_app_data_dir", lambda: temp_assets / "appdata")
    scene = _scene(temp_assets / "photo.png", temp_assets / "anim.gif")
    source = temp_assets / "shared.hudpack"
    themepack.export_pack(scene, source)
    imported = themepack.import_pack(source)

    listed = themepack.list_packs()
    assert [item["name"] for item in listed] == ["shared"]
    assert listed[0]["kind"] == "dir"
    assert listed[0]["path"] == imported["dir"]
    assert listed[0]["bytes"] > 0

    assert themepack.remove_pack("shared") is True
    assert not Path(imported["dir"]).exists()
    assert themepack.list_packs() == []


def test_list_packs_ignores_dirs_without_theme_file(temp_assets: Path, monkeypatch) -> None:
    """缺少 ``theme.json`` 的目录不算主题包。"""
    monkeypatch.setattr(themepack, "_app_data_dir", lambda: temp_assets / "appdata")
    stray = themepack.packs_dir() / "not-a-pack"
    stray.mkdir(parents=True)
    (stray / "notes.txt").write_text("hi", encoding="utf-8")
    assert themepack.list_packs() == []
    assert themepack.remove_pack("not-a-pack") is False

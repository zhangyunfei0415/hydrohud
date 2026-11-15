"""主题包（``.hudpack``）：把工程场景连同它引用的素材打成一个可分享的 zip。

主题包是一个普通 zip：

``theme.json``
    场景 JSON（与 ``.hud.json`` 同一格式），其中素材路径全部改写为 ``assets/`` 下的相对路径。
``assets/<n>.<ext>``
   场景引用的图片 / GIF / 视频等素材，按出现顺序重新编号。

导出时素材会被复制进包里；导入时会被解压到 :func:`packs_dir` 下的独立目录，
再把 ``theme.json`` 里的相对路径改写回解压后的绝对路径，随后可以直接交给
``hydrohud.scenes.load_project`` 使用。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any

PACK_SUFFIX = ".hudpack"
THEME_FILE = "theme.json"
ASSETS_DIR = "assets"
FORMAT = "hydrohud.theme/1"

_APP_DIR_NAME = "HydroHUD"


def _app_data_dir() -> Path:
    """返回 ``%APPDATA%\\HydroHUD``；非 Windows 上退回用户主目录下的同名目录。"""
    app_data = os.environ.get("APPDATA")
    if app_data:
        return Path(app_data) / _APP_DIR_NAME
    return Path.home() / _APP_DIR_NAME


def packs_dir() -> Path:
    """主题包安装目录 ``%APPDATA%\\HydroHUD\\themes``。"""
    return _app_data_dir() / "themes"


def _asset_paths(scene: dict) -> list[str]:
    """按出现顺序收集场景里引用到的本地素材路径（去重）。"""
    found: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text:
            return
        key = os.path.normcase(os.path.abspath(text))
        if key in seen:
            return
        seen.add(key)
        found.append(text)

    background = scene.get("background")
    if isinstance(background, dict):
        add(background.get("path"))

    layers = scene.get("layers")
    if isinstance(layers, list):
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            add(layer.get("path"))
            paths = layer.get("paths")
            if isinstance(paths, list):
                for item in paths:
                    add(item)
    return found


def _walk_paths(node: Any, mapping: dict[str, str]) -> Any:
    """返回新结构，把所有命中的素材路径按 ``mapping`` 改写。"""
    if isinstance(node, dict):
        return {key: _walk_paths(value, mapping) for key, value in node.items()}
    if isinstance(node, list):
        return [_walk_paths(item, mapping) for item in node]
    if isinstance(node, str):
        return mapping.get(os.path.normcase(os.path.abspath(node)), mapping.get(node, node))
    return node


def _relative_name(index: int, source: str) -> str:
    suffix = Path(source).suffix.lower() or ".bin"
    return f"{ASSETS_DIR}/{index}{suffix}"


def export_pack(
    scene: dict,
    path: str | os.PathLike[str],
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """把场景导出为 ``.hudpack``。

    :param scene: 已归一化的场景 dict；不会被修改。
    :param path: 目标文件路径（建议以 ``.hudpack`` 结尾）。
    :param name: 覆盖包内的场景名称；``None`` 保留原名称。
    :returns: ``{"path": str, "assets": int, "bytes": int, "missing": list[str]}``。
    """
    target = Path(path)
    payload = copy.deepcopy(scene)
    if name:
        payload["name"] = name

    sources = _asset_paths(payload)
    mapping: dict[str, str] = {}
    missing: list[str] = []
    plan: list[tuple[str, str]] = []
    for index, source in enumerate(sources, start=1):
        if not os.path.isfile(source):
            missing.append(source)
            continue
        relative = _relative_name(index, source)
        mapping[os.path.normcase(os.path.abspath(source))] = relative
        plan.append((source, relative))

    payload = _walk_paths(payload, mapping)
    payload["format"] = FORMAT

    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(THEME_FILE, json.dumps(payload, ensure_ascii=False, indent=2))
        for source, relative in plan:
            archive.write(source, relative)

    return {
        "path": str(target),
        "assets": len(plan),
        "bytes": target.stat().st_size,
        "missing": missing,
    }


def _safe_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    """只解压 ``theme.json`` 与 ``assets/`` 下的普通文件，忽略目录穿越条目。"""
    for member in archive.infolist():
        name = member.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            continue
        if name != THEME_FILE and not name.startswith(ASSETS_DIR + "/"):
            continue
        archive.extract(member, destination)


def import_pack(
    path: str | os.PathLike[str],
    *,
    dest_dir: str | os.PathLike[str] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """导入 ``.hudpack``，返回可直接加载的场景。

    :param path: 主题包路径。
    :param dest_dir: 解压目录；``None`` 时用 ``packs_dir()/<包名>``（重名时追加序号）。
    :param name: 覆盖导入后的场景名称；``None`` 保留包内名称。
    :returns: ``{"scene": dict, "dir": str, "assets": int, "name": str}``。
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"主题包不存在：{source}")

    with zipfile.ZipFile(source) as archive:
        try:
            raw = archive.read(THEME_FILE)
        except KeyError as exc:
            raise ValueError(f"主题包缺少 {THEME_FILE}") from exc
        try:
            scene = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"主题包里的 {THEME_FILE} 不是合法 JSON：{exc}") from exc
        if not isinstance(scene, dict):
            raise ValueError(f"{THEME_FILE} 必须是 JSON 对象")

        destination = Path(dest_dir) if dest_dir else packs_dir() / source.stem
        if dest_dir is None:
            index = 2
            base = destination
            while destination.exists():
                destination = base.with_name(f"{base.name}-{index}")
                index += 1
        destination.mkdir(parents=True, exist_ok=True)
        _safe_extract(archive, destination)

    mapping: dict[str, str] = {}
    assets_root = destination / ASSETS_DIR
    if assets_root.is_dir():
        for item in sorted(assets_root.iterdir()):
            if item.is_file():
                mapping[f"{ASSETS_DIR}/{item.name}"] = str(item)

    scene = _walk_paths(scene, mapping)
    scene.pop("format", None)
    if name:
        scene["name"] = name
    return {
        "scene": scene,
        "dir": str(destination),
        "assets": len(mapping),
        "name": str(scene.get("name") or source.stem),
    }


def list_packs() -> list[dict[str, Any]]:
    """列出已安装的主题包（按名称排序）。

    两种形态都会列出：``themes/<名称>.hudpack`` 原始包文件，以及
    :func:`import_pack` 解压出来的 ``themes/<名称>/`` 目录（``kind`` 字段区分）。
    """
    root = packs_dir()
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for pack in sorted(root.glob(f"*{PACK_SUFFIX}")):
        if pack.is_file():
            items.append(
                {"path": str(pack), "name": pack.stem, "bytes": pack.stat().st_size, "kind": "pack"}
            )
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not (folder / THEME_FILE).is_file():
            continue
        items.append(
            {
                "path": str(folder),
                "name": folder.name,
                "bytes": sum(item.stat().st_size for item in folder.rglob("*") if item.is_file()),
                "kind": "dir",
            }
        )
    return sorted(items, key=lambda item: str(item["name"]))


def remove_pack(name: str) -> bool:
    """删除已安装的主题包；返回是否真的删掉了包文件或解压目录。"""
    root = packs_dir()
    stem = name[: -len(PACK_SUFFIX)] if name.endswith(PACK_SUFFIX) else name
    candidate = root / (stem + PACK_SUFFIX)
    if candidate.is_file():
        candidate.unlink()
        return True
    folder = root / stem
    if folder.is_dir() and (folder / THEME_FILE).is_file():
        shutil.rmtree(folder, ignore_errors=True)
        return not folder.exists()
    return False

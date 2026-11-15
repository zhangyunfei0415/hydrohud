"""Linux packaging resources: udev rule, installer, desktop entry and guide.

These tests only parse files under the repository root; they never touch
``/sys``, ``/dev`` or a real panel, so they run identically on every platform.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGING = REPO_ROOT / "packaging" / "linux"
RULES = PACKAGING / "99-hydrohud-lg600.rules"
INSTALLER = PACKAGING / "install-udev.sh"
DESKTOP = PACKAGING / "hydrohud.desktop"
GUIDE = REPO_ROOT / "docs" / "LINUX.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rule_lines() -> list[str]:
    lines = []
    for raw in _read(RULES).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def _desktop_fields() -> dict[str, str]:
    fields = {}
    for raw in _read(DESKTOP).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    return fields


def test_rule_file_exists_and_documents_installation():
    assert RULES.is_file()
    text = _read(RULES)
    assert "HydroHUD" in text
    assert "/etc/udev/rules.d/" in text
    assert "udevadm control --reload-rules" in text


def test_rule_covers_both_panel_product_ids():
    text = _read(RULES)
    assert 'ATTRS{idProduct}=="1520"' in text
    assert 'ATTRS{idProduct}=="151c"' in text


def test_rule_matches_hidraw_and_grants_user_write_access():
    text = _read(RULES)
    assert 'SUBSYSTEM=="hidraw"' in text
    assert 'ATTRS{idVendor}=="1a2c"' in text
    assert 'MODE="0660"' in text
    assert 'TAG+="uaccess"' in text
    assert 'GROUP="plugdev"' in text


def test_every_rule_line_is_complete():
    lines = _rule_lines()
    assert len(lines) == 2
    for line in lines:
        assert "==" in line
        assert not line.endswith(",")
        assert all(part.strip() for part in line.split(","))
        assert 'TAG+="uaccess"' in line


def test_installer_uses_bash_and_strict_mode():
    text = _read(INSTALLER)
    assert text.splitlines()[0] == "#!/usr/bin/env bash"
    assert "set -eu" in text


def test_installer_refuses_to_run_without_root():
    text = _read(INSTALLER)
    assert "id -u" in text
    assert "exit 1" in text


def test_installer_copies_the_shipped_rule_relative_to_itself():
    text = _read(INSTALLER)
    assert "99-hydrohud-lg600.rules" in text
    assert "BASH_SOURCE" in text
    assert "/etc/udev/rules.d" in text


def test_installer_reloads_and_triggers_udev():
    text = _read(INSTALLER)
    assert "udevadm control --reload-rules" in text
    assert "udevadm trigger" in text


def test_desktop_entry_has_required_fields():
    text = _read(DESKTOP)
    assert "[Desktop Entry]" in text
    fields = _desktop_fields()
    assert fields["Type"] == "Application"
    assert fields["Name"] == "HydroHUD"
    assert fields["Exec"] == "hydrohud"
    assert fields["Terminal"] == "false"
    assert fields["Categories"].endswith(";")
    assert fields["Comment"]


def test_guide_exists_and_references_the_real_transport():
    assert GUIDE.is_file()
    text = _read(GUIDE)
    assert "HidrawTransport" in text
    assert "enumerate_hidraw" in text
    assert "HYDROHUD_SYSFS_ROOT" in text
    assert "udev" in text
    assert "python3-tk" in text


def test_guide_documents_both_devices_and_the_frame_budget():
    text = _read(GUIDE)
    assert "1a2c:1520" in text
    assert "1a2c:151c" in text
    assert "46920" in text
    assert "480×480" in text


def test_guide_states_the_current_limitation_honestly():
    text = _read(GUIDE)
    assert "尚未就位" in text
    assert "未验证" in text

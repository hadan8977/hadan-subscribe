"""The read-only preview must start without optional historical screenshots."""

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_preview_builds_without_docs_assets(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("preview_clean", REPO / "scripts/preview.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / "src").symlink_to(REPO / "src", target_is_directory=True)
    (tmp_path / "scripts").symlink_to(REPO / "scripts", target_is_directory=True)
    (tmp_path / "docs").mkdir()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    pages, assets = module.build_pages()
    assert ("/", "zh-CN") in pages
    assert ("/app", "en") in pages
    assert ("/agent", "zh-CN") in pages
    assert ("/preview", "en") in pages
    assert not any(key.startswith("/preview-assets/preview-") for key in assets)
    gallery = pages[("/preview", "en")].decode()
    assert "not included in this checkout" in gallery

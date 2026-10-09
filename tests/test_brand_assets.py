"""Exact owner-provided artwork; HA visual acceptance is a separate owner gate.

SHA-256 values were read independently from the approved wm.zip entries before
copying (archive SHA-256: 2f1c61d0c3c6a2a94f91620e56b93baf5bf77f729e37039d66922d5fe21a68b9).
PNGs are unchanged exports; icon.pxd and logo.pxd are the canonical owner sources.
No redraw, resize, conversion or generated SVG is permitted. PXD checks below
verify only a bounded ZIP container, not Pixelmator opening or layer editability.
"""

import hashlib
import stat
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "custom_components/wolt_monitor/brand"
SOURCES = ROOT / "assets/brand"
APPROVED_PNG_SHA256 = {
    "icon": "7219bc6ea7ad6051bc87bde919e0716dca6cf6fc556d58a90f575502fa111c2a",
    "dark_icon": "7a166a190453d0027d372bc79c0638540f430cd414c54e25b3e7cd5f73fc342c",
    "logo": "5e37f815b47a6929a05cfa8b8905fde4d66cdb1ebf07c1ead3f80aaebe937c8b",
    "dark_logo": "cbebbdfca8eb1dd7336cab8538d7e4a22730ef117b96275f70ea3868fd9edff3",
}
APPROVED_PXD_SHA256 = {
    "icon": "e4e32ffb92490fdac6ee7cf895ed0b6c80d1c4bf0e8c41038889ed9f5df60671",
    "logo": "649dc087f1821070c9c9e38db92439a2a5bcc4310133dfeab965ec4fcc6639c4",
}
# Observed opaque owner-export colors, not the obsolete assistant SVG palette.
TURQUOISE = (0, 218, 245, 255)
LIGHT_NEUTRAL = (236, 236, 236, 255)
DARK_NEUTRAL = (32, 37, 44, 255)


@pytest.mark.parametrize("name", APPROVED_PNG_SHA256)
def test_production_png_matches_exact_approved_owner_export(name):
    assert (
        hashlib.sha256((BRAND / f"{name}.png").read_bytes()).hexdigest()
        == (APPROVED_PNG_SHA256[name])
    )


@pytest.mark.parametrize("name", APPROVED_PNG_SHA256)
def test_owner_png_format_dimensions_and_transparency(name):
    with Image.open(BRAND / f"{name}.png") as image:
        assert image.format == "PNG"
        assert image.mode == "RGBA"
        image.load()
        assert image.size == ((256, 256) if "icon" in name else (1146, 180))
        assert image.width > image.height or "icon" in name
        assert 128 <= image.height <= 256
        for point in (
            (0, 0),
            (image.width - 1, 0),
            (0, image.height - 1),
            (image.width - 1, image.height - 1),
        ):
            assert image.getpixel(point)[3] == 0
        assert image.getchannel("A").getextrema() == (0, 255)
        assert any(0 < alpha < 255 for *_, alpha in image.get_flattened_data())
        if "icon" in name:
            assert image.getpixel((128, 128))[3] == 255
        else:
            assert image.getpixel((500, 10))[3] == 0


@pytest.mark.parametrize("name", APPROVED_PNG_SHA256)
def test_owner_export_has_substantial_opaque_symbol_and_contrasting_neutral(name):
    with Image.open(BRAND / f"{name}.png") as image:
        palette = Counter(image.get_flattened_data())
        neutral = LIGHT_NEUTRAL if name in {"icon", "dark_logo"} else DARK_NEUTRAL
        assert palette[TURQUOISE] > (13_000 if "icon" in name else 20_000)
        assert palette[neutral] > (46_000 if "icon" in name else 36_000)
        if "icon" in name:
            assert image.getpixel((128, 24)) == neutral
            assert image.getpixel((40, 90)) == TURQUOISE


@pytest.mark.parametrize("name", ["icon", "logo"])
def test_dark_variant_is_distinct(name):
    with Image.open(BRAND / f"{name}.png") as light:
        with Image.open(BRAND / f"dark_{name}.png") as dark:
            assert light.size == dark.size
            assert light.tobytes() != dark.tobytes()


def test_header_variants_share_the_exact_foreground_alpha_geometry():
    with Image.open(BRAND / "logo.png") as light:
        with Image.open(BRAND / "dark_logo.png") as dark:
            assert light.mode == dark.mode == "RGBA"
            assert light.getchannel("A").tobytes() == dark.getchannel("A").tobytes()


@pytest.mark.parametrize("name", APPROVED_PXD_SHA256)
def test_canonical_source_is_the_exact_owner_pxd(name):
    source = SOURCES / f"{name}.pxd"
    assert source.is_file()
    assert not source.is_symlink()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == APPROVED_PXD_SHA256[name]


@pytest.mark.parametrize("name", APPROVED_PXD_SHA256)
def test_pxd_is_a_bounded_valid_zip_container_without_unsafe_entry_paths(name):
    source = SOURCES / f"{name}.pxd"
    assert source.read_bytes().startswith(b"PK\x03\x04")
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        assert len(entries) == 4
        assert len({entry.filename for entry in entries}) == len(entries)
        assert sum(entry.file_size for entry in entries) < 2_000_000
        names = {entry.filename for entry in entries}
        assert {"metadata.info", "QuickLook/Thumbnail.webp", "QuickLook/Icon.webp"} < names
        assert len([n for n in names if n.startswith("data/")]) == 1
        for entry in entries:
            path = PurePosixPath(entry.filename)
            assert not path.is_absolute()
            assert ".." not in path.parts
            assert "\\" not in entry.filename
            assert ":" not in entry.filename
            assert not stat.S_ISLNK(entry.external_attr >> 16)
            assert not entry.flag_bits & 1
        assert archive.testzip() is None
    # No recursive unpacking, interpretation or execution of project contents.


@pytest.mark.parametrize("name", APPROVED_PNG_SHA256)
def test_obsolete_assistant_svg_does_not_masquerade_as_owner_source(name):
    assert not (SOURCES / f"{name}.svg").exists()


def test_apple_archive_metadata_is_not_in_brand_directories():
    for directory in (BRAND, SOURCES):
        assert not any(
            path.name == "__MACOSX" or path.name.startswith("._") for path in directory.rglob("*")
        )


def test_readme_always_uses_exact_owner_light_banner():
    readme = (ROOT / "README.md").read_text()
    header = readme.split("<h1", 1)[0]
    relative = "custom_components/wolt_monitor/brand/logo.png"
    assert header.count("<img ") == 1
    assert f'src="{relative}"' in header
    assert 'width="640"' in header and 'alt="Wolt Monitor"' in header
    assert "<picture>" not in header and "<source" not in header
    assert "prefers-color-scheme" not in header and "dark_logo.png" not in header
    assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == APPROVED_PNG_SHA256["logo"]

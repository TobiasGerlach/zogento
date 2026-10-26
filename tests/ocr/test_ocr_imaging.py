from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from immich_book_ocr.imaging import TextGeometry, _rotate_boxes, prepare_spread


def _spread(path: Path, split: int, *, seam: bool = False, columns: bool = False) -> Path:
    """Unequal page widths, irregular line lengths, and optional dark binding."""
    image = Image.new("RGB", (1600, 1050), "#f4f1e9")
    draw = ImageDraw.Draw(image)
    rng = np.random.default_rng(17)
    regions = [(70, split - 58), (split + 58, 1530)]
    if columns:
        regions = [part for left, right in regions for part in (
            (left, (left + right) // 2 - 30),
            ((left + right) // 2 + 30, right),
        )]
    for left, right in regions:
        for y in range(95, 950, 27):
            end = right - int(rng.integers(0, 24))
            x = left
            while x < end - 10:
                word = min(int(rng.integers(20, 65)), end - x)
                # Disconnected glyph-like strokes prevent a fixture made only
                # of long horizontal rules from masking detector failures.
                for letter in range(x, x + word - 5, 8):
                    draw.rectangle((letter, y, letter + 4, y + 10), fill="#242424")
                x += word + int(rng.integers(7, 15))
    if seam:
        draw.polygon(((split - 8, 0), (split + 4, 0),
                      (split + 8, 1050), (split - 4, 1050)), fill="#282828")
    image.save(path)
    return path


@pytest.mark.parametrize("fraction", [0.31, 0.39, 0.62, 0.71])
def test_off_center_gutter_is_detected_per_image(tmp_path: Path, fraction: float) -> None:
    path = _spread(tmp_path / "spread.png", round(1600 * fraction))
    prepared = prepare_spread(path, deskew=False)
    assert abs(prepared.split_fraction - fraction) < 0.025
    assert len(prepared.pages) == 2
    assert prepared.pages[0].width + prepared.pages[1].width == 1600
    assert all(page.mode == "L" for page in prepared.pages)
    assert prepared.confidence > 0.55


def test_dark_binding_beats_column_gaps(tmp_path: Path) -> None:
    path = _spread(tmp_path / "columns.png", 1030, seam=True, columns=True)
    prepared = prepare_spread(path, deskew=False)
    assert abs(prepared.split_fraction - 1030 / 1600) < 0.018
    assert prepared.confidence >= 0.60


def test_manual_split_and_right_to_left_order_preserve_pixels(tmp_path: Path) -> None:
    image = Image.new("RGB", (200, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 59, 99), fill="#777777")
    path = tmp_path / "spread.png"
    image.save(path)
    prepared = prepare_spread(path, split_fraction=0.3, reading_order="rtl", deskew=False)
    assert [page.width for page in prepared.pages] == [140, 60]
    assert prepared.pages[0].getpixel((5, 5)) == 255
    assert prepared.pages[1].getpixel((5, 5)) == 119
    assert prepared.split_fraction == 0.3
    assert prepared.confidence == 1


def test_blank_spread_is_flagged_for_review(tmp_path: Path) -> None:
    path = tmp_path / "blank.png"
    Image.new("RGB", (600, 400), "white").save(path)
    prepared = prepare_spread(path, deskew=False)
    assert prepared.split_fraction == 0.5
    assert prepared.confidence == 0
    assert any("Too little text" in warning for warning in prepared.warnings)


def test_exif_orientation_is_applied_before_manual_split(tmp_path: Path) -> None:
    image = Image.new("RGB", (100, 200), "white")
    exif = Image.Exif()
    exif[274] = 6
    path = tmp_path / "rotated.jpg"
    image.save(path, exif=exif)
    prepared = prepare_spread(path, split_fraction=0.4, deskew=False)
    assert [page.size for page in prepared.pages] == [(80, 100), (120, 100)]


def test_single_page_is_not_split(tmp_path: Path) -> None:
    path = tmp_path / "single.png"
    Image.new("RGB", (200, 300), "white").save(path)
    prepared = prepare_spread(path, single_page=True, deskew=False)
    assert prepared.split_fraction is None
    assert [page.size for page in prepared.pages] == [(200, 300)]


def test_multiframe_images_fail_instead_of_dropping_pages(tmp_path: Path) -> None:
    path = tmp_path / "frames.tiff"
    first = Image.new("RGB", (200, 300), "white")
    first.save(path, save_all=True, append_images=[first.copy()])
    with pytest.raises(ValueError, match="Export all TIFF frames"):
        prepare_spread(path, single_page=True, deskew=False)


def test_sixteen_bit_tiff_preserves_grayscale(tmp_path: Path) -> None:
    path = tmp_path / "sixteen-bit.tiff"
    pixels = np.full((100, 200), 65535, dtype=np.uint16)
    pixels[20:80, 20:80] = 32768
    Image.fromarray(pixels).save(path)
    prepared = prepare_spread(path, single_page=True, deskew=False)
    assert 126 <= prepared.pages[0].getpixel((30, 30)) <= 128
    assert prepared.pages[0].getpixel((100, 30)) == 255


def test_deskew_straightens_text_rows_without_cropping(tmp_path: Path) -> None:
    path = _spread(tmp_path / "upright.png", 800)
    with Image.open(path) as image:
        rotated = image.crop((0, 0, 800, 1050)).rotate(3, expand=True, fillcolor="white")
    path = tmp_path / "skewed.png"
    rotated.save(path)
    unchanged = prepare_spread(path, single_page=True, deskew=False).pages[0]
    corrected = prepare_spread(path, single_page=True, deskew=True).pages[0]
    # Straight text puts more ink into a few rows; equal-sized row bins make
    # this metric independent of the expanded white canvas.
    before = (np.asarray(unchanged) < 100).sum(axis=1)
    after = (np.asarray(corrected) < 100).sum(axis=1)
    assert np.square(after.astype(float)).sum() > np.square(before.astype(float)).sum() * 1.3
    assert corrected.width >= unchanged.width
    assert corrected.height >= unchanged.height


@pytest.mark.parametrize("fraction", [-0.1, 0, 1, 1.1, float("nan")])
def test_invalid_manual_split_is_rejected(tmp_path: Path, fraction: float) -> None:
    with pytest.raises(ValueError, match="strictly between"):
        prepare_spread(tmp_path / "does-not-exist.jpg", split_fraction=fraction)


def _rotated(path: Path, degrees: int, split: int = 800) -> Path:
    spread = Image.open(_spread(path.with_name("upright.png"), split))
    spread.rotate(degrees, expand=True, fillcolor="#f4f1e9").save(path)
    return path


def _probe(angle: float, boxes=None, support: int = 40):
    """Stand in for the OCR pass, which needs real printed text to measure.

    With no boxes the engine reported only an angle, as Tesseract's OSD does,
    and the pixel gutter detector stays in charge.
    """
    geometry = TextGeometry(angle, list(boxes or []), support)
    return lambda image: geometry


def _page_lines(left: float, right: float, count: int = 20, top: float = 0.1):
    """Boxes for one column of text spanning `left`..`right`."""
    step = (0.9 - top) / count
    return [(left, top + i * step, right, top + i * step + step * 0.6) for i in range(count)]


@pytest.mark.parametrize("degrees,expected", [(90, 270), (180, 180), (270, 90)])
def test_auto_rotation_restores_a_turned_spread_before_splitting(
    tmp_path: Path, degrees: int, expected: int
) -> None:
    path = _rotated(tmp_path / "turned.png", degrees)
    # A page turned counter-clockwise by `degrees` reads at -degrees.
    prepared = prepare_spread(path, rotation="auto", angle_probe=_probe(-expected), deskew=False)
    assert prepared.rotation == expected
    assert len(prepared.pages) == 2
    # Both pages come back portrait, as the upright spread's halves are.
    for page in prepared.pages:
        assert page.height > page.width
    assert 0.35 < prepared.split_fraction < 0.65


def test_measured_skew_is_applied_instead_of_estimating_per_page(tmp_path: Path) -> None:
    path = _spread(tmp_path / "flat.png", 800)
    prepared = prepare_spread(path, rotation="auto", angle_probe=_probe(-96.0), deskew=True)
    assert prepared.rotation == 90
    # 96 degrees of correction is a quarter turn plus six degrees of skew.
    upright = prepare_spread(path, rotation="auto", angle_probe=_probe(-90.0), deskew=True)
    assert prepared.pages[0].size != upright.pages[0].size


def test_probe_that_finds_no_text_falls_back_without_rotating(tmp_path: Path) -> None:
    path = _spread(tmp_path / "flat.png", 800)
    prepared = prepare_spread(path, rotation="auto", angle_probe=lambda image: None, deskew=False)
    assert prepared.rotation == 0
    assert len(prepared.pages) == 2


def test_manual_rotation_overrides_detection_and_is_validated(tmp_path: Path) -> None:
    path = _rotated(tmp_path / "turned.png", 90)
    def probe(image):
        raise AssertionError("a pinned rotation must not be measured")
    prepared = prepare_spread(path, rotation=270, angle_probe=probe, deskew=False)
    assert prepared.rotation == 270
    assert all(page.height > page.width for page in prepared.pages)
    with pytest.raises(ValueError):
        prepare_spread(path, rotation=45)


def test_upright_spread_is_left_alone_by_auto_rotation(tmp_path: Path) -> None:
    path = _spread(tmp_path / "flat.png", 800)
    prepared = prepare_spread(path, rotation="auto", angle_probe=_probe(0.0), deskew=False)
    assert prepared.rotation == 0
    assert abs(prepared.split_fraction - 800 / 1600) < 0.03


def _on_dark_background(path: Path, split: int = 800, margin: int = 260) -> Path:
    """A spread photographed on a desk: the frame holds more than the book."""
    spread = Image.open(_spread(path.with_name("bare.png"), split))
    frame = Image.new("RGB", (spread.width + margin * 2, spread.height + margin * 2), "#121212")
    frame.paste(spread, (margin, margin))
    frame.save(path)
    return path


def test_dark_surround_is_trimmed_so_the_book_edge_is_not_taken_for_the_binding(tmp_path: Path) -> None:
    path = _on_dark_background(tmp_path / "held.png")
    prepared = prepare_spread(path, deskew=False)
    assert prepared.cropped
    # Without the crop the split lands on the book's outer edge instead.
    assert 0.35 < prepared.split_fraction < 0.65
    assert len(prepared.pages) == 2


def test_cropping_can_be_turned_off(tmp_path: Path) -> None:
    path = _on_dark_background(tmp_path / "held.png")
    assert prepare_spread(path, deskew=False, crop_paper=False).cropped is False


def test_a_dark_page_is_never_mistaken_for_background(tmp_path: Path) -> None:
    """A plate or tinted page is darker than its facing page but is not a surround."""
    image = Image.new("RGB", (1600, 1050), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 779, 1049), fill="#6f6f6f")
    path = tmp_path / "plate.png"
    image.save(path)
    prepared = prepare_spread(path, split_fraction=0.5, deskew=False)
    assert prepared.cropped is False
    assert [page.width for page in prepared.pages] == [800, 800]
    assert prepared.pages[0].getpixel((5, 5)) == 111


def test_a_full_frame_scan_is_left_uncropped(tmp_path: Path) -> None:
    path = _spread(tmp_path / "flat.png", 800)
    prepared = prepare_spread(path, deskew=False)
    assert prepared.cropped is False
    assert sum(page.width for page in prepared.pages) == 1600


def test_binding_is_found_from_text_blocks_without_a_dark_seam(tmp_path: Path) -> None:
    """The case the pixel detector scores badly: a flat, evenly lit gutter."""
    path = _spread(tmp_path / "flat.png", 800)
    boxes = _page_lines(0.08, 0.46) + _page_lines(0.54, 0.92)
    prepared = prepare_spread(path, rotation="auto", angle_probe=_probe(0.0, boxes), deskew=False)
    assert abs(prepared.split_fraction - 0.5) < 0.02
    assert prepared.confidence > 0.85


def test_a_column_gap_does_not_outrank_the_binding(tmp_path: Path) -> None:
    path = _spread(tmp_path / "flat.png", 800)
    # Two columns per page: three channels, only the middle one is the binding.
    boxes = (_page_lines(0.04, 0.22) + _page_lines(0.26, 0.46)
             + _page_lines(0.54, 0.74) + _page_lines(0.78, 0.96))
    prepared = prepare_spread(path, rotation="auto", angle_probe=_probe(0.0, boxes), deskew=False)
    assert abs(prepared.split_fraction - 0.5) < 0.02


def test_text_on_only_one_page_is_reported_rather_than_split_confidently(tmp_path: Path) -> None:
    path = _spread(tmp_path / "flat.png", 800)
    prepared = prepare_spread(path, rotation="auto",
                              angle_probe=_probe(0.0, _page_lines(0.08, 0.46)), deskew=False)
    assert prepared.confidence == 0.0
    assert any("Only one page carries text" in note for note in prepared.warnings)


def test_lopsided_text_lowers_confidence_without_moving_the_split(tmp_path: Path) -> None:
    path = _spread(tmp_path / "flat.png", 800)
    even = _page_lines(0.08, 0.46, count=20) + _page_lines(0.54, 0.92, count=20)
    lopsided = _page_lines(0.08, 0.46, count=20) + _page_lines(0.54, 0.92, count=3)
    balanced = prepare_spread(path, rotation="auto", angle_probe=_probe(0.0, even), deskew=False)
    skewed = prepare_spread(path, rotation="auto", angle_probe=_probe(0.0, lopsided), deskew=False)
    assert abs(balanced.split_fraction - skewed.split_fraction) < 0.02
    assert skewed.confidence < balanced.confidence


def test_engine_without_layout_falls_back_to_the_pixel_detector(tmp_path: Path) -> None:
    """Tesseract's OSD reports a quarter turn and nothing about the layout."""
    path = _spread(tmp_path / "flat.png", 640, seam=True)
    prepared = prepare_spread(path, rotation="auto", angle_probe=_probe(0.0), deskew=False)
    assert abs(prepared.split_fraction - 640 / 1600) < 0.03


@pytest.mark.parametrize("degrees", [90, 180, 270])
def test_line_boxes_survive_rotation_and_cropping(tmp_path: Path, degrees: int) -> None:
    """The layout is measured before the scan is squared up, so the boxes have
    to be carried through the same turn and crop as the pixels."""
    path = _on_dark_background(tmp_path / "held.png", split=800)
    with Image.open(path) as opened:
        turned = opened.rotate(-degrees, expand=True, fillcolor="#121212")
    turned.save(path)
    boxes = _page_lines(0.08, 0.46) + _page_lines(0.54, 0.92)
    # Boxes are expressed against the frame as photographed, like the real probe.
    frame = Image.open(path)
    inset_x = 260 / (frame.width if degrees == 180 else frame.height)
    inset_y = 260 / (frame.height if degrees == 180 else frame.width)
    placed = [(0.5 + (x0 - 0.5) * (1 - 2 * inset_x), 0.5 + (y0 - 0.5) * (1 - 2 * inset_y),
               0.5 + (x1 - 0.5) * (1 - 2 * inset_x), 0.5 + (y1 - 0.5) * (1 - 2 * inset_y))
              for x0, y0, x1, y1 in boxes]
    turned_boxes = _rotate_boxes(placed, -degrees % 360)
    prepared = prepare_spread(path, rotation="auto",
                              angle_probe=_probe(-(degrees % 360) if degrees != 180 else 180, turned_boxes),
                              deskew=False)
    assert prepared.rotation == degrees % 360
    assert 0.35 < prepared.split_fraction < 0.65

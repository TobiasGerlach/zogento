"""Apple Vision OCR engine. No network and no scans leave the machine."""

import sys

import pytest
from PIL import Image, ImageDraw, ImageFont

from immich_book_ocr import imaging, vision

SENTENCES = [
    "The first page tells a story.",
    "A careful reader follows the words.",
    "Every chapter opens another door.",
    "The library holds many old books.",
]


def page_image(path, rotate=0):
    image = Image.new("RGB", (1100, 1450), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=34)
    for row in range(14):
        draw.text((90, 130 + row * 75), SENTENCES[row % len(SENTENCES)], font=font, fill="black")
    if rotate:
        image = image.rotate(rotate, expand=True, fillcolor="white")
    image.save(path)
    return path


requires_vision = pytest.mark.skipif(
    sys.platform != "darwin" or not vision.available("eng"),
    reason="Apple Vision is unavailable on this machine",
)


def test_language_codes_map_to_vision_tags():
    assert vision.recognition_languages("deu+eng") == ["de-DE", "en-US"]
    assert vision.recognition_languages("eng") == ["en-US"]


def test_unsupported_language_is_refused_by_name():
    # Fraktur has a Tesseract model but no Vision counterpart.
    with pytest.raises(vision.VisionUnavailable, match="deu_latf"):
        vision.recognition_languages("deu_latf")
    assert vision.available("deu_latf") is False


def test_invalid_language_is_rejected_without_calling_vision():
    with pytest.raises(ValueError):
        vision.recognition_languages("../eng")
    assert vision.available("eng;ls") is False


def test_configuration_signature_changes_with_the_language_set():
    assert vision.configuration_signature("eng") != vision.configuration_signature("deu+eng")
    assert vision.configuration_signature("eng") == vision.configuration_signature("eng")


@requires_vision
def test_reads_an_upright_page_and_writes_tesseract_shaped_artifacts(tmp_path):
    result = vision.recognize_page(page_image(tmp_path / "page.png"), tmp_path / "out", language="eng")
    assert "The first page tells a story." in result.text
    assert result.words > 20 and result.confidence > 80
    assert result.warnings == []
    assert (tmp_path / "out.txt").read_text(encoding="utf-8").strip() == result.text
    header, *rows = (tmp_path / "out.tsv").read_text(encoding="utf-8").strip().splitlines()
    assert header.split("\t")[:6] == ["level", "page_num", "block_num", "par_num", "line_num", "word_num"]
    assert len(rows) == result.words
    assert all(len(row.split("\t")) == 12 for row in rows)


@requires_vision
def test_reads_english_and_german_together_with_umlauts_and_sharp_s(tmp_path):
    if not vision.available("eng+deu"):
        pytest.skip("Apple Vision does not support both English and German")
    lines = [
        "The first page tells a story.",
        "A careful reader follows the words.",
        "Die Bücher erzählen eine schöne Geschichte.",
        "Große Häuser stehen an der Straße.",
        "We preserve English and German words.",
        "Über den grünen Hügel führt ein schmaler Weg.",
    ]
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=44)
    for row, line in enumerate(lines):
        draw.text((70, 80 + row * 110), line, font=font, fill="black")
    image.save(tmp_path / "bilingual.png")

    result = vision.recognize_page(tmp_path / "bilingual.png", tmp_path / "bilingual", language="eng+deu")

    for line in lines:
        assert line in result.text
    assert (tmp_path / "bilingual.txt").read_text(encoding="utf-8").strip() == result.text


@requires_vision
@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_pages_are_read_without_any_preprocessing(tmp_path, rotation):
    """The reason for this engine: handheld book photos arrive sideways."""
    image = page_image(tmp_path / f"page{rotation}.png", rotate=rotation)
    result = vision.recognize_page(image, tmp_path / f"out{rotation}", language="eng")
    assert "The first page tells a story." in result.text
    assert result.text.index("The first page") < result.text.index("A careful reader")


@requires_vision
def test_blank_page_reports_no_text_instead_of_failing(tmp_path):
    Image.new("RGB", (800, 1000), "white").save(tmp_path / "blank.png")
    result = vision.recognize_page(tmp_path / "blank.png", tmp_path / "blank", language="eng")
    assert result.text == ""
    assert result.words == 0 and result.confidence is None
    assert "No text recognized" in result.warnings[0]


@requires_vision
def test_missing_image_raises_instead_of_returning_empty_text(tmp_path):
    with pytest.raises(OSError):
        vision.recognize_page(tmp_path / "absent.png", tmp_path / "out", language="eng")


@requires_vision
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_measured_angle_is_the_turn_that_makes_a_page_upright(tmp_path, rotation):
    """The angle is what tells the splitter which way is up, so the test that
    matters is that acting on it actually leaves the page square."""
    image = page_image(tmp_path / f"page{rotation}.png", rotate=rotation)
    measured = vision.text_angle(image, "eng")
    assert measured is not None
    angle, lines = measured
    assert lines >= 8

    correction = round(-angle / 90) * 90 % 360
    straightened = tmp_path / f"fixed{rotation}.png"
    with Image.open(image) as opened:
        imaging._apply_rotation(opened, correction).save(straightened)
    settled, _ = vision.text_angle(straightened, "eng")
    assert abs((settled + 180) % 360 - 180) < 5, f"still {settled}° off after turning {correction}°"
    assert "The first page tells a story." in vision.recognize_page(
        straightened, tmp_path / f"out{rotation}", language="eng").text


@requires_vision
def test_each_quarter_turn_is_told_apart_from_the_others(tmp_path):
    corrections = set()
    for rotation in (0, 90, 180, 270):
        angle, _ = vision.text_angle(page_image(tmp_path / f"p{rotation}.png", rotate=rotation), "eng")
        corrections.add(round(-angle / 90) * 90 % 360)
    assert corrections == {0, 90, 180, 270}


@requires_vision
def test_text_angle_reports_nothing_for_a_blank_page(tmp_path):
    Image.new("RGB", (900, 1200), "white").save(tmp_path / "blank.png")
    assert vision.text_angle(tmp_path / "blank.png", "eng") is None

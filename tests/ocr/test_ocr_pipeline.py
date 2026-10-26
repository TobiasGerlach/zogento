import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from immich_book_ocr import pipeline
from immich_book_ocr.imaging import PreparedSpread
from immich_book_ocr.ocr import OCRResult, language_codes
from immich_book_ocr.pipeline import PipelineOptions, run_local


def make_scan(path, word):
    Image.new("RGB", (300, 250), "white").save(path)
    path.with_suffix(".label").write_text(word)


@pytest.fixture
def fake_ocr(monkeypatch):
    monkeypatch.setattr(pipeline, "ensure_models", lambda *args: None)
    monkeypatch.setattr(pipeline, "configuration_signature", lambda *args: "test-model")
    # These tests cover pipeline mechanics with a stubbed recognizer, so pin the
    # Tesseract path rather than letting "auto" pick Vision on a macOS machine.
    monkeypatch.setattr(pipeline.vision, "available", lambda language: False)
    calls = []

    def prepare(path, **kwargs):
        image = Image.new("L", (100, 100), int(path.stem[-1]) * 20)
        return PreparedSpread([image], 0.4, 0.9, [], image.convert("RGB"))

    def recognize(image, base, **kwargs):
        calls.append(image)
        with Image.open(image) as page:
            text = f"page {page.getpixel((0, 0))}"
        base.with_suffix(".txt").write_text(text)
        base.with_suffix(".tsv").write_text("level\tconf\ttext\n")
        return OCRResult(text, 96, 2, 0, [])

    monkeypatch.setattr(pipeline, "prepare_spread", prepare)
    monkeypatch.setattr(pipeline, "recognize_page", recognize)
    return calls


def test_cache_and_changed_split_only_reruns_affected_scan(tmp_path, fake_ocr):
    scans = tmp_path / "scans"
    scans.mkdir()
    for number in (1, 2):
        make_scan(scans / f"scan{number}.png", str(number))
    output = tmp_path / "out"
    first = run_local(scans, output, options=PipelineOptions(models="system"))
    assert first["complete"] and first["pages"] == 2
    original_text = (output / "book.txt").read_text()
    run_local(scans, output, options=PipelineOptions(models="system"))
    assert len(fake_ocr) == 2
    assert (output / "book.txt").read_text() == original_text
    run_local(scans, output, options=PipelineOptions(models="system", split_overrides={"scan2.png": 0.6}))
    assert len(fake_ocr) == 3


def test_reordered_sources_do_not_reuse_overwritten_numbered_text(tmp_path, fake_ocr):
    output = tmp_path / "out"
    scans = output / "originals"
    scans.mkdir(parents=True)
    for number in (1, 2):
        make_scan(scans / f"scan{number}.png", str(number))
    manifest = {"title": "Book", "sources": [{"source": f"scan{n}.png", "file": f"scan{n}.png"} for n in (1, 2)]}
    (output / "sources.json").write_text(json.dumps(manifest))
    run_local(scans, output, options=PipelineOptions(models="system"))
    manifest["sources"].reverse()
    (output / "sources.json").write_text(json.dumps(manifest))
    run_local(scans, output, options=PipelineOptions(models="system"))
    text = (output / "book.txt").read_text()
    assert text.index("page 40") < text.index("page 20")
    assert len(fake_ocr) == 4


def test_failed_scan_marks_output_incomplete_and_continues(tmp_path, fake_ocr, monkeypatch):
    scans = tmp_path / "scans"
    scans.mkdir()
    for number in (1, 2):
        make_scan(scans / f"scan{number}.png", str(number))
    normal = pipeline.prepare_spread

    def fail_one(path, **kwargs):
        if path.stem == "scan1":
            raise ValueError("Unreadable image")
        return normal(path, **kwargs)

    monkeypatch.setattr(pipeline, "prepare_spread", fail_one)
    output = tmp_path / "out"
    report = run_local(scans, output, options=PipelineOptions(models="system"))
    assert not report["complete"]
    assert report["failed_scans"] == 1
    assert report["pages"] == 1
    assert "OCR INCOMPLETE: scan1.png" in (output / "book.txt").read_text()
    assert "page 40" in (output / "book.txt").read_text()


def test_interrupted_changed_settings_cannot_corrupt_committed_cache(tmp_path, fake_ocr, monkeypatch):
    scans = tmp_path / "scans"
    scans.mkdir()
    make_scan(scans / "scan1.png", "first")
    output = tmp_path / "out"
    run_local(scans, output, options=PipelineOptions(models="system", psm=3))
    original = (output / "book.txt").read_text()

    def interrupted(image, base, **kwargs):
        base.with_suffix(".txt").write_text("interrupted output from different settings")
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "recognize_page", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_local(scans, output, options=PipelineOptions(models="system", psm=6))
    report = run_local(scans, output, options=PipelineOptions(models="system", psm=3))
    assert report["complete"]
    assert (output / "book.txt").read_text() == original


@pytest.mark.parametrize("value", ["../eng", "eng+", "eng;ls", "-eng", "/eng"])
def test_language_codes_reject_paths_and_invalid_options(value):
    with pytest.raises(ValueError):
        language_codes(value)


@pytest.mark.parametrize("models", ["system", "best"])
def test_real_tesseract_reads_shifted_two_page_spread(tmp_path, models):
    binary = shutil.which("tesseract")
    if not binary:
        pytest.skip("Tesseract is not installed")
    langs = subprocess.run([binary, "--list-langs"], capture_output=True, text=True).stdout
    if "eng" not in langs.splitlines():
        pytest.skip("English system model is not installed")
    if models == "best" and not (Path.cwd() / ".models/best/eng.traineddata").exists():
        pytest.skip("English tessdata_best model is not cached; tests do not download models")
    image = Image.new("RGB", (2200, 1450), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=34)
    left = ["The first page tells a story.", "A careful reader follows the words.", "Every chapter opens another door.", "The library holds many old books."]
    right = ["The second page continues here.", "Bright morning light fills the room.", "We preserve the printed words.", "The final sentence closes the book."]
    for x, lines in ((90, left), (1080, right)):
        for row in range(14):
            draw.text((x, 130 + row * 75), lines[row % len(lines)], font=font, fill="black")
    draw.line([(970, 80), (970, 1360)], fill="#333333", width=7)
    scans = tmp_path / "scans"
    scans.mkdir()
    image.save(scans / "scan1.png")
    output = tmp_path / "out"
    result = run_local(scans, output, options=PipelineOptions(language="eng", models=models, engine="tesseract"))
    assert result["complete"]
    assert result["pages"] == 2
    assert abs(result["entries"][0]["split_fraction"] - 970 / 2200) < 0.03
    text = (output / "book.txt").read_text()
    assert "The first page tells a story." in text
    assert "The second page continues here." in text
    assert text.index("The first page") < text.index("The second page")
    assert "\f" in text


def test_auto_engine_prefers_vision_and_falls_back_to_tesseract(tmp_path, monkeypatch, fake_ocr):
    monkeypatch.setattr(pipeline.vision, "available", lambda language: True)
    monkeypatch.setattr(pipeline.vision, "configuration_signature", lambda language: "vision-test")
    options = PipelineOptions(models="system")
    engine, tessdata, signature = pipeline._prepare_engine(options, lambda _: None)
    assert (engine, tessdata, signature) == ("vision", None, "vision-test")
    # The resolved name is written back so cache keys and the report record it.
    assert options.engine == "vision"

    monkeypatch.setattr(pipeline.vision, "available", lambda language: False)
    fallback = PipelineOptions(models="system")
    assert pipeline._prepare_engine(fallback, lambda _: None)[0] == "tesseract"
    assert fallback.engine == "tesseract"


def test_explicit_tesseract_never_consults_vision(tmp_path, monkeypatch, fake_ocr):
    def fail(language):
        raise AssertionError("Vision must not be probed when Tesseract was requested")

    monkeypatch.setattr(pipeline.vision, "available", fail)
    options = PipelineOptions(models="system", engine="tesseract")
    assert pipeline._prepare_engine(options, lambda _: None)[0] == "tesseract"


def test_report_records_the_engine_and_worker_count(tmp_path, fake_ocr):
    scans = tmp_path / "scans"
    scans.mkdir()
    make_scan(scans / "scan1.png", "1")
    report = run_local(scans, tmp_path / "out", options=PipelineOptions(models="system", workers=3))
    assert report["engine"] == "tesseract"
    assert report["workers"] == 3


def test_parallel_scans_are_assembled_in_source_order(tmp_path, monkeypatch, fake_ocr):
    """Scans finish out of order under concurrency; book.txt must not."""
    import time

    scans = tmp_path / "scans"
    scans.mkdir()
    for number in (1, 2, 3):
        make_scan(scans / f"scan{number}.png", str(number))
    finished = []
    original = pipeline.recognize_page

    def slow(image, base, **kwargs):
        with Image.open(image) as page:
            value = page.getpixel((0, 0))
        # Earlier scans are slowest, so completion order reverses source order.
        time.sleep(0.25 if value == 20 else 0.15 if value == 40 else 0.0)
        finished.append(value)
        return original(image, base, **kwargs)

    monkeypatch.setattr(pipeline, "recognize_page", slow)
    report = run_local(scans, tmp_path / "out", options=PipelineOptions(models="system", workers=3))
    assert finished == [60, 40, 20], "the test did not actually run the scans concurrently"
    assert [entry["source"] for entry in report["entries"]] == ["scan1.png", "scan2.png", "scan3.png"]
    body = (tmp_path / "out" / "book.txt").read_text()
    assert body.index("page 20") < body.index("page 40") < body.index("page 60")


def test_worker_count_does_not_invalidate_cached_pages(tmp_path, fake_ocr):
    scans = tmp_path / "scans"
    scans.mkdir()
    for number in (1, 2):
        make_scan(scans / f"scan{number}.png", str(number))
    output = tmp_path / "out"
    run_local(scans, output, options=PipelineOptions(models="system", workers=1))
    assert len(fake_ocr) == 2
    run_local(scans, output, options=PipelineOptions(models="system", workers=4))
    assert len(fake_ocr) == 2, "changing the thread count must not re-run OCR"


@pytest.mark.parametrize("options", [{"engine": "sideways"}, {"workers": -1}, {"workers": True}, {"workers": 999}])
def test_invalid_engine_and_worker_settings_are_rejected(options):
    with pytest.raises(ValueError):
        PipelineOptions(**options).validate()


def test_rotation_is_measured_recorded_and_applied(tmp_path, monkeypatch, fake_ocr):
    seen = {}
    normal = pipeline.prepare_spread

    def capture(path, **kwargs):
        seen.update(kwargs)
        spread = normal(path, **{k: v for k, v in kwargs.items() if k not in {"rotation", "angle_probe"}})
        spread.rotation = 90
        return spread

    monkeypatch.setattr(pipeline, "prepare_spread", capture)
    scans = tmp_path / "scans"
    scans.mkdir()
    make_scan(scans / "scan1.png", "1")
    report = run_local(scans, tmp_path / "out", options=PipelineOptions(models="system"))
    assert seen["rotation"] == "auto"
    assert seen["angle_probe"] is not None
    assert report["entries"][0]["rotation"] == 90


def test_pinned_rotation_skips_the_measuring_pass(tmp_path, monkeypatch, fake_ocr):
    seen = {}
    normal = pipeline.prepare_spread

    def capture(path, **kwargs):
        seen.update(kwargs)
        return normal(path, **{k: v for k, v in kwargs.items() if k not in {"rotation", "angle_probe"}})

    monkeypatch.setattr(pipeline, "prepare_spread", capture)
    scans = tmp_path / "scans"
    scans.mkdir()
    make_scan(scans / "scan1.png", "1")
    run_local(scans, tmp_path / "out", options=PipelineOptions(models="system", rotation="180"))
    assert seen["rotation"] == 180
    assert seen["angle_probe"] is None, "no OCR pass is needed when the rotation is pinned"


@pytest.mark.parametrize("value", ["sideways", "45", "-90"])
def test_invalid_rotation_settings_are_rejected(value):
    with pytest.raises(ValueError):
        PipelineOptions(rotation=value).validate()

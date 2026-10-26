"""On-device OCR through Apple's Vision framework (the engine iOS/macOS use).

Vision recognizes rotated and perspective-distorted text without preprocessing,
which matters for handheld book photos that arrive sideways or skewed. It runs
entirely on the local Neural Engine, so no scan leaves the computer.
"""
from __future__ import annotations

import math
import statistics
import threading
from pathlib import Path

from .imaging import TextGeometry
from .ocr import OCRResult, language_codes

# Tesseract language codes mapped onto the BCP-47 tags Vision expects.
_LANGUAGES = {
    "eng": "en-US", "deu": "de-DE", "fra": "fr-FR", "ita": "it-IT", "spa": "es-ES",
    "por": "pt-BR", "nld": "nl-NL", "dan": "da-DK", "swe": "sv-SE", "nor": "nb-NO",
    "pol": "pl-PL", "rus": "ru-RU", "ukr": "uk-UA", "ces": "cs-CZ", "tur": "tr-TR",
    "ron": "ro-RO", "ind": "id-ID", "msa": "ms-MY", "tha": "th-TH", "vie": "vi-VT",
    "ara": "ar-SA", "kor": "ko-KR", "jpn": "ja-JP",
    "chi_sim": "zh-Hans", "chi_tra": "zh-Hant",
}

_lock = threading.Lock()
_modules: tuple | None = None


class VisionUnavailable(RuntimeError):
    """Vision cannot be used on this machine or for these languages."""


def _load() -> tuple:
    """Import the PyObjC bridges once; they are expensive to resolve."""
    global _modules
    with _lock:
        if _modules is None:
            try:
                import Quartz
                import Vision
                from Foundation import NSURL
            except ImportError as error:  # pragma: no cover - platform dependent
                raise VisionUnavailable(
                    "Apple Vision needs the optional pyobjc packages. Install with: "
                    "pip install pyobjc-framework-Vision pyobjc-framework-Quartz"
                ) from error
            _modules = (Vision, Quartz, NSURL)
    return _modules


def supported_languages() -> list[str]:
    Vision, _, _ = _load()
    request = Vision.VNRecognizeTextRequest.alloc().init()
    tags, error = request.supportedRecognitionLanguagesAndReturnError_(None)
    if error is not None or not tags:  # pragma: no cover - platform dependent
        raise VisionUnavailable("Vision did not report any recognition languages.")
    return list(tags)


def recognition_languages(language: str) -> list[str]:
    """Translate 'deu+eng' into Vision tags, rejecting codes Vision cannot read."""
    available = set(supported_languages())
    tags, unsupported = [], []
    for code in language_codes(language):
        tag = _LANGUAGES.get(code)
        if tag is not None and tag in available:
            tags.append(tag)
        else:
            unsupported.append(code)
    if unsupported:
        raise VisionUnavailable(
            "Apple Vision cannot read: " + ", ".join(unsupported)
            + ". Choose another language or switch the OCR engine to Tesseract."
        )
    return tags


def available(language: str) -> bool:
    try:
        recognition_languages(language)
        return True
    except (VisionUnavailable, ValueError):
        return False


def configuration_signature(language: str) -> str:
    """Invalidate cached pages when the engine or its language set changes."""
    Vision, _, _ = _load()
    revision = Vision.VNRecognizeTextRequest.currentRevision()
    return "vision-" + str(revision) + "-" + "+".join(recognition_languages(language))


def _observations(path: Path, tags: list[str]) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    Vision, Quartz, NSURL = _load()
    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
    if source is None:
        raise OSError(f"Vision could not open the page image: {path.name}")
    image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if image is None:
        raise OSError(f"Vision could not decode the page image: {path.name}")
    lines: list[tuple[str, float, tuple[float, float, float, float]]] = []
    failure: list[str] = []

    def collect(request, error):
        if error is not None:  # pragma: no cover - platform dependent
            failure.append(str(error))
            return
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            candidate = candidates[0]
            text = candidate.string()
            if not text or not text.strip():
                continue
            box = observation.boundingBox()
            lines.append((text, float(candidate.confidence()),
                          (box.origin.x, box.origin.y, box.size.width, box.size.height)))

    request = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(collect)
    request.setRecognitionLevel_(0)  # VNRequestTextRecognitionLevelAccurate
    request.setRecognitionLanguages_(tags)
    request.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
    ok, error = handler.performRequests_error_([request], None)
    if failure or (not ok and error is not None):
        raise OSError("Apple Vision failed to read the page: " + (failure[0] if failure else str(error)))
    return lines


def recognize_page(image: Path, output_base: Path, *, language: str) -> OCRResult:
    """Recognize one page image, writing the same .txt/.tsv artifacts as Tesseract.

    Vision's own line ordering is preserved: it already accounts for rotated and
    skewed text, which re-sorting by coordinate would undo.
    """
    tags = recognition_languages(language)
    output_base.parent.mkdir(parents=True, exist_ok=True)
    lines = _observations(Path(image), tags)
    text = "\n".join(line for line, _, _ in lines).strip()
    output_base.with_suffix(".txt").write_text(text + "\n" if text else "", encoding="utf-8")

    # Vision scores whole lines, not words. Each word inherits its line's score and
    # bounding box so the TSV stays in Tesseract's shape for the review tooling.
    # Written by hand rather than through csv: words come from str.split(), so they
    # contain no delimiter, and Tesseract's TSV is likewise unquoted and unescaped.
    confidences: list[float] = []
    rows = ["\t".join(["level", "page_num", "block_num", "par_num", "line_num", "word_num",
                       "left", "top", "width", "height", "conf", "text"])]
    for line_number, (line, score, (x, y, width, height)) in enumerate(lines, 1):
        percent = round(score * 100, 2)
        for word_number, word in enumerate(line.split(), 1):
            confidences.append(percent)
            rows.append("\t".join(str(value) for value in (
                5, 1, 1, 1, line_number, word_number,
                round(x, 6), round(1 - y - height, 6), round(width, 6), round(height, 6),
                percent, word)))
    output_base.with_suffix(".tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    mean = round(sum(confidences) / len(confidences), 2) if confidences else None
    low = sum(score < 60 for score in confidences)
    warnings = []
    if not text:
        warnings.append("No text recognized; inspect this page for missing text or a blank/illustrated page.")
    elif mean is None or mean < 80 or low > len(confidences) * 0.15:
        warnings.append("OCR confidence is low; proofread this page against the scan.")
    return OCRResult(text, mean, len(confidences), low, warnings)


def text_geometry(image: Path, language: str) -> TextGeometry | None:
    """Measure how the printed text sits on the page, from a single read.

    Vision reads rotated text and hands back the corners of every line it found,
    so one pass yields both the page's tilt — including which way up it is,
    since text is only read in its true reading direction — and the layout of
    the text blocks, which is what locates the binding. Returns None when too
    little text was found to be worth trusting.
    """
    Vision, Quartz, NSURL = _load()
    path = Path(image)
    source = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(str(path)), None)
    if source is None:
        raise OSError(f"Vision could not open the page image: {path.name}")
    decoded = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
    if decoded is None:
        raise OSError(f"Vision could not decode the page image: {path.name}")
    width = Quartz.CGImageGetWidth(decoded)
    height = Quartz.CGImageGetHeight(decoded)
    angles: list[float] = []
    boxes: list[tuple[float, float, float, float]] = []

    def collect(request, error):
        if error is not None:  # pragma: no cover - platform dependent
            return
        for observation in request.results() or []:
            if not observation.topCandidates_(1):
                continue
            left, right = observation.topLeft(), observation.topRight()
            # Normalised coordinates: scale back to pixels before taking the angle.
            angles.append(math.degrees(math.atan2((right.y - left.y) * height,
                                                  (right.x - left.x) * width)))
            box = observation.boundingBox()
            # Vision measures from the bottom left; flip to a top-left origin.
            boxes.append((box.origin.x, 1 - box.origin.y - box.size.height,
                          box.origin.x + box.size.width, 1 - box.origin.y))

    request = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(collect)
    # Only the accurate recognizer finds text on a rotated page; the fast level
    # and the text-rectangle detector both report every line as horizontal.
    request.setRecognitionLevel_(0)
    request.setRecognitionLanguages_(recognition_languages(language))
    request.setUsesLanguageCorrection_(False)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(decoded, None)
    handler.performRequests_error_([request], None)
    if len(angles) < 8:
        return None
    return TextGeometry(float(statistics.median(angles)), boxes, len(angles))


def text_angle(image: Path, language: str) -> tuple[float, int] | None:
    """The page tilt alone, kept for callers that do not need the layout."""
    geometry = text_geometry(image, language)
    return None if geometry is None else (geometry.angle, len(geometry.lines))

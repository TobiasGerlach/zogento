"""Prepare scanned book spreads without assuming that the binding is centered.

Detection uses a reduced image; the returned page images retain the original
resolution. Only the detection mask is binarized: OCR receives grayscale images
so that punctuation, accents, and faint printing survive preprocessing.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps


@dataclass
class TextGeometry:
    """What an OCR pass can tell us about how a page is laid out.

    ``angle`` is the printed lines' tilt in degrees counter-clockwise. ``lines``
    are their boxes as (left, top, right, bottom) fractions of the image, with
    the origin at the top left — the ordinary image convention, not Vision's.
    An engine that reports only a coarse orientation leaves ``lines`` empty but
    still sets ``support``, the weight of evidence behind the angle.
    """
    angle: float
    lines: list[tuple[float, float, float, float]]
    support: int = 0

    def __post_init__(self) -> None:
        if not self.support:
            self.support = len(self.lines)


@dataclass
class PreparedSpread:
    pages: list[Image.Image]
    split_fraction: float | None
    confidence: float
    warnings: list[str]
    preview: Image.Image
    rotation: int = 0
    orientation_confidence: float = 1.0
    cropped: bool = False


@dataclass
class _GutterCandidate:
    position: float
    score: float
    seam: float
    support: float
    width: int


def _load_image(path: Path) -> tuple[Image.Image, list[str]]:
    if path.suffix.lower() in {".heic", ".heif", ".hif"}:
        try:
            from pillow_heif import register_heif_opener
        except ImportError as exc:
            raise ValueError(
                "HEIC/HEIF images require the optional pillow-heif package."
            ) from exc
        register_heif_opener()
    warnings: list[str] = []
    with Image.open(path) as opened:
        if getattr(opened, "n_frames", 1) > 1:
            raise ValueError(
                "This image contains multiple frames. Export all TIFF frames "
                "as separate images first so that no scanned pages are omitted."
            )
        oriented = ImageOps.exif_transpose(opened)
        if oriented.mode.startswith("I;16") or oriented.mode == "I":
            # Pillow's direct I;16 -> RGB conversion clips values above 255.
            # Scanner TIFFs commonly contain 16-bit grayscale samples.
            pixels = np.asarray(oriented, dtype=np.float32)
            largest = float(pixels.max())
            scale = 65535 if oriented.mode.startswith("I;16") or 255 < largest <= 65535 else max(255, largest)
            image = Image.fromarray(np.clip(pixels * (255 / scale), 0, 255).astype(np.uint8)).convert("RGB")
        elif oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
            rgba = oriented.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            image = Image.alpha_composite(background, rgba).convert("RGB")
        else:
            image = oriented.convert("RGB")
    if min(image.size) < 32:
        raise ValueError("The image is too small to be a readable book scan.")
    return image, warnings


def _small_gray(gray: np.ndarray, limit: int = 1600) -> np.ndarray:
    height, width = gray.shape
    scale = min(1.0, limit / max(height, width))
    if scale == 1:
        return gray
    return cv2.resize(
        gray, (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _text_mask(gray: np.ndarray) -> np.ndarray:
    """Ignore solid borders and long binding lines when looking for text gaps."""
    height, width = gray.shape
    block = max(15, min(61, (min(height, width) // 20) | 1))
    mask = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, block, 12,
    )
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    keep = np.ones(count, dtype=np.uint8)
    keep[0] = 0
    for index in range(1, count):
        _, _, comp_width, comp_height, area = stats[index]
        # This is a detection mask only. Never discard these pixels from output.
        if (
            area < 2
            or comp_height > height * 0.18
            or comp_width > width * 0.35
        ):
            keep[index] = 0
    return keep[labels]


def _runs(values: np.ndarray) -> list[tuple[int, int]]:
    changes = np.diff(np.r_[False, values, False].astype(np.int8))
    return list(zip(np.flatnonzero(changes == 1), np.flatnonzero(changes == -1)))


# The angle probe reads text on a reduced copy. Below roughly this size the
# recognizer stops finding lines on a spread photographed at arm's length.
_PROBE_PIXELS = 2000

_ROTATIONS = {
    90: Image.Transpose.ROTATE_90,
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,
}


def _band_variation(mask: np.ndarray) -> float:
    """How strongly ink clusters into bands along the vertical axis.

    Upright text alternates between line bodies and the blank leading between
    them, so the row profile swings between peaks and near-zero. Sideways text
    puts part of every line in every row, giving a much flatter profile.
    """
    profile = mask.mean(axis=1).astype(np.float32)
    inked = np.flatnonzero(profile > profile.max() * 0.05)
    if inked.size < 8:
        return 0.0
    profile = profile[inked[0]:inked[-1] + 1]
    average = float(profile.mean())
    return float(profile.std() / average) if average > 0 else 0.0


def _text_axis(mask: np.ndarray) -> tuple[bool, float]:
    """Return whether text lines run horizontally, and how clear that is."""
    horizontal = _band_variation(mask)
    vertical = _band_variation(mask.T)
    total = horizontal + vertical
    if total <= 0:
        return True, 0.0
    return horizontal >= vertical, abs(horizontal - vertical) / total


def _ascender_bias(mask: np.ndarray) -> float:
    """Positive when ink sits above each line's midpoint, as upright Latin script does.

    x-height bodies and ascenders carry far more ink than the sparse descenders
    below the baseline, so a line band's ink centroid lies above its centre.
    Turning the page 180 degrees reverses that.
    """
    profile = mask.mean(axis=1).astype(np.float32)
    if profile.max() <= 0:
        return 0.0
    bands = _runs(profile > profile.max() * 0.25)
    offsets = []
    for start, end in bands:
        height = end - start
        if height < 4:
            continue
        weights = profile[start:end]
        total = float(weights.sum())
        if total <= 0:
            continue
        centroid = float((np.arange(height) * weights).sum() / total)
        offsets.append(((height - 1) / 2 - centroid) / height)
    if len(offsets) < 4:
        return 0.0
    return float(np.median(offsets))


def _geometric_orientation(small: np.ndarray) -> tuple[int, float, list[str]]:
    """Last-resort orientation from ink geometry alone, for scans no engine read.

    Reliable only on clean, roughly upright scans. On a photograph of a held
    book — tilted, on a dark background — the profiles flatten and this
    correctly reports that it does not know, rather than guessing.
    """
    mask = _text_mask(small)
    horizontal, axis_confidence = _text_axis(mask)
    if axis_confidence < 0.15:
        return 0, axis_confidence, [
            "The text direction could not be determined; the scan was left as it is. "
            "Photograph spreads upright, or set the rotation manually."
        ]
    upright = small if horizontal else cv2.rotate(small, cv2.ROTATE_90_COUNTERCLOCKWISE)
    base = 0 if horizontal else 90
    bias = _ascender_bias(_text_mask(upright))
    if abs(bias) < 0.015:
        # A wrong half turn silently reverses page order, so decline to guess.
        return base, min(axis_confidence, 0.3), [
            "The scan could not be told apart from its upside-down twin. Check the "
            "preview for reversed page order, or set the rotation manually."
        ]
    rotation = (base + 180) % 360 if bias < 0 else base
    return rotation, min(axis_confidence, min(1.0, abs(bias) / 0.05)), []


def detect_orientation(
    gray: np.ndarray,
    angle_probe: Callable[[Image.Image], TextGeometry | None] | None = None,
) -> tuple[int, float | None, float, list[str], TextGeometry | None]:
    """Work out how a scan sits in the frame.

    Returns the quarter turn to apply counter-clockwise, the skew left over
    afterwards (``None`` when unmeasured), a confidence, and any warnings.

    ``angle_probe`` measures the angle of the printed lines — an OCR pass, which
    is the only thing that reads a page reliably enough to tell upright from
    upside down. Geometry alone cannot: it sees the axis but not the direction.
    """
    small = _small_gray(gray, 1000)
    if np.count_nonzero(_text_mask(small)) < small.size * 0.0005:
        return 0, None, 0.0, ["Too little text was found to determine the page orientation."], None
    if angle_probe is not None:
        # The probe needs enough pixels to read by; 1000px is too small for a
        # spread photographed at arm's length.
        probe_image = Image.fromarray(_small_gray(gray, _PROBE_PIXELS))
        measured = angle_probe(probe_image)
        if measured is not None:
            angle, support = measured.angle, measured.support
            correction = -angle
            rotation = round(correction / 90) * 90
            residual = correction - rotation
            confidence = float(np.clip(support / 20, 0, 1))
            warnings: list[str] = []
            if abs(residual) > 20:
                warnings.append(
                    f"The text lines sit {residual:.0f}° off square, which is more than "
                    "straightening can correct well. Photograph the page square-on."
                )
            return rotation % 360, float(residual), confidence, warnings, measured
    rotation, confidence, warnings = _geometric_orientation(small)
    return rotation, None, confidence, warnings, None


def _apply_rotation(image: Image.Image, rotation: int) -> Image.Image:
    transpose = _ROTATIONS.get(rotation % 360)
    return image if transpose is None else image.transpose(transpose)


def _rotate_boxes(boxes: list[tuple[float, float, float, float]], rotation: int
                  ) -> list[tuple[float, float, float, float]]:
    """Carry normalised boxes through a quarter turn counter-clockwise."""
    turn = rotation % 360
    if turn == 0:
        return list(boxes)
    if turn == 90:
        move = lambda x, y: (y, 1 - x)
    elif turn == 180:
        move = lambda x, y: (1 - x, 1 - y)
    else:
        move = lambda x, y: (1 - y, x)
    moved = []
    for left, top, right, bottom in boxes:
        corners = [move(left, top), move(right, top), move(left, bottom), move(right, bottom)]
        xs = [point[0] for point in corners]
        ys = [point[1] for point in corners]
        moved.append((min(xs), min(ys), max(xs), max(ys)))
    return moved


def _crop_boxes(boxes: list[tuple[float, float, float, float]],
                region: tuple[int, int, int, int], size: tuple[int, int],
                ) -> list[tuple[float, float, float, float]]:
    """Re-express normalised boxes against a cropped frame."""
    width, height = size
    left, top, right, bottom = region
    span_x = (right - left) / width
    span_y = (bottom - top) / height
    if span_x <= 0 or span_y <= 0:
        return list(boxes)
    origin_x, origin_y = left / width, top / height
    return [((x0 - origin_x) / span_x, (y0 - origin_y) / span_y,
             (x1 - origin_x) / span_x, (y1 - origin_y) / span_y)
            for x0, y0, x1, y1 in boxes]


def _gutter_from_lines(boxes: list[tuple[float, float, float, float]],
                       ) -> tuple[float, float, list[str]] | None:
    """Locate the binding from the recognized text blocks rather than from pixels.

    A spread's two pages are two columns of text with a clear channel between
    them. That channel is what the binding is, and text-line boxes describe it
    directly — no need to infer it from whitespace runs and dark seams, which is
    guesswork on a photograph where the gutter is lit rather than shadowed.
    Returns None when there is too little text to say anything.
    """
    if len(boxes) < 8:
        return None
    bins = 1000
    occupied = np.zeros(bins, dtype=bool)
    for left, _, right, _ in boxes:
        start = max(0, int(left * bins))
        end = min(bins, int(math.ceil(right * bins)))
        if end > start:
            occupied[start:end] = True
    channels = [
        (start, end) for start, end in _runs(~occupied)
        if 0.25 < (start + end) / 2 / bins < 0.75
    ]
    if not channels:
        return None
    channels.sort(key=lambda run: run[1] - run[0], reverse=True)
    start, end = channels[0]
    position = (start + end) / 2 / bins
    channel = (end - start) / bins

    left_lines = sum(1 for _, _, right, _ in boxes if right <= position)
    right_lines = sum(1 for left, _, _, _ in boxes if left >= position)
    crossing = sum(1 for left, _, right, _ in boxes if left < position < right)
    if not left_lines or not right_lines:
        return position, 0.0, [
            "Only one page carries text, so the binding could not be confirmed. "
            "Check the preview, or mark this scan as a single page."
        ]
    warnings: list[str] = []
    # What locates the binding is the inner edge of each text block, and a
    # handful of lines fixes an edge as well as a hundred do. Judging the two
    # sides against each other instead would call a chapter opening — a few
    # lines facing a full page — ambiguous when the channel is perfectly clear.
    evidence = float(np.clip(min(left_lines, right_lines) / 8, 0, 1))
    intact = 1 - crossing / len(boxes)
    runner_up = (channels[1][1] - channels[1][0]) / bins if len(channels) > 1 else 0.0
    separation = float(np.clip(1 - runner_up / channel, 0, 1)) if channel else 0.0
    # A binding parts two blocks of text. A channel wide enough to be a page of
    # its own means a blank or pictorial page, not a confident split.
    if channel <= 0.18:
        room = 1.0
    else:
        room = float(np.clip((0.40 - channel) / 0.22, 0, 1))
        warnings.append(
            "The blank channel between the pages is unusually wide; a blank or "
            "full-page illustration can make automatic splitting unreliable."
        )
    confidence = float(np.clip(evidence * intact * (0.45 + 0.55 * separation) * room, 0, 1))
    if confidence < 0.60:
        warnings.append(
            "The gutter is ambiguous (for example, a text-column gap can resemble "
            "a binding). Review the preview or provide a manual split."
        )
    return position, confidence, warnings


def _paper_region(gray: np.ndarray) -> tuple[int, int, int, int] | None:
    """Locate the open book within the frame, as (left, top, right, bottom).

    A photograph of a held book carries a lot besides the book: a dark desk, a
    bookshelf, fingers. The gutter detector hunts for a dark vertical band, and
    the book's own outer edge against a dark background is exactly that, so
    without this the split can land on the edge of the paper instead of the
    binding. Returns None when no single bright region dominates, leaving the
    full frame in use rather than cropping away real text on a guess.
    """
    small = _small_gray(gray, 1000)
    height, width = small.shape
    blurred = cv2.GaussianBlur(small, (0, 0), 3)
    _, bright = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Close over the text so a page reads as one filled region, not a mesh of lines.
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(3, width // 60) | 1, max(3, height // 60) | 1))
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, kernel)
    count, _, stats, _ = cv2.connectedComponentsWithStats(bright, connectivity=8)
    if count < 2:
        return None
    index = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    left, top, region_width, region_height, area = stats[index]
    # A page filling most of the frame needs no crop; a small blob is not a book.
    if not 0.25 <= area / (height * width) <= 0.96:
        return None
    if region_width < width * 0.4 or region_height < height * 0.4:
        return None
    # Only ever trim away a dark surround. A merely darker page — a plate, a
    # heavy illustration, tinted paper — must never be cropped off as if it
    # were background, so the discarded part has to be dark in its own right
    # and not just darker than the rest.
    outside = np.ones((height, width), dtype=bool)
    outside[top:top + region_height, left:left + region_width] = False
    if not outside.any():
        return None
    surround = float(np.median(small[outside]))
    paper = float(np.median(small[~outside]))
    if surround > 90 or surround > paper * 0.5:
        return None
    scale_x = gray.shape[1] / width
    scale_y = gray.shape[0] / height
    return (
        max(0, int(left * scale_x)),
        max(0, int(top * scale_y)),
        min(gray.shape[1], int((left + region_width) * scale_x)),
        min(gray.shape[0], int((top + region_height) * scale_y)),
    )


def _detect_gutter(gray: np.ndarray) -> tuple[float, float, list[str]]:
    gray = _small_gray(gray)
    height, width = gray.shape
    # Keep covers, scanner borders, running heads, and folios out of the score.
    body = gray[round(height * 0.09):round(height * 0.91), :]
    ink = _text_mask(body)
    warnings: list[str] = []
    search_left, search_right = round(width * 0.20), round(width * 0.80)
    if np.count_nonzero(ink) < width * 0.3:
        return 0.5, 0.0, [
            "Too little text was found to locate the gutter. A midpoint split "
            "was used; review the preview or provide a manual split."
        ]

    projection = ink.mean(axis=0).astype(np.float32)
    smoothing = max(3, round(width * 0.006) | 1)
    density = np.convolve(projection, np.ones(smoothing) / smoothing, mode="same")
    reference = max(float(np.percentile(density, 80)), 0.003)
    gap_limit = max(0.001, reference * 0.12)
    gaps = density < gap_limit
    gaps[:search_left] = False
    gaps[search_right:] = False
    # A dark binding can be more informative than an empty vertical strip.
    # Requiring darkness over most rows prevents ordinary letters from voting.
    darkness = (body < 100).mean(axis=0)
    dark_columns = darkness > 0.60
    candidates: list[_GutterCandidate] = []
    neighborhood = max(8, round(width * 0.14))
    for start, end in _runs(gaps):
        gap_width = end - start
        if gap_width < max(3, width * 0.006):
            continue
        left = density[max(0, start - neighborhood):start]
        right = density[end:min(width, end + neighborhood)]
        if not left.size or not right.size:
            continue
        support = min(float(left.mean()), float(right.mean())) / reference
        support = float(np.clip(support, 0, 1))
        seam_runs = _runs(dark_columns[start:end])
        seam_runs = [
            (start + a, start + b) for a, b in seam_runs
            if b - a <= width * 0.08
        ]
        if seam_runs:
            seam_start, seam_end = max(seam_runs, key=lambda run: run[1] - run[0])
            position = (seam_start + seam_end) / 2
            seam = float(darkness[seam_start:seam_end].mean())
        else:
            position = (start + end) / 2
            seam = 0.0
        whiteness = 1 - min(1.0, float(density[start:end].mean()) / gap_limit)
        width_score = min(1.0, gap_width / (width * 0.07))
        center_prior = 1 - abs(position / width - 0.5) * 2
        score = (
            0.40 * width_score + 0.30 * support + 0.15 * whiteness
            + 0.45 * seam + 0.02 * center_prior
        )
        # A gap adjoining empty paper is not necessarily the binding.
        score *= 0.35 + 0.65 * support
        candidates.append(_GutterCandidate(position, score, seam, support, gap_width))

    if not candidates:
        center = (search_left + search_right) / 2
        positions = np.arange(search_left, search_right)
        # Weak center preference resolves an otherwise arbitrary equal minimum.
        adjusted = density[search_left:search_right] + (
            np.abs(positions - center) / width * reference * 0.04
        )
        position = int(positions[int(np.argmin(adjusted))])
        return position / width, 0.15, [
            "No clear gutter was found. The estimated split may cross text; "
            "review the preview and provide a manual split if needed."
        ]

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    best = candidates[0]
    alternative = next(
        (candidate for candidate in candidates[1:]
         if abs(candidate.position - best.position) > width * 0.04),
        None,
    )
    separation = (
        min(1.0, max(0.0, (best.score - alternative.score) / max(best.score, 0.01)) * 3)
        if alternative else 1.0
    )
    confidence = float(np.clip(best.score * (0.55 + 0.45 * separation), 0, 1))
    if best.support < 0.18:
        confidence = min(confidence, 0.35)
    if best.width > width * 0.20:
        confidence = min(confidence, 0.40)
        warnings.append(
            "The possible gutter is unusually wide; blank pages or illustrations "
            "can make automatic splitting unreliable."
        )
    if confidence < 0.60:
        warnings.append(
            "The gutter is ambiguous (for example, a text-column gap can resemble "
            "a binding). Review the preview or provide a manual split."
        )
    return best.position / width, confidence, warnings


def _deskew(gray: np.ndarray) -> np.ndarray:
    """Use long text baselines, accepting only a small, consistent rotation."""
    small = _small_gray(gray, 1400)
    height, width = small.shape
    mask = _text_mask(small)
    # Joining letters into words/lines makes Hough evidence about baselines,
    # rather than the vertical strokes of individual characters.
    joined = cv2.morphologyEx(
        mask * 255, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (max(3, width // 100), 1)),
    )
    edges = cv2.Canny(joined, 60, 180)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 720, threshold=max(30, width // 10),
        minLineLength=max(40, width // 7), maxLineGap=max(5, width // 40),
    )
    if lines is None:
        return gray
    angles: list[float] = []
    lengths: list[float] = []
    for x1, y1, x2, y2 in lines[:, 0, :]:
        angle = math.degrees(math.atan2(float(y2 - y1), float(x2 - x1)))
        if -7 <= angle <= 7:
            angles.append(angle)
            lengths.append(math.hypot(float(x2 - x1), float(y2 - y1)))
    if len(angles) < 4:
        return gray
    median = float(np.median(angles))
    residuals = np.abs(np.asarray(angles) - median)
    consistent = residuals < 1.0
    if float(np.mean(consistent)) < 0.65 or float(np.median(residuals)) > 0.5:
        return gray
    angle = float(np.average(np.asarray(angles)[consistent], weights=np.asarray(lengths)[consistent]))
    if abs(angle) < 0.15 or abs(angle) > 6:
        return gray
    height, width = gray.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1)
    cosine, sine = abs(matrix[0, 0]), abs(matrix[0, 1])
    target_width = math.ceil(height * sine + width * cosine)
    target_height = math.ceil(height * cosine + width * sine)
    matrix[0, 2] += (target_width - width) / 2
    matrix[1, 2] += (target_height - height) / 2
    return cv2.warpAffine(
        gray, matrix, (target_width, target_height), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT, borderValue=255,
    )


def _rotate(gray: np.ndarray, angle: float) -> np.ndarray:
    """Rotate by an exact angle, growing the canvas so no ink is cropped."""
    height, width = gray.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1)
    cosine, sine = abs(matrix[0, 0]), abs(matrix[0, 1])
    target_width = math.ceil(height * sine + width * cosine)
    target_height = math.ceil(height * cosine + width * sine)
    matrix[0, 2] += (target_width - width) / 2
    matrix[1, 2] += (target_height - height) / 2
    return cv2.warpAffine(
        gray, matrix, (target_width, target_height), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT, borderValue=255,
    )


def _prepare_page(gray: np.ndarray, deskew: bool, correction: float | None = None) -> Image.Image:
    """``correction`` is the counter-clockwise angle still needed to sit square,
    as measured for the whole spread. When it is None the skew is estimated from
    this page's own baselines instead."""
    # Lift a gray paper background gently without thresholding or denoising.
    # Denoising can erase faint punctuation; aggressive stretching crushes ink.
    paper = float(np.percentile(gray, 95))
    if 120 < paper < 250:
        gray = np.clip(gray.astype(np.float32) * (250 / paper), 0, 255).astype(np.uint8)
    if not deskew:
        return Image.fromarray(gray)
    if correction is None:
        return Image.fromarray(_deskew(gray))
    # A measured angle beats re-estimating it from Hough lines on a single page.
    # OpenCV turns counter-clockwise for a positive angle, matching the measurement.
    if abs(correction) >= 0.15:
        gray = _rotate(gray, correction)
    return Image.fromarray(gray)


def _preview(image: Image.Image, split: float | None, confidence: float) -> Image.Image:
    preview = image.copy()
    preview.thumbnail((1400, 1100), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(preview)
    if split is not None:
        x = round(preview.width * split)
        color = "#15803d" if confidence >= 0.60 else "#dc2626"
        draw.line((x, 0, x, preview.height), fill=color, width=3)
        label = f"Split {split:.1%} | confidence {confidence:.0%}"
    else:
        color = "#15803d"
        label = "Single page"
    label_box = draw.textbbox((10, 10), label)
    draw.rectangle((5, 5, label_box[2] + 6, label_box[3] + 6), fill="white")
    draw.text((10, 10), label, fill=color)
    return preview


def prepare_spread(
    path: Path,
    *,
    split_fraction: float | None = None,
    single_page: bool = False,
    reading_order: str = "ltr",
    deskew: bool = True,
    rotation: int | str = 0,
    angle_probe: Callable[[Image.Image], TextGeometry | None] | None = None,
    crop_paper: bool = True,
) -> PreparedSpread:
    """Load a scan, locate its gutter, and return OCR-ready pages in book order.

    ``rotation`` is a quarter turn counter-clockwise applied before anything
    else, or ``"auto"`` to measure it. Correcting it matters beyond legibility:
    the gutter detector only looks for a vertical binding, so a spread
    photographed sideways would otherwise be split across both pages at once.

    ``crop_paper`` trims the frame to the book itself, so that a photograph's
    background cannot be mistaken for the binding.

    ``split_fraction`` is measured from the left edge of the image that is
    actually split: after any rotation and paper crop.
    Auto detection searches 20–80% of each scan's width. A manual split accepts
    any fraction strictly between zero and one. It can resolve blank pages,
    diagrams, unusual crops, and column layouts which cannot be inferred from
    whitespace alone. Confidence describes the gutter estimate, not OCR quality.
    This corrects mild rotation, not perspective distortion or curved pages.
    """
    if reading_order not in {"ltr", "rtl"}:
        raise ValueError("reading_order must be 'ltr' or 'rtl'.")
    if single_page and split_fraction is not None:
        raise ValueError("single_page and split_fraction cannot be used together.")
    if split_fraction is not None and not (0 < split_fraction < 1):
        raise ValueError("split_fraction must be strictly between 0 and 1.")
    if rotation != "auto" and rotation not in {0, 90, 180, 270}:
        raise ValueError("rotation must be 'auto', 0, 90, 180, or 270.")
    image, warnings = _load_image(Path(path))
    gray = np.asarray(image.convert("L"))
    skew: float | None = None
    orientation_confidence = 1.0
    geometry = None
    if rotation == "auto":
        rotation, skew, orientation_confidence, orientation_warnings, geometry = detect_orientation(gray, angle_probe)
        warnings.extend(orientation_warnings)
    lines = list(geometry.lines) if geometry is not None else []
    if rotation:
        image = _apply_rotation(image, rotation)
        gray = np.asarray(image.convert("L"))
        lines = _rotate_boxes(lines, int(rotation))
    cropped = False
    if crop_paper:
        region = _paper_region(gray)
        if region is not None:
            lines = _crop_boxes(lines, region, image.size)
            image = image.crop(region)
            gray = np.asarray(image.convert("L"))
            cropped = True
    if single_page:
        fraction, confidence = None, 1.0
        crops = [gray]
    else:
        if split_fraction is None:
            # Recognized text blocks describe the binding directly; the pixel
            # detector is the fallback when no engine read the page.
            found = _gutter_from_lines(lines)
            if found is None:
                found = _detect_gutter(gray)
            fraction, confidence, detection_warnings = found
            warnings.extend(detection_warnings)
        else:
            fraction, confidence = float(split_fraction), 1.0
        split_x = round(gray.shape[1] * fraction)
        if split_x < 1 or split_x >= gray.shape[1]:
            raise ValueError("The split would create an empty page at this image resolution.")
        crops = [gray[:, :split_x], gray[:, split_x:]]
        if reading_order == "rtl":
            crops.reverse()
    pages = [_prepare_page(crop, deskew, skew) for crop in crops]
    return PreparedSpread(
        pages=pages, split_fraction=fraction, confidence=confidence,
        warnings=warnings, preview=_preview(image, fraction, confidence),
        rotation=int(rotation), orientation_confidence=orientation_confidence,
        cropped=cropped,
    )

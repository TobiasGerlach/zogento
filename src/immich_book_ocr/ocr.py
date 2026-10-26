"""Tesseract LSTM OCR with official high-accuracy models and word confidences."""
from __future__ import annotations

import csv
import hashlib
import io
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class OCRError(RuntimeError):
    pass


@dataclass
class OCRResult:
    text: str
    confidence: float | None
    words: int
    low_confidence_words: int
    warnings: list[str]


def language_codes(language: str) -> list[str]:
    codes = language.split("+")
    if not codes or any(not re.fullmatch(r"(?:script/)?[A-Za-z][A-Za-z0-9_]*", code) for code in codes):
        raise ValueError("Use Tesseract language codes such as eng, deu, deu+eng, or script/Fraktur.")
    return list(dict.fromkeys(codes))


def tesseract_binary() -> str:
    # A bundled Tesseract (Windows app package) is announced through this variable.
    binary = os.environ.get("BOOK_OCR_TESSERACT") or shutil.which("tesseract")
    if not binary:
        raise OCRError("Tesseract is missing. On macOS run: brew install tesseract. On Linux install tesseract-ocr.")
    return binary


def ensure_models(language: str, mode: str, cache: Path, progress: Callable[[str], None]) -> Path | None:
    codes = language_codes(language)
    binary = tesseract_binary()
    if mode == "system":
        found = subprocess.run([binary, "--list-langs"], capture_output=True, text=True, timeout=30)
        installed = set(found.stdout.splitlines()[1:])
        missing = set(codes) - installed
        if missing:
            raise OCRError(f"Missing installed OCR languages: {', '.join(sorted(missing))}. Use --models best to download them.")
        return None
    if mode != "best":
        raise ValueError("models must be best or system")
    cache.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        session.mount("https://", HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
        for code in codes:
            target = cache / f"{code}.traineddata"
            checksum_file = target.with_suffix(".sha256")
            if target.is_file() and checksum_file.is_file():
                with target.open("rb") as handle:
                    digest = hashlib.file_digest(handle, "sha256").hexdigest()
                if digest == checksum_file.read_text().strip():
                    continue
            progress(f"Downloading high-accuracy OCR model: {code}")
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(".part")
            try:
                url = f"https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/{code}.traineddata"
                with session.get(url, stream=True, timeout=(15, 90)) as response:
                    if response.status_code == 404:
                        raise OCRError(f"No tessdata_best model exists for '{code}'. Check the language code.")
                    response.raise_for_status()
                    digest = hashlib.sha256()
                    with partial.open("wb") as handle:
                        for chunk in response.iter_content(1024 * 1024):
                            if chunk:
                                handle.write(chunk)
                                digest.update(chunk)
                if partial.stat().st_size < 100_000:
                    raise OCRError(f"The downloaded OCR model for {code} is incomplete.")
                partial.replace(target)
                checksum_file.write_text(digest.hexdigest() + "\n")
            except requests.RequestException:
                raise OCRError(f"Could not download OCR model '{code}'. Check internet access or use --models system.") from None
            finally:
                partial.unlink(missing_ok=True)
    return cache.resolve()


def configuration_signature(language: str, tessdata: Path | None) -> str:
    """Invalidate page caches when either the engine or language models change."""
    binary = tesseract_binary()
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    digest = hashlib.sha256((version.stdout + version.stderr).encode())
    if tessdata is None:
        listing = subprocess.run([binary, "--list-langs"], capture_output=True, text=True, timeout=30)
        match = re.search(r'in "([^"]+)"', listing.stdout + listing.stderr)
        if not match:
            # Unknown installation layout: fresh OCR is safer than stale text.
            return uuid.uuid4().hex
        tessdata = Path(match.group(1))
    for code in language_codes(language):
        try:
            with (tessdata / f"{code}.traineddata").open("rb") as handle:
                digest.update(hashlib.file_digest(handle, "sha256").digest())
        except OSError:
            return uuid.uuid4().hex
    return digest.hexdigest()


def recognize_page(image: Path, output_base: Path, *, language: str, tessdata: Path | None, psm: int = 3) -> OCRResult:
    language_codes(language)
    if psm not in (3, 4, 6, 11):
        raise ValueError("Supported page segmentation modes are 3, 4, 6, and 11.")
    output_base.parent.mkdir(parents=True, exist_ok=True)
    command = [tesseract_binary(), str(image), str(output_base), "-l", language, "--oem", "1", "--psm", str(psm), "--dpi", "300"]
    if tessdata is not None:
        command += ["--tessdata-dir", str(tessdata)]
    # Set renderers directly: downloaded model directories need no config files.
    command += ["-c", "tessedit_create_txt=1", "-c", "tessedit_create_tsv=1"]
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=300, env=env)
    except subprocess.TimeoutExpired:
        raise OCRError("OCR exceeded five minutes for one page. Check the scan dimensions and image quality.") from None
    if result.returncode:
        raise OCRError("Tesseract failed: " + result.stderr.strip()[-1500:])
    txt = output_base.with_suffix(".txt").read_text(encoding="utf-8").strip()
    tsv = output_base.with_suffix(".tsv").read_text(encoding="utf-8")
    confidences = []
    for word in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        if word.get("level") == "5" and word.get("text", "").strip():
            try:
                score = float(word["conf"])
            except (ValueError, TypeError):
                continue
            if score >= 0:
                confidences.append(score)
    mean = round(sum(confidences) / len(confidences), 2) if confidences else None
    low = sum(score < 60 for score in confidences)
    warnings = []
    if not txt:
        warnings.append("No text recognized; inspect this page for missing text or a blank/illustrated page.")
    elif mean is None or mean < 80 or low > len(confidences) * 0.15:
        warnings.append("OCR confidence is low; proofread this page against the scan.")
    return OCRResult(txt, mean, len(confidences), low, warnings)


def text_angle(image: Path, tessdata: Path | None) -> tuple[float, int] | None:
    """Coarse page orientation from Tesseract's OSD model, counter-clockwise.

    OSD resolves only quarter turns, so the returned angle is always a multiple
    of 90 and any remaining skew is left to the deskewer. Returns None when OSD
    is missing or unsure, so the caller can fall back rather than mis-rotate.
    """
    command = [tesseract_binary(), str(image), "stdout", "--psm", "0"]
    if tessdata is not None:
        command += ["--tessdata-dir", str(tessdata)]
    env = dict(os.environ, OMP_THREAD_LIMIT="1")
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, env=env)
    except (subprocess.TimeoutExpired, OCRError):
        return None
    if result.returncode:
        return None
    report = dict(
        (key.strip(), value.strip())
        for key, _, value in (line.partition(":") for line in result.stdout.splitlines())
        if value
    )
    try:
        # "Rotate" is the clockwise correction; this module works counter-clockwise.
        rotation = (360 - int(report["Rotate"])) % 360
        confidence = float(report["Orientation confidence"])
    except (KeyError, ValueError):
        return None
    if confidence < 1.0:
        return None
    # OSD reports a quarter turn, not a line angle: one "line" of evidence.
    return float(rotation if rotation <= 180 else rotation - 360) * -1, int(confidence)

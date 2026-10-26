"""Command-line entry point and local browser application launcher."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from .pipeline import PipelineOptions, run_album, run_local


def _default_output(source: str, command: str) -> Path:
    """Keep retries together without writing a private share key into a path."""
    identity = source.strip() if command == "album" else str(Path(source).resolve())
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return Path("output") / f"{command}-{digest}"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["album"]
    elif argv[0].strip().lower().startswith(("http://", "https://")):
        argv.insert(0, "album")
    parser = argparse.ArgumentParser(
        prog="book-ocr",
        description="Paste an Immich share link to OCR English and German in capture-date order.",
        epilog="Run book-ocr with no arguments to paste a link, or use book-ocr 'URL'. "
               "Output folders are chosen automatically; serve opens the optional browser app.",
    )
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="Open a local browser app for album links")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--output", type=Path, default=Path("output"))
    for name, help_text in (("album", "Download and OCR an Immich album"), ("local", "OCR a folder of downloaded scans")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("source", **({"nargs": "?"} if name == "album" else {}),
                             help="Immich share link (asks once if omitted)" if name == "album" else "Directory of scan images")
        command.add_argument("--output", "-o", type=Path, help="Output directory (default: a reusable folder under output/)")
        command.add_argument("--language", "-l", default="eng+deu", help="OCR languages (default: eng+deu, English and German together)")
        command.add_argument("--order", choices=["filename", "album", "date"], default="date", help="Scan order (default: date, oldest capture first for Immich)")
        command.add_argument("--reading-order", choices=["ltr", "rtl"], default="ltr")
        command.add_argument("--single-page", action="store_true", help="Treat each image as one page")
        command.add_argument("--psm", type=int, choices=[3, 4, 6, 11], default=3, help="Tesseract layout mode: 3 automatic, 4 single column, 6 block, 11 sparse")
        command.add_argument("--models", choices=["best", "system"], default="best", help="Tesseract model set; ignored by the Vision engine")
        command.add_argument("--engine", choices=["auto", "vision", "tesseract"], default="auto",
                             help="auto prefers Apple Vision on macOS: far better on rotated or skewed handheld photos, and several times faster")
        command.add_argument("--rotation", choices=["auto", "none", "0", "90", "180", "270"], default="auto",
                             help="Quarter turn counter-clockwise applied before splitting; auto measures it per scan")
        command.add_argument("--no-crop", dest="crop_paper", action="store_false",
                             help="Keep the whole photo instead of trimming a dark background around the book")
        command.add_argument("--workers", type=int, default=0, help="Scans to process in parallel (0 chooses a value from the CPU count)")
        command.add_argument("--splits", type=Path, help='JSON mapping original scan filenames to split fractions, e.g. {"scan001.jpg": 0.43}')
    args = parser.parse_args(argv)
    if args.command == "serve":
        from .web import create_app

        port = getattr(args, "port", 8765)
        if not 1 <= port <= 65535:
            parser.error("port must be between 1 and 65535")
        output = getattr(args, "output", Path("output"))
        print(f"Open http://127.0.0.1:{port} — press Ctrl+C to stop.", flush=True)
        create_app(output).run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
        return 0
    try:
        if args.command == "album":
            args.source = (args.source if args.source is not None else input("Immich share link: ")).strip()
            if not args.source:
                raise ValueError("Paste an Immich share link.")
        output = args.output if args.output is not None else _default_output(args.source, args.command)
        splits = json.loads(args.splits.read_text(encoding="utf-8")) if args.splits else {}
        if not isinstance(splits, dict):
            raise ValueError("The split override file must contain a JSON object.")
        options = PipelineOptions(language=args.language, order=args.order, reading_order=args.reading_order,
                                  single_page=args.single_page, psm=args.psm, models=args.models,
                                  engine=args.engine, workers=args.workers, rotation=args.rotation,
                                  crop_paper=args.crop_paper, split_overrides=splits)
        progress = lambda message: print(message, flush=True)
        print(f"OCR languages: {options.language} | Scan order: {options.order}", flush=True)
        print(f"Output: {output.resolve()}", flush=True)
        if args.command == "album":
            report = run_album(args.source, output, options=options,
                               api_key=os.environ.get("IMMICH_API_KEY"), password=os.environ.get("IMMICH_SHARE_PASSWORD"), progress=progress)
        else:
            report = run_local(Path(args.source), output, options=options, progress=progress)
        print(f"Text: {(output / 'book.txt').resolve()}")
        print(f"Review: {(output / 'review.html').resolve()}")
        return 0 if report["complete"] else 2
    except EOFError:
        print("Error: No link received. Paste an Immich share link or pass it as an argument.", file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped. Completed downloads and OCR pages remain cached; rerun to continue.", file=sys.stderr)
        return 130

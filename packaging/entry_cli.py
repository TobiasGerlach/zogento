"""PyInstaller-Einstieg für die Konsole (`bib`)."""
import multiprocessing
import os
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if os.environ.get("BIBLIOTHEK_SMOKE"):
        import smoke
        sys.exit(smoke.run())
    from bibliothek.cli import main
    sys.exit(main())

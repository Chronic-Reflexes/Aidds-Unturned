"""Unturned workshop compatibility patch generator."""

import argparse
import sys
from pathlib import Path

try:
    from Modules.gui import run_gui
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unturned compatibility patch generator for workshop mods."
    )
    parser.add_argument("--no-gui", action="store_true", help="Run without GUI")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.no_gui and GUI_AVAILABLE:
        run_gui()
    else:
        print("GUI is not available or --no-gui was specified.")
        print("This application uses tkinter from the Python standard library.")
        sys.exit(1)


if __name__ == "__main__":
    main()

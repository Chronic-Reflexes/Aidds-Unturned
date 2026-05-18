import csv
import os
from pathlib import Path
from typing import Optional

try:
    import tkinter as tk
    from tkinter import filedialog
    GUI_AVAILABLE = True
except ImportError:
    GUI_AVAILABLE = False


def _ask_path(prompt: str) -> str:
    path = input(f"{prompt}: ").strip()
    return path


def choose_file(title: str, initialdir: Optional[str] = None, filetypes=None, use_gui: bool = True) -> Optional[str]:
    if use_gui and GUI_AVAILABLE:
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(
                title=title,
                initialdir=initialdir or os.getcwd(),
                filetypes=filetypes or [("All files", "*.*")],
            )
            root.destroy()
            if path:
                return path
        except Exception:
            pass
    print(f"{title}")
    path = request_existing_path("Enter the path to the file")
    return str(path)


def choose_directory(title: str, initialdir: Optional[str] = None, use_gui: bool = True) -> Optional[str]:
    if use_gui and GUI_AVAILABLE:
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(
                title=title,
                initialdir=initialdir or os.getcwd(),
            )
            root.destroy()
            if path:
                return path
        except Exception:
            pass
    print(f"{title}")
    directory = request_existing_directory("Enter the workshop mods directory path")
    return str(directory)


def request_existing_path(prompt: str) -> Path:
    while True:
        raw = _ask_path(prompt)
        path = Path(raw).expanduser()
        if path.exists():
            return path
        print(f"Path not found: {path}")


def request_existing_directory(prompt: str) -> Path:
    while True:
        raw = _ask_path(prompt)
        path = Path(raw).expanduser()
        if path.exists() and path.is_dir():
            return path
        print(f"Directory not found: {path}")


def request_csv_path(prompt: str) -> Path:
    while True:
        raw = _ask_path(prompt)
        path = Path(raw).expanduser()
        if path.exists() and path.is_file():
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    sample = handle.read(2048)
                    if sample.strip():
                        return path
            except Exception:
                pass
        print(f"Invalid CSV path: {path}")

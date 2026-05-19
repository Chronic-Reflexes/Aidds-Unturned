import os
import sys
import threading
from pathlib import Path
from queue import Queue, Empty
from typing import Any, List

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText
import webbrowser

from .models import WorkshopMod
from .workshop import WorkshopScanner
from .worker import PatchWorker


class MainWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Aidds - Automatic ID Deployment System")
        self.geometry("980x760")
        self.game_dir: Path | None = None
        self.workshop_path: Path | None = None
        self.csv_path: Path | None = None
        self.log_path: Path | None = None
        self.output_path: Path | None = None
        self.output_base: Path = Path(__file__).resolve().parents[1]
        self.mods: List[WorkshopMod] = []
        self.worker: PatchWorker | None = None
        self.worker_queue: Queue = Queue()
        self.last_output_path: Path | None = None
        self.scanner = WorkshopScanner()

        self._build_ui()
        self.after(200, self._poll_worker_queue)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)

        input_frame = ttk.Frame(frame)
        input_frame.pack(fill=tk.X, pady=(0, 10))

        self.game_button = ttk.Button(input_frame, text="Select Unturned Game Directory", command=self.on_select_game_directory)
        self.game_button.grid(row=0, column=0, sticky=tk.W)
        self.game_label = ttk.Entry(input_frame, state="readonly")
        self.game_label.grid(row=0, column=1, sticky=tk.EW, padx=5)

        self.workshop_button = ttk.Button(input_frame, text="Override Workshop Folder", command=self.on_select_manual_workshop_folder)
        self.workshop_button.grid(row=1, column=0, sticky=tk.W, pady=5)
        self.workshop_button.state(["disabled"])
        self.workshop_label = ttk.Entry(input_frame, state="readonly")
        self.workshop_label.grid(row=1, column=1, sticky=tk.EW, padx=5)

        self.csv_button = ttk.Button(input_frame, text="Select SPAWN Legacy ID Availability.csv", command=self.on_select_csv)
        self.csv_button.grid(row=2, column=0, sticky=tk.W, pady=5)
        self.csv_label = ttk.Entry(input_frame, state="readonly")
        self.csv_label.grid(row=2, column=1, sticky=tk.EW, padx=5)

        self.log_button = ttk.Button(input_frame, text="Select Client.log", command=self.on_select_log)
        self.log_button.grid(row=3, column=0, sticky=tk.W)
        self.log_label = ttk.Entry(input_frame, state="readonly")
        self.log_label.grid(row=3, column=1, sticky=tk.EW, padx=5)

        self.output_button = ttk.Button(input_frame, text="Select Output Folder", command=self.on_select_output_directory)
        self.output_button.grid(row=4, column=0, sticky=tk.W, pady=5)
        self.output_label = ttk.Entry(input_frame, state="readonly")
        self.output_label.grid(row=4, column=1, sticky=tk.EW, padx=5)

        input_frame.columnconfigure(1, weight=1)
        self._set_label(self.output_label, f"Default: {self.output_base}")

        mods_frame = ttk.LabelFrame(frame, text="Workshop Mods")
        mods_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        columns = ("selected", "title", "workshop_id")
        self.mods_tree = ttk.Treeview(mods_frame, columns=columns, show="headings", selectmode="none")
        self.mods_tree.heading("selected", text="Selected")
        self.mods_tree.heading("title", text="Mod Name")
        self.mods_tree.heading("workshop_id", text="Workshop ID")
        self.mods_tree.column("selected", width=90, anchor=tk.CENTER)
        self.mods_tree.column("title", width=600, anchor=tk.W)
        self.mods_tree.column("workshop_id", width=160, anchor=tk.CENTER)
        self.mods_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        self.mods_tree.bind("<Button-1>", self._on_mods_tree_click)
        self.mods_tree.bind("<Double-1>", self._on_mods_tree_double_click)

        scrollbar = ttk.Scrollbar(mods_frame, orient=tk.VERTICAL, command=self.mods_tree.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.mods_tree.configure(yscrollcommand=scrollbar.set)

        options_frame = ttk.Frame(frame)
        options_frame.pack(fill=tk.X, pady=(0, 10))

        self.dry_run_var = tk.BooleanVar(value=False)
        self.export_csv_var = tk.BooleanVar(value=False)
        self.export_json_var = tk.BooleanVar(value=False)
        self.dry_run_checkbox = ttk.Checkbutton(options_frame, text="Dry run", variable=self.dry_run_var)
        self.export_csv_checkbox = ttk.Checkbutton(options_frame, text="Export CSV mapping", variable=self.export_csv_var)
        self.export_json_checkbox = ttk.Checkbutton(options_frame, text="Export JSON mapping", variable=self.export_json_var)
        self.dry_run_checkbox.grid(row=0, column=0, padx=(0, 20))
        self.export_csv_checkbox.grid(row=0, column=1, padx=(0, 20))
        self.export_json_checkbox.grid(row=0, column=2, padx=(0, 20))
        self.rent_server_button = ttk.Button(options_frame, text="Rent a server", command=self.on_rent_server)
        self.rent_server_button.grid(row=0, column=3)

        buttons_frame = ttk.Frame(frame)
        buttons_frame.pack(fill=tk.X, pady=(0, 10))

        self.start_button = ttk.Button(buttons_frame, text="Start Patch Generation", command=self.on_start)
        self.start_button.pack(side=tk.LEFT)
        self.start_button.state(["disabled"])

        self.open_output_button = ttk.Button(buttons_frame, text="Open Last Output Folder", command=self.on_open_output_folder)
        self.open_output_button.pack(side=tk.LEFT, padx=(10, 0))
        self.open_output_button.state(["disabled"])

        self.progress_bar = ttk.Progressbar(frame, mode="determinate")
        self.progress_bar.pack(fill=tk.X, pady=(0, 10))

        self.log_output = ScrolledText(frame, height=14, state="disabled")
        self.log_output.pack(fill=tk.BOTH, expand=True)

        self.status_label = ttk.Label(frame, text="Awaiting input selection...")
        self.status_label.pack(fill=tk.X, pady=(5, 0))

    def on_select_game_directory(self) -> None:
        path = filedialog.askdirectory(title="Select Unturned Game Directory")
        if not path:
            return
        selected_path = Path(path)
        if not self._validate_game_dir(selected_path):
            messagebox.showerror(
                "Invalid Unturned directory",
                "The selected folder is not a valid Unturned game directory. It must contain Unturned.exe or Unturned_Data.",
            )
            return

        self.game_dir = selected_path
        self.game_label.config(state="normal")
        self.game_label.delete(0, tk.END)
        self.game_label.insert(0, str(self.game_dir))
        self.game_label.config(state="readonly")

        self._resolve_workshop_path()
        self._resolve_game_paths()

    def on_select_manual_workshop_folder(self) -> None:
        path = filedialog.askdirectory(title="Select Workshop Mods Folder")
        if not path:
            return
        selected_path = Path(path)
        if not selected_path.exists():
            messagebox.showerror("Invalid path", "Selected workshop path does not exist.")
            return

        self.workshop_path = selected_path
        self.workshop_label.config(state="normal")
        self.workshop_label.delete(0, tk.END)
        self.workshop_label.insert(0, str(self.workshop_path))
        self.workshop_label.config(state="readonly")
        self.workshop_button.state(["disabled"])
        self._load_workshop_mods()
        self._update_start_button()

    def _resolve_game_paths(self) -> None:
        if not self.game_dir:
            return

        log_candidate = self.game_dir / "Logs" / "Client.log"
        if log_candidate.exists():
            self.log_path = log_candidate
            self._set_label(self.log_label, self.log_path)
        else:
            self.log_path = None
            self._set_label(self.log_label, "<Client.log not found>")

        csv_candidate = (
            self.game_dir
            / "Extras"
            / "AssetIDs"
            / "All Assets"
            / "Grouped by Legacy Category"
            / "SPAWN Legacy ID Availability.csv"
        )
        if csv_candidate.exists():
            self.csv_path = csv_candidate
            self._set_label(self.csv_label, self.csv_path)
        else:
            self.csv_path = None
            self._set_label(self.csv_label, "<SPAWN Legacy ID Availability.csv not found>")
            messagebox.showinfo(
                "CSV Not Found",
                "SPAWN Legacy ID Availability.csv was not found in the Unturned directory.\n\n"
                "Please generate it from the Unturned main menu:\n"
                "1. Click the Workshop button\n"
                "2. Press F1 on your keyboard\n"
                "3. Click \"Export Asset IDs\"\n"
                "4. Re-run the program",
            )

        self._update_start_button()

    def on_select_output_directory(self) -> None:
        path = filedialog.askdirectory(title="Select Output Folder")
        if not path:
            return
        self.output_path = Path(path)
        self._set_label(self.output_label, self.output_path)

    def _set_label(self, widget: ttk.Entry, value: object) -> None:
        widget.config(state="normal")
        widget.delete(0, tk.END)
        widget.insert(0, str(value))
        widget.config(state="readonly")

    def _validate_game_dir(self, path: Path) -> bool:
        return (path / "Unturned.exe").exists() or (path / "Unturned_Data").exists()

    def _resolve_workshop_path(self) -> None:
        if not self.game_dir:
            return
        if len(self.game_dir.parents) < 2:
            self.workshop_path = None
            self._show_workshop_resolution_failure()
            return

        steamapps = self.game_dir.parents[1]
        candidate = steamapps / "workshop" / "content" / "304930"
        if candidate.exists() and candidate.is_dir():
            self.workshop_path = candidate
            self.workshop_label.config(state="normal")
            self.workshop_label.delete(0, tk.END)
            self.workshop_label.insert(0, str(self.workshop_path))
            self.workshop_label.config(state="readonly")
            self.workshop_button.state(["disabled"])
            self._load_workshop_mods()
            self._update_start_button()
        else:
            self.workshop_path = None
            self._show_workshop_resolution_failure()

    def _show_workshop_resolution_failure(self) -> None:
        self.workshop_label.config(state="normal")
        self.workshop_label.delete(0, tk.END)
        self.workshop_label.insert(0, "<automatic resolution failed>")
        self.workshop_label.config(state="readonly")
        self.status_label.config(text="Resolved workshop path not found. Use Override Workshop Folder to choose manually.")
        self.workshop_button.state(["!disabled"])
        self.mods_tree.delete(*self.mods_tree.get_children())
        self.mods = []
        self._update_start_button()

    def on_select_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="Select SPAWN Legacy ID Availability.csv",
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*")],
        )
        if path:
            self.csv_path = Path(path)
            self.csv_label.config(state="normal")
            self.csv_label.delete(0, tk.END)
            self.csv_label.insert(0, path)
            self.csv_label.config(state="readonly")
            self._update_start_button()

    def on_select_log(self) -> None:
        path = filedialog.askopenfilename(
            title="Select Client.log",
            filetypes=[("Log Files", "*.log"), ("All Files", "*")],
        )
        if path:
            self.log_path = Path(path)
            self.log_label.config(state="normal")
            self.log_label.delete(0, tk.END)
            self.log_label.insert(0, path)
            self.log_label.config(state="readonly")
            self._update_start_button()

    def on_rent_server(self) -> None:
        webbrowser.open_new_tab("https://midnighthostingsolutions.com")

    def _load_workshop_mods(self) -> None:
        self.mods_tree.delete(*self.mods_tree.get_children())
        self.mods = []
        if not self.workshop_path or not self.workshop_path.exists():
            return
        self.mods = self.scanner.discover_workshop_mods(self.workshop_path)
        self.mods.sort(key=lambda mod: mod.title.lower())
        for mod in self.mods:
            self.mods_tree.insert("", tk.END, iid=mod.workshop_id, values=("☐", mod.title, mod.workshop_id))
        self.status_label.config(text=f"Loaded {len(self.mods)} workshop mods. Select mods to patch.")

    def _update_start_button(self) -> None:
        enabled = bool(self.workshop_path and self.csv_path and self.log_path and self.mods)
        if enabled:
            self.start_button.state(["!disabled"])
        else:
            self.start_button.state(["disabled"])

    def _on_mods_tree_click(self, event: Any) -> None:
        item_id = self.mods_tree.identify_row(event.y)
        column = self.mods_tree.identify_column(event.x)
        if not item_id or column != "#1":
            return
        current = self.mods_tree.set(item_id, "selected")
        new_value = "☐" if current == "☑" else "☑"
        self.mods_tree.set(item_id, "selected", new_value)

    def _get_selected_mods(self) -> List[WorkshopMod]:
        selected: List[WorkshopMod] = []
        for mod in self.mods:
            cell = self.mods_tree.set(mod.workshop_id, "selected")
            if cell == "☑":
                selected.append(mod)
        return selected

    def _on_mods_tree_double_click(self, event: Any) -> None:
        item_id = self.mods_tree.identify_row(event.y)
        if not item_id:
            return
        mod = next((m for m in self.mods if m.workshop_id == item_id), None)
        if not mod:
            return

        new_title = simpledialog.askstring(
            "Edit Mod Name",
            "Enter a friendly mod name for this workshop mod:",
            initialvalue=mod.title,
            parent=self,
        )
        if new_title is None:
            return
        new_title = new_title.strip() or "Unknown"
        mod.title = new_title
        mod.display_name = f"{new_title} ({mod.workshop_id})"
        self.mods_tree.set(item_id, "title", new_title)
        self.scanner.set_override(mod.workshop_id, new_title)
        self.status_label.config(text=f"Saved custom title for {mod.workshop_id}.")

    def on_start(self) -> None:
        selected_mods = self._get_selected_mods()
        if not selected_mods:
            messagebox.showwarning("No mods selected", "Please select at least one mod to patch.")
            return
        if not self.workshop_path or not self.csv_path or not self.log_path:
            messagebox.showwarning("Missing inputs", "Select the workshop folder, CSV file, and Client.log before starting.")
            return

        self._set_ui_state(running=True)
        self._append_log("Starting patch generation...")
        self.progress_bar.config(value=0)
        self.status_label.config(text="Starting patch generation...")

        self.worker = PatchWorker(
            workshop_root=self.workshop_path,
            client_log_path=self.log_path,
            csv_path=self.csv_path,
            selected_mods=selected_mods,
            output_root=self.output_path or self.output_base,
            dry_run=self.dry_run_var.get(),
            export_csv=self.export_csv_var.get(),
            export_json=self.export_json_var.get(),
            log_callback=self._enqueue_log,
            progress_callback=self._enqueue_progress,
            finished_callback=self._enqueue_finished,
            error_callback=self._enqueue_error,
        )
        self.worker.start()

    def _set_ui_state(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for widget in [self.game_button, self.workshop_button, self.csv_button, self.log_button, self.output_button, self.start_button]:
            widget.state([state] if state == "disabled" else ["!disabled"])
        self.mods_tree.state([state] if state == "disabled" else ["!disabled"])
        if running:
            self.open_output_button.state(["disabled"])

    def _append_log(self, text: str) -> None:
        self.log_output.configure(state="normal")
        self.log_output.insert(tk.END, text + "\n")
        self.log_output.see(tk.END)
        self.log_output.configure(state="disabled")

    def _enqueue_log(self, message: str) -> None:
        self.worker_queue.put(("log", message))

    def _enqueue_progress(self, current: int, total: int, message: str) -> None:
        self.worker_queue.put(("progress", (current, total, message)))

    def _enqueue_finished(self, success: bool, output_path: str, report: object) -> None:
        self.worker_queue.put(("finished", (success, output_path, report)))

    def _enqueue_error(self, message: str) -> None:
        self.worker_queue.put(("error", message))

    def _poll_worker_queue(self) -> None:
        try:
            while True:
                item = self.worker_queue.get_nowait()
                self._handle_worker_message(item)
        except Empty:
            pass
        self.after(100, self._poll_worker_queue)

    def _handle_worker_message(self, item: Any) -> None:
        kind, payload = item
        if kind == "log":
            self._append_log(payload)
        elif kind == "progress":
            current, total, message = payload
            if total > 0:
                value = min(100, int(current / total * 100))
            else:
                value = 0
            self.progress_bar.config(value=value)
            self.status_label.config(text=message)
        elif kind == "finished":
            success, output_path, report = payload
            self._append_log("Finished patch generation.")
            self.status_label.config(text="Patch generation complete")
            self._set_ui_state(running=False)
            self.open_output_button.state(["!disabled"])
            self.last_output_path = Path(output_path)
            messagebox.showinfo("Completed", f"Patch output folder: {output_path}")
            self._open_folder(self.last_output_path)
        elif kind == "error":
            self._append_log(f"[ERROR] {payload}")
            self.status_label.config(text="Error occurred")
            messagebox.showerror("Error", payload)
            self._set_ui_state(running=False)

    def on_open_output_folder(self) -> None:
        if self.last_output_path and self.last_output_path.exists():
            self._open_folder(self.last_output_path)

    def _open_folder(self, folder: Path) -> None:
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform.startswith("darwin"):
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception:
            messagebox.showwarning("Open folder", f"Unable to open folder: {folder}")


def run_gui() -> None:
    app = MainWindow()
    app.mainloop()

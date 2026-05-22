import threading
from pathlib import Path
from typing import Callable, List, Optional

from .models import WorkshopMod
from .patch_builder import PatchBuilder


class PatchWorker(threading.Thread):
    def __init__(
        self,
        workshop_root: Path,
        client_log_path: Path,
        csv_path: Path,
        selected_mods: List[WorkshopMod],
        output_root: Optional[Path] = None,
        dry_run: bool = False,
        export_csv: bool = False,
        export_json: bool = False,
        game_root: Optional[Path] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        finished_callback: Optional[Callable[[bool, str, object], None]] = None,
        error_callback: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(daemon=True)
        self.workshop_root = workshop_root
        self.client_log_path = client_log_path
        self.csv_path = csv_path
        self.selected_mods = selected_mods
        self.output_root = output_root
        self.dry_run = dry_run
        self.export_csv = export_csv
        self.export_json = export_json
        self.game_root = game_root
        self.progress_callback = progress_callback
        self.log_callback = log_callback
        self.finished_callback = finished_callback
        self.error_callback = error_callback

    def run(self) -> None:
        try:
            builder = PatchBuilder(
                workshop_root=self.workshop_root,
                client_log_path=self.client_log_path,
                csv_path=self.csv_path,
                selected_mods=self.selected_mods,
                output_root=self.output_root,
                dry_run=self.dry_run,
                export_csv=self.export_csv,
                export_json=self.export_json,
                game_root=self.game_root,
                log_callback=self.log_callback,
                progress_callback=self.progress_callback,
            )
            output_root, report = builder.run()
            if self.finished_callback:
                self.finished_callback(True, str(output_root), report)
        except Exception as exc:
            if self.error_callback:
                self.error_callback(str(exc))

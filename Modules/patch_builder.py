import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .id_manager import IDManager
from .logger import configure_logger
from .models import AssignedID, Conflict, FixReport, WorkshopMod
from .parsers import ClientLogParser, DatAssetScanner
from .report import ReportGenerator


def sanitize_folder_name(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "", name).strip()
    safe = re.sub(r"\s+", " ", safe)
    return safe or "WorkshopCompatibilityPatch"


class PatchBuilder:
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
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ):
        self.workshop_root = workshop_root
        self.client_log_path = client_log_path
        self.csv_path = csv_path
        self.selected_mods = selected_mods
        self.dry_run = dry_run
        self.export_csv = export_csv
        self.export_json = export_json
        self.log_callback = log_callback
        self.progress_callback = progress_callback

        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.base_output_root = output_root or Path.cwd()
        self.output_root = self.base_output_root / f"unturned_patch_{self.timestamp}"
        if self.dry_run:
            self.output_root = self.base_output_root / f"unturned_patch_dryrun_{self.timestamp}"
        self.output_root.mkdir(parents=True, exist_ok=True)

        self.log_file = self.output_root / f"fix_unturned_ids_{self.timestamp}.log"
        self.logger = configure_logger(self.log_file, verbose=True)
        self.report_generator = ReportGenerator(self.output_root)
        self.report = FixReport()
        self.id_manager = IDManager(min_allocatable_id=3000)
        self.mod_map: Dict[str, WorkshopMod] = {mod.workshop_id: mod for mod in self.selected_mods}
        self.assignment_map: Dict[Tuple[str, str, int], AssignedID] = {}

    def run(self) -> Tuple[Path, FixReport]:
        self._log("Starting compatibility patch build")
        self._emit_progress(0, 100, "Starting build")
        conflicts = self._parse_conflicts()
        selected_ids = {mod.workshop_id for mod in self.selected_mods}
        self._log(f"Selected mods: {', '.join(sorted(selected_ids))}")
        filtered_conflicts = [
            c
            for c in conflicts
            if c.source_workshop_id in selected_ids or c.existing_owner_workshop_id in selected_ids
        ]
        self._log(f"Parsed {len(conflicts)} conflicts; {len(filtered_conflicts)} involve selected mods")
        deduped_conflicts = self._deduplicate_conflicts(filtered_conflicts)
        if not deduped_conflicts:
            self._log("No selected mod conflicts were found.")
            self._write_reports()
            return self.output_root, self.report

        patch_mod_ids: Set[str] = set()
        for conflict in deduped_conflicts:
            if conflict.source_workshop_id in self.mod_map:
                patch_mod_ids.add(conflict.source_workshop_id)
            elif conflict.existing_owner_workshop_id in self.mod_map:
                patch_mod_ids.add(conflict.existing_owner_workshop_id)

        patch_mods = [self.mod_map[workshop_id] for workshop_id in sorted(patch_mod_ids)]
        self._prepare_patch_workspace(patch_mods)
        self._load_id_pool()
        self._process_conflicts(deduped_conflicts)
        if not self.dry_run:
            self._log("Applying patches to copied workspace")
        else:
            self._log("Dry run enabled; no files will be modified")
        self._write_reports()
        self._emit_progress(100, 100, "Completed")
        self._log("Compatibility patch build completed")
        return self.output_root, self.report

    def _parse_conflicts(self) -> List[Conflict]:
        parser = ClientLogParser()
        return parser.parse_conflicts(self.client_log_path)

    def _prepare_patch_workspace(self, mods_to_copy: Optional[List[WorkshopMod]] = None) -> None:
        if self.dry_run:
            self._log("Skipping workspace copy in dry-run mode")
            for mod in self.selected_mods:
                mod.patch_path = mod.path
            return

        mods = mods_to_copy if mods_to_copy is not None else self.selected_mods
        self._log("Copying selected mods into temporary patch workspace")
        for index, mod in enumerate(mods, start=1):
            folder_name = sanitize_folder_name(mod.title)
            patch_folder_name = f"{folder_name} Compatibility Patch ({mod.workshop_id})"
            destination = self.output_root / patch_folder_name
            if destination.exists():
                suffix = 1
                while True:
                    alternate = self.output_root / f"{patch_folder_name} ({suffix})"
                    if not alternate.exists():
                        destination = alternate
                        break
                    suffix += 1
            shutil.copytree(mod.path, destination)
            mod.patch_path = destination
            self._log(f"Copied {mod.display_name} to patch folder")
            self._emit_progress(index, len(self.selected_mods) + 5, f"Copied {mod.display_name}")
            self._update_workshop_json_title(mod)

    def _update_workshop_json_title(self, mod: WorkshopMod) -> None:
        if not mod.patch_path:
            return
        workshop_json = mod.patch_path / "workshop.json"
        if not workshop_json.exists():
            return
        try:
            payload = json.loads(workshop_json.read_text(encoding="utf-8", errors="replace"))
            patch_title = f"{mod.title} Compatibility Patch"
            if "title" in payload:
                payload["title"] = patch_title
            elif "name" in payload:
                payload["name"] = patch_title
            workshop_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self._log(f"Updated workshop.json title for {mod.display_name}")
        except Exception as exc:
            self._log(f"Warning: failed to update workshop.json for {mod.display_name}: {exc}")

    def _load_id_pool(self) -> None:
        self._log("Loading available ID pool from CSV")
        self.id_manager.load_from_csv(self.csv_path)
        self._emit_progress(0, 100, "Loaded ID availability")
        existing_ids = self._collect_existing_ids()
        self.id_manager.mark_existing_ids(existing_ids)
        self._log(self.id_manager.summary())

    def _collect_existing_ids(self) -> Set[int]:
        scanner = DatAssetScanner(self.workshop_root)
        ids = scanner.collect_used_ids()
        self._log(f"Collected {len(ids)} existing IDs from workshop library")
        return ids

    def _process_conflicts(self, conflicts: List[Conflict]) -> None:
        total = len(conflicts)
        for index, conflict in enumerate(self._deduplicate_conflicts(conflicts), start=1):
            self._emit_progress(index, total + 10, f"Processing conflict {index}/{total}")
            self._resolve_conflict(conflict)

    def _deduplicate_conflicts(self, conflicts: List[Conflict]) -> List[Conflict]:
        seen: Set[Tuple[str, str, int, str]] = set()
        unique: List[Conflict] = []
        for conflict in conflicts:
            key = (conflict.source_workshop_id, conflict.guid, conflict.legacy_id, conflict.asset_name)
            if key in seen:
                continue
            seen.add(key)
            unique.append(conflict)
        return unique

    def _resolve_conflict(self, conflict: Conflict) -> None:
        selected_workshop_id = None
        patch_side = None
        if conflict.source_workshop_id in self.mod_map:
            selected_workshop_id = conflict.source_workshop_id
            patch_side = "source"
        elif conflict.existing_owner_workshop_id in self.mod_map:
            selected_workshop_id = conflict.existing_owner_workshop_id
            patch_side = "owner"

        if not selected_workshop_id:
            self.report.add_error(
                f"Selected mod {conflict.source_workshop_id} or {conflict.existing_owner_workshop_id} is missing from mod map"
            )
            return

        mod = self.mod_map.get(selected_workshop_id)
        if not mod:
            self.report.add_error(f"Selected mod {selected_workshop_id} is missing from mod map")
            return

        scan_root = mod.patch_path or mod.path
        scanner = DatAssetScanner(scan_root)
        if patch_side == "source":
            match = scanner.find_match(conflict)
        else:
            match = scanner.find_owner_match(conflict)

        if not match:
            side = "source" if patch_side == "source" else "owner"
            self.report.add_error(
                f"Could not locate {side} asset for conflict {conflict.asset_name} ({conflict.guid}) with legacy ID {conflict.legacy_id} in {scan_root}"
            )
            return

        conflict_key = (conflict.source_workshop_id, conflict.guid, conflict.legacy_id, conflict.asset_name)
        if conflict_key in self.assignment_map:
            self._log(f"Conflict already resolved for {conflict.asset_name}; skipping duplicate entry")
            return

        try:
            new_id = self.id_manager.allocate_id()
        except ValueError as exc:
            self.report.add_error(str(exc))
            return

        assignment = AssignedID(
            workshop_id=conflict.source_workshop_id,
            source_id=conflict.legacy_id,
            target_id=new_id,
            asset_name=conflict.asset_name,
            asset_type=conflict.asset_type,
            guid=conflict.guid,
            file_path=match.asset_block.file_path,
            match_method=match.match_method,
        )
        self.report.add_assignment(assignment)
        self.assignment_map[conflict_key] = assignment
        self._log(
            f"Resolved {conflict.asset_name} ({conflict.guid}) from {conflict.legacy_id} to {new_id} in {assignment.file_path}"
        )
        if not self.dry_run:
            patched = scanner.patch_match(match, new_id)
            if not patched:
                self.report.add_error(f"Failed to patch {assignment.file_path} for ID {new_id}")
            else:
                self._log(f"Patched {assignment.file_path}")

    def _write_reports(self) -> None:
        mapping_txt = self.output_root / f"mapping_{self.timestamp}.txt"
        self.report_generator.write_mapping_text(self.report.assignments, mapping_txt)
        self._log(f"Saved mapping report to {mapping_txt}")
        if self.export_csv:
            mapping_csv = self.output_root / f"mapping_{self.timestamp}.csv"
            self.report_generator.write_mapping_csv(self.report.assignments, mapping_csv)
            self._log(f"Saved CSV mapping report to {mapping_csv}")
        if self.export_json:
            mapping_json = self.output_root / f"mapping_{self.timestamp}.json"
            self.report_generator.write_mapping_json(self.report.assignments, mapping_json)
            self._log(f"Saved JSON mapping report to {mapping_json}")
        summary_path = self.output_root / f"summary_{self.timestamp}.txt"
        self.report_generator.write_summary(self.report, summary_path)
        self._log(f"Saved summary report to {summary_path}")

    def _emit_progress(self, current: int, total: int, message: str) -> None:
        if self.progress_callback:
            self.progress_callback(current, total, message)

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"{timestamp} [INFO] {message}"
        if self.log_callback:
            self.log_callback(formatted)
        if self.logger:
            self.logger.info(message)

import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .cli import choose_directory, choose_file
from .id_manager import IDManager
from .logger import configure_logger
from .models import AssignedID, Conflict, FixReport
from .parsers import ClientLogParser, DatAssetScanner
from .report import ReportGenerator


class UnturnedIDFixer:
    def __init__(
        self,
        client_log_path: Path,
        csv_path: Path,
        workshop_root: Path,
        dry_run: bool = False,
        verbose: bool = False,
        no_backup: bool = False,
        export_csv: bool = False,
        export_json: bool = False,
        log_dir: Optional[Path] = None,
    ):
        self.client_log_path = client_log_path
        self.csv_path = csv_path
        self.workshop_root = workshop_root
        self.dry_run = dry_run
        self.verbose = verbose
        self.no_backup = no_backup
        self.export_csv = export_csv
        self.export_json = export_json
        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_dir = log_dir or Path.cwd()
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"fix_unturned_ids_{self.timestamp}.log"
        self.logger = configure_logger(self.log_file, self.verbose)
        self.report = FixReport()
        self.report_generator = ReportGenerator(self.log_dir)

    def run(self) -> FixReport:
        self.logger.info("Starting Unturned legacy ID fixer")
        conflicts = self._parse_conflicts()
        if not conflicts:
            self.logger.info("No workshop legacy ID conflicts were detected in the Client.log.")
            return self.report

        workshop_mods = self._discover_workshop_mod_directories()
        if not workshop_mods:
            raise FileNotFoundError(
                f"No workshop mod directories with .dat files were found under {self.workshop_root}"
            )
        self.logger.info(f"Found {len(workshop_mods)} workshop mods under {self.workshop_root}")

        id_manager = IDManager(min_allocatable_id=3000)
        id_manager.load_from_csv(self.csv_path)
        existing_ids = self._collect_existing_ids()
        id_manager.mark_existing_ids(existing_ids)
        self.logger.info(id_manager.summary())

        pending_modifications: Dict[str, List[AssignedID]] = {}
        files_to_backup: set[Path] = set()

        for conflict in self._deduplicate_conflicts(conflicts):
            self._resolve_conflict(conflict, workshop_mods, id_manager, pending_modifications, files_to_backup)

        if self.dry_run:
            self.logger.info("Dry-run enabled: no files will be modified.")
        if files_to_backup and not self.dry_run and not self.no_backup:
            self._backup_files(files_to_backup)
        elif self.no_backup:
            self.logger.warning("Backup step skipped by user (--no-backup).")

        for assignment in self.report.assignments:
            if self.dry_run:
                continue
            self._apply_assignment(assignment)

        self._write_reports()
        self.logger.info("Fixer run complete")
        self.logger.info(self.report.summary())
        return self.report

    def _parse_conflicts(self):
        parser = ClientLogParser()
        conflicts = parser.parse_conflicts(self.client_log_path)
        self.logger.info(f"Parsed {len(conflicts)} conflict entries from Client.log")
        return conflicts

    def _discover_workshop_mod_directories(self) -> Dict[str, Path]:
        mod_dirs: Dict[str, Path] = {}
        search_roots = [self.workshop_root]
        for directory in self.workshop_root.rglob("*"):
            if directory.is_dir() and directory.name.isdigit():
                search_roots.append(directory)
        for candidate in sorted(set(search_roots)):
            if candidate.is_dir() and candidate.name.isdigit():
                if any(candidate.rglob("*.dat")):
                    mod_dirs[candidate.name] = candidate
        return mod_dirs

    def _collect_existing_ids(self) -> set[int]:
        scanner = DatAssetScanner(self.workshop_root)
        existing = scanner.collect_used_ids()
        self.logger.info(f"Collected {len(existing)} IDs from workshop .dat files")
        return existing

    def _deduplicate_conflicts(self, conflicts: List) -> List:
        seen = set()
        unique_conflicts = []
        for conflict in conflicts:
            key = (
                conflict.source_workshop_id,
                conflict.guid,
                conflict.legacy_id,
                conflict.asset_name,
            )
            if key in seen:
                continue
            seen.add(key)
            unique_conflicts.append(conflict)
        return unique_conflicts

    def _resolve_conflict(
        self,
        conflict,
        workshop_mods: Dict[str, Path],
        id_manager: IDManager,
        pending_modifications: Dict[str, List[AssignedID]],
        files_to_backup: set[Path],
    ) -> None:
        source_mod = workshop_mods.get(conflict.source_workshop_id)
        if not source_mod:
            self.report.add_error(
                f"Source workshop mod {conflict.source_workshop_id} not found under workshop root."
            )
            return

        if conflict.existing_owner_workshop_id == conflict.source_workshop_id:
            self.report.add_warning(
                f"Conflict references the same workshop ID {conflict.source_workshop_id}; skipping."
            )
            return

        scanner = DatAssetScanner(source_mod)
        match = scanner.find_match(conflict)
        if not match:
            self._handle_no_match(conflict, source_mod)
            return

        if match.match_method == "guid" and match.asset_block.legacy_id is not None:
            if match.asset_block.legacy_id != conflict.legacy_id:
                self.report.add_skip(
                    f"Conflict for GUID {conflict.guid} already fixed in {source_mod}; current ID is {match.asset_block.legacy_id}."
                )
                return

        try:
            new_id = id_manager.allocate_id()
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
        files_to_backup.add(match.asset_block.file_path)
        pending_modifications.setdefault(conflict.source_workshop_id, []).append(assignment)
        self.logger.info(
            f"Resolved {conflict.asset_name} ({conflict.guid}) from {conflict.legacy_id} to {new_id} in {match.asset_block.file_path}"
        )

    def _handle_no_match(self, conflict, source_mod: Path) -> None:
        if any(source_mod.rglob("*.dat")):
            self.report.add_error(
                f"Could not locate asset {conflict.asset_name} ({conflict.guid}) with legacy ID {conflict.legacy_id} in {source_mod}"
            )
        else:
            self.report.add_error(
                f"Source workshop mod {source_mod} contains no .dat files to patch."
            )

    def _backup_files(self, files: Iterable[Path]) -> None:
        backup_root = Path.cwd() / f"unturned_id_fix_backup_{self.timestamp}"
        for file_path in sorted(files):
            relative = file_path.relative_to(self.workshop_root)
            dest = backup_root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dest)
            self.report.backup_files.add(dest)
        self.logger.info(f"Created backup of {len(self.report.backup_files)} files to {backup_root}")

    def _apply_assignment(self, assignment: AssignedID) -> None:
        scanner = DatAssetScanner(assignment.file_path.parent)
        conflict = Conflict(
            source_workshop_id=assignment.workshop_id,
            asset_name=assignment.asset_name,
            asset_type=assignment.asset_type,
            guid=assignment.guid,
            legacy_id=assignment.source_id,
            existing_owner_name="",
            existing_owner_type="",
            existing_owner_workshop_id=assignment.workshop_id,
        )
        match = scanner.find_match(conflict)
        if not match:
            self.report.add_error(
                f"Unable to re-locate asset for assignment in {assignment.file_path}."
            )
            return
        patched = scanner.patch_match(match, assignment.target_id)
        if not patched:
            self.report.add_error(
                f"Failed to patch file {assignment.file_path} for ID {assignment.target_id}."
            )

    def _write_reports(self) -> None:
        text_path = self.log_dir / f"mapping_{self.timestamp}.txt"
        self.report_generator.write_mapping_text(self.report.assignments, text_path)
        self.logger.info(f"Saved mapping report to {text_path}")
        if self.export_csv:
            csv_path = self.log_dir / f"mapping_{self.timestamp}.csv"
            self.report_generator.write_mapping_csv(self.report.assignments, csv_path)
            self.logger.info(f"Saved mapping CSV to {csv_path}")
        if self.export_json:
            json_path = self.log_dir / f"mapping_{self.timestamp}.json"
            self.report_generator.write_mapping_json(self.report.assignments, json_path)
            self.logger.info(f"Saved mapping JSON to {json_path}")
        summary_path = self.log_dir / f"summary_{self.timestamp}.txt"
        self.report_generator.write_summary(self.report, summary_path)
        self.logger.info(f"Saved summary report to {summary_path}")


def run_fixer(
    client_log: Optional[Path] = None,
    csv_path: Optional[Path] = None,
    workshop_dir: Optional[Path] = None,
    dry_run: bool = False,
    verbose: bool = False,
    no_backup: bool = False,
    export_csv: bool = False,
    export_json: bool = False,
    use_gui: bool = True,
) -> FixReport:
    if client_log is None:
        selected = choose_file(
            title="Select Client.log",
            filetypes=[("Log files", "*.log"), ("All files", "*.*")],
            use_gui=use_gui,
        )
        client_log = Path(selected) if selected else None
    if csv_path is None:
        selected = choose_file(
            title="Select ITEM.csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            use_gui=use_gui,
        )
        csv_path = Path(selected) if selected else None
    if workshop_dir is None:
        selected = choose_directory(
            title="Select Unturned workshop mods directory",
            use_gui=use_gui,
        )
        workshop_dir = Path(selected) if selected else None

    if not client_log or not client_log.exists():
        raise FileNotFoundError("Client.log path is required and must exist.")
    if not csv_path or not csv_path.exists():
        raise FileNotFoundError("ITEM.csv path is required and must exist.")
    if not workshop_dir or not workshop_dir.exists():
        raise FileNotFoundError("Workshop mods directory path is required and must exist.")

    fixer = UnturnedIDFixer(
        client_log_path=client_log,
        csv_path=csv_path,
        workshop_root=workshop_dir,
        dry_run=dry_run,
        verbose=verbose,
        no_backup=no_backup,
        export_csv=export_csv,
        export_json=export_json,
    )
    return fixer.run()

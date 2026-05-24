import json
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from .id_manager import IDManager
from .logger import configure_logger
from .models import AssetBlock, AssetMatch, AssignedID, Conflict, FixReport, WorkshopMod
from .parsers import ClientLogParser, DatAssetScanner
from .report import ReportGenerator

CRAFTING_ASSET_TYPE = "SDG.Unturned.CraftingAsset, Assembly-CSharp, Version=0.0.0.0, Culture=neutral, PublicKeyToken=null"
SALVAGE_CATEGORY_GUID = "7ed29f9101ae4523a3b2e389414b7bd9"


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
        game_root: Optional[Path] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ):
        self.workshop_root = workshop_root
        self.client_log_path = client_log_path
        self.csv_path = csv_path
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.selected_mods = []
        for mod in selected_mods:
            if getattr(mod, "is_virtual", False) or not mod.path or not mod.path.exists():
                if log_callback:
                    log_callback(f"Skipping non-patchable source {mod.display_name}")
                continue
            self.selected_mods.append(mod)
        self.dry_run = dry_run
        self.export_csv = export_csv
        self.export_json = export_json

        self.timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.base_output_root = output_root or Path.cwd()
        self.output_root = self.base_output_root / f"unturned_patch_{self.timestamp}"
        if self.dry_run:
            self.output_root = self.base_output_root / f"unturned_patch_dryrun_{self.timestamp}"
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.patch_root = self.output_root / "CompatibilityPatch"
        self.patch_root.mkdir(parents=True, exist_ok=True)

        self.log_file = self.output_root / f"fix_unturned_ids_{self.timestamp}.log"
        self.logger = configure_logger(self.log_file, verbose=True)
        self.report_generator = ReportGenerator(self.output_root)
        self.report = FixReport()
        self.id_manager = IDManager(min_allocatable_id=3000)
        self.game_root = game_root
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
            return self.patch_root, self.report

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
        return self.patch_root, self.report

    def _parse_conflicts(self) -> List[Conflict]:
        parser = ClientLogParser()
        return parser.parse_conflicts(self.client_log_path)

    def _prepare_patch_workspace(self, mods_to_copy: Optional[List[WorkshopMod]] = None) -> None:
        if self.dry_run:
            self._log("Skipping patch workspace setup in dry-run mode")
            for mod in self.selected_mods:
                mod.patch_path = mod.path
            return

        mods = mods_to_copy if mods_to_copy is not None else self.selected_mods
        self._log("Preparing lean compatibility patch workspace")
        for index, mod in enumerate(mods, start=1):
            mod.original_masterbundle_name = self._find_original_masterbundle_name(mod)
            mod.patch_path = self.patch_root
            self._log(f"Prepared {mod.display_name} for on-demand asset patching")
            self._emit_progress(index, len(self.selected_mods) + 5, f"Prepared {mod.display_name}")
        self._write_placeholder_masterbundle()
        self._write_patch_workshop_json(mods)

    def _write_placeholder_masterbundle(self) -> None:
        masterbundle_path = self.patch_root / "Items" / "MasterBundle.dat"
        masterbundle_path.parent.mkdir(parents=True, exist_ok=True)
        masterbundle_path.write_text(
            "Asset_Bundle_Name core.masterbundle\n"
            "Asset_Prefix Items/Supplies/Scrap_Metal\n",
            encoding="utf-8",
        )
        self._log("Wrote placeholder Items/MasterBundle.dat")

    def _get_bundle_override_path(self, asset_file: Path, mod_root: Path) -> str:
        relative_dir = asset_file.relative_to(mod_root).parent
        normalized = "/".join(relative_dir.parts)
        return normalized

    def _find_original_masterbundle_name(self, mod: WorkshopMod) -> Optional[str]:
        if not mod.path.exists():
            return None
        candidates = [
            path for path in mod.path.rglob("*.masterbundle") if path.is_file()
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda path: (len(path.relative_to(mod.path).parts), path.name.lower()))
        return candidates[0].name

    def _write_patch_workshop_json(self, mods: List[WorkshopMod]) -> None:
        workshop_json = self.patch_root / "workshop.json"
        title = "Compatibility Patch"
        if len(mods) == 1:
            title = f"{mods[0].title} Compatibility Patch"
        elif mods:
            title = f"{len(mods)} Mod Compatibility Patch"
        try:
            payload = {}
            for mod in mods:
                candidate = mod.path / "workshop.json"
                if candidate.exists():
                    payload = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
                    break
            if not isinstance(payload, dict):
                payload = {}
            if "title" in payload or "name" not in payload:
                payload["title"] = title
            else:
                payload["name"] = title
            workshop_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self._log("Wrote merged patch workshop.json")
        except Exception as exc:
            self._log(f"Warning: failed to write merged workshop.json: {exc}")

    def _load_id_pool(self) -> None:
        self._log("Loading available ID pool from CSV")
        self.id_manager.load_from_csv(self.csv_path)
        self._emit_progress(0, 100, "Loaded ID availability")
        self._log("Scanning existing IDs from patch workspace and game bundles")
        self._emit_progress(0, 100, "Scanning existing IDs")
        existing_ids = self._collect_existing_ids()
        self.id_manager.mark_existing_ids(existing_ids)
        self._log(self.id_manager.summary())

    def _collect_existing_ids(self) -> Set[int]:
        ids: Set[int] = set()
        scan_roots: List[Path] = []
        if self.patch_root.exists():
            scan_roots.append(self.patch_root)
        scan_roots.extend(
            mod.path
            for mod in self.selected_mods
            if mod.path and mod.path.exists()
        )
        if self.game_root and self.game_root.exists():
            bundles_root = self.game_root / "Bundles"
            scan_roots.append(bundles_root if bundles_root.exists() else self.game_root)

        for index, scan_root in enumerate(scan_roots, start=1):
            self._log(f"Scanning existing IDs in {scan_root}")
            self._emit_progress(index, len(scan_roots) + 1, f"Scanning IDs in {scan_root.name}")
            scanner = DatAssetScanner(scan_root)
            ids.update(scanner.collect_used_ids())
        self._log(f"Collected {len(ids)} existing IDs from patch workspace and game content")
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

        scan_root = mod.path
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
        self.assignment_map[conflict_key] = assignment
        self._log(
            f"Resolved {conflict.asset_name} ({conflict.guid}) from {conflict.legacy_id} to {new_id} in {assignment.file_path}"
        )
        if self.dry_run:
            self.report.add_assignment(assignment)
        else:
            patch_file = self._copy_asset_file_to_patch(match.asset_block.file_path, mod.path, mod)
            patch_scanner = DatAssetScanner(patch_file.parent)
            patch_match = self._find_match_in_file(patch_scanner, patch_file, conflict, patch_side)
            if not patch_match:
                self.report.add_error(f"Failed to locate copied patch asset {patch_file} for ID {new_id}")
                return
            bundle_override_path = self._get_bundle_override_path(match.asset_block.file_path, mod.path)
            bundle_override_master = mod.original_masterbundle_name or "original.masterbundle"
            patched_path = patch_scanner.patch_match(
                patch_match,
                new_id,
                make_override=True,
                bundle_override_path=bundle_override_path,
                bundle_override_master=bundle_override_master,
            )
            if not patched_path:
                self.report.add_error(f"Failed to patch {patch_file} for ID {new_id}")
            else:
                assignment.file_path = patched_path
                self.report.add_assignment(assignment)
                self._log(f"Patched {patched_path}")

    def _copy_asset_file_to_patch(self, asset_file: Path, mod_root: Path, mod: WorkshopMod) -> Path:
        try:
            relative = asset_file.relative_to(mod_root)
        except ValueError:
            relative = Path(asset_file.parent.name) / asset_file.name
        destination = self.patch_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self._log(f"Using existing copied patch asset {destination.relative_to(self.patch_root)}")
            self._copy_localization_file(asset_file, destination)
            self._migrate_generated_salvage_blueprints(destination)
            return destination
        shutil.copy2(asset_file, destination)
        self._copy_localization_file(asset_file, destination)
        self._migrate_generated_salvage_blueprints(destination)
        self._log(f"Copied conflicted asset {relative} from {mod.display_name}")
        return destination

    def _migrate_generated_salvage_blueprints(self, dat_path: Path) -> None:
        try:
            lines = dat_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except Exception:
            return
        cleaned, migrated_count = self._migrate_modern_salvage_blocks(lines, dat_path)
        if cleaned != lines:
            dat_path.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
            self._log(
                f"Migrated {migrated_count} generated salvage blueprint(s) from {dat_path.relative_to(self.patch_root)}"
            )

    def _migrate_modern_salvage_blocks(self, lines: List[str], source_dat_path: Path) -> Tuple[List[str], int]:
        cleaned: List[str] = []
        migrated_count = 0
        index = 0
        while index < len(lines):
            if lines[index].strip() == "{":
                end_index = self._find_modern_block_end(lines, index)
                block = lines[index : end_index + 1] if end_index is not None else [lines[index]]
                block_text = "\n".join(block)
                if self._is_generated_salvage_block(block_text):
                    self._write_migrated_crafting_asset(source_dat_path, block, migrated_count)
                    migrated_count += 1
                    index = (end_index + 1) if end_index is not None else index + 1
                    continue
            cleaned.append(lines[index])
            index += 1
        return cleaned, migrated_count

    def _write_migrated_crafting_asset(self, source_dat_path: Path, block: List[str], sequence: int) -> None:
        patch_name = self._sanitize_patch_asset_name(f"{source_dat_path.parent.name}_{source_dat_path.stem}_Salvage")
        if sequence:
            patch_name = f"{patch_name}_{sequence + 1}"
        patch_dir = self.patch_root / "Crafting" / patch_name
        patch_dir.mkdir(parents=True, exist_ok=True)
        source_guid = self._read_asset_guid(source_dat_path)
        block = self._normalize_migrated_crafting_block(block, source_guid)
        dat_path = patch_dir / f"{patch_name}.asset"
        recipe_guid = secrets.token_hex(16)
        dat_path.write_text(
            "\n".join(
                [
                    '"Metadata"',
                    "{",
                    f'\t"Type" "{CRAFTING_ASSET_TYPE}"',
                    f'\t"GUID" "{recipe_guid}"',
                    "}",
                    '"Asset"',
                    "{",
                    "\tBlueprints",
                    "\t[",
                    *self._indent_crafting_block(block),
                    "\t]",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self._log(f"Wrote migrated CraftingAsset salvage recipe {dat_path.relative_to(self.patch_root)}")

    def _indent_crafting_block(self, block: List[str]) -> List[str]:
        return [f"\t\t{line.lstrip()}" if line.strip() else line for line in block]

    def _sanitize_patch_asset_name(self, value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
        return safe or "MigratedSalvage"

    def _is_generated_salvage_block(self, block_text: str) -> bool:
        if SALVAGE_CATEGORY_GUID not in block_text:
            return False
        generated_markers = (
            "Native Salvage Tab GUID",
            "YOUR ADDED SALVAGE BLUEPRINT",
            "Build 28",
        )
        return any(marker in block_text for marker in generated_markers)

    def _normalize_migrated_crafting_block(self, block: List[str], source_guid: str) -> List[str]:
        if not block:
            return [
                "\t{",
                f"\t\tCategoryTag \"{SALVAGE_CATEGORY_GUID}\" // Native Salvage Tab GUID",
                "\t}",
            ]
        result = list(block)
        if source_guid:
            result = [self._replace_this_asset_pointer(line, source_guid) for line in result]
        result = [
            line
            for line in result
            if not re.match(r"^\s*(Type|Build)\s+\S+", line)
        ]
        if not any("CategoryTag" in line for line in result):
            insert_index = 1 if result[0].strip() == "{" else 0
            result.insert(insert_index, f"\t\tCategoryTag \"{SALVAGE_CATEGORY_GUID}\" // Native Salvage Tab GUID")
        return result

    def _replace_this_asset_pointer(self, line: str, source_guid: str) -> str:
        line = re.sub(
            r'(?i)(InputItems|OutputItems)(\s+)"?this"?(\s+x\s+\d+)?',
            lambda match: f'{match.group(1)}{match.group(2)}"{source_guid}{match.group(3) or ""}"',
            line,
        )
        return re.sub(
            r'(?i)"this(\s+x\s+\d+)?"',
            lambda match: f'"{source_guid}{match.group(1) or ""}"',
            line,
        )

    def _read_asset_guid(self, dat_path: Path) -> str:
        try:
            for line in dat_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                match = re.match(r'^\s*"?GUID"?\s+"?([0-9A-Fa-f]{32})"?', line)
                if match:
                    return match.group(1)
        except Exception:
            return ""
        return ""

    def _find_modern_block_end(self, lines: List[str], start_index: int) -> Optional[int]:
        depth = 0
        for index in range(start_index, len(lines)):
            stripped = lines[index].strip()
            if stripped == "{":
                depth += 1
            elif stripped == "}":
                depth -= 1
                if depth == 0:
                    return index
        return None

    def _copy_localization_file(self, source_asset_file: Path, destination_asset_file: Path) -> None:
        source_english = self._find_localization_file(source_asset_file.parent)
        if not source_english:
            return
        destination_english = destination_asset_file.parent / source_english.name
        if destination_english.exists():
            return
        shutil.copy2(source_english, destination_english)
        self._log(f"Copied localization file {source_english.name} for {destination_asset_file.relative_to(self.patch_root)}")

    def _find_localization_file(self, folder: Path) -> Optional[Path]:
        for name in ("English.dat", "english.dat"):
            candidate = folder / name
            if candidate.exists():
                return candidate
        for candidate in folder.glob("*.dat"):
            if candidate.name.lower() == "english.dat":
                return candidate
        return None

    def _find_match_in_file(
        self,
        scanner: DatAssetScanner,
        dat_path: Path,
        conflict: Conflict,
        patch_side: str,
    ) -> Optional[AssetMatch]:
        candidates: List[Tuple[int, AssetBlock, str]] = []
        for block in scanner._parse_asset_file(dat_path):
            if patch_side == "source":
                score = scanner._match_score(block, conflict)
                method = scanner._match_method(block, conflict)
            else:
                score = scanner._match_owner_score(block, conflict)
                method = "legacy_id"
            if score > 0:
                candidates.append((score, block, method))
        if not candidates:
            return None
        candidates.sort(key=lambda item: -item[0])
        _, block, method = candidates[0]
        return AssetMatch(conflict=conflict, asset_block=block, match_method=method)

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

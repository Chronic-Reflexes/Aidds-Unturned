import re
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from .models import AssetBlock, AssetMatch, Conflict

ID_LINE_PATTERN = re.compile(r'^(?P<indent>\s*)(?P<key>ID|Item_ID|Vehicle_ID|Object_ID|Animal_ID)\s+(?P<value>\d+)(?P<suffix>\s*)$')
GUID_LINE_PATTERN = re.compile(r'^(?P<indent>\s*)(?P<key>GUID|Asset_GUID|Pack_GUID|Plugin_GUID|AssetGUID|guid)\s+(?P<value>.+?)\s*$')
NAME_LINE_PATTERN = re.compile(r'^(?P<indent>\s*)(?P<key>Name|name|Item_Name|Asset_Name|itemName|assetName)\s+(?P<value>.+?)\s*$')
KEY_VALUE_PATTERN = re.compile(r'^(?P<indent>\s*)(?P<key>[A-Za-z0-9_]+)\s+(?P<value>.+?)\s*$')


class ClientLogParser:
    CONFLICT_PATTERN = re.compile(
        r"Workshop File \((?P<source_id>\d+)\)\s+"
        r"(?P<asset_name>.+?)\s+\((?P<asset_type>[^)]+)\)\s+\[(?P<guid>[^\]]+)\]:\s+"
        r"legacy ID (?P<legacy_id>\d+) already taken by\s+"
        r"(?P<owner_name>.+?)\s+\((?P<owner_type>[^)]+)\)\s+in\s+Workshop File \((?P<owner_id>\d+)\)!"
    )

    def parse_conflicts(self, log_path: Path) -> List[Conflict]:
        conflicts: List[Conflict] = []
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                match = self.CONFLICT_PATTERN.search(line)
                if not match:
                    continue
                conflicts.append(
                    Conflict(
                        source_workshop_id=match.group("source_id"),
                        asset_name=match.group("asset_name").strip(),
                        asset_type=match.group("asset_type").strip(),
                        guid=match.group("guid").strip(),
                        legacy_id=int(match.group("legacy_id")),
                        existing_owner_name=match.group("owner_name").strip(),
                        existing_owner_type=match.group("owner_type").strip(),
                        existing_owner_workshop_id=match.group("owner_id"),
                    )
                )
        return conflicts


class DatAssetScanner:
    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.asset_files: Dict[Path, List[AssetBlock]] = {}

    def discover_dat_files(self) -> List[Path]:
        return sorted(self.root_path.rglob("*.dat"))

    def scan_all_files(self) -> None:
        for dat_path in self.discover_dat_files():
            self.asset_files[dat_path] = self._parse_asset_file(dat_path)

    def collect_used_ids(self) -> set[int]:
        used_ids: set[int] = set()
        for dat_path in self.discover_dat_files():
            with dat_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("//") or line.startswith("#"):
                        continue
                    match = ID_LINE_PATTERN.match(line)
                    if match:
                        used_ids.add(int(match.group("value")))
        return used_ids

    def find_match(self, conflict: Conflict) -> Optional[AssetMatch]:
        self.scan_all_files()
        candidates: List[Tuple[int, AssetBlock, str]] = []
        for blocks in self.asset_files.values():
            for block in blocks:
                score = self._match_score(block, conflict)
                if score > 0:
                    candidate = (score, block, self._match_method(block, conflict))
                    candidates.append(candidate)
        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0], item[1].file_path.as_posix()))
        score, block, method = candidates[0]
        return AssetMatch(conflict=conflict, asset_block=block, match_method=method)

    def find_owner_match(self, conflict: Conflict) -> Optional[AssetMatch]:
        self.scan_all_files()
        candidates: List[Tuple[int, AssetBlock, str]] = []
        for blocks in self.asset_files.values():
            for block in blocks:
                score = self._match_owner_score(block, conflict)
                if score > 0:
                    candidate = (score, block, "legacy_id")
                    candidates.append(candidate)
        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0], item[1].file_path.as_posix()))
        score, block, method = candidates[0]
        return AssetMatch(conflict=conflict, asset_block=block, match_method=method)

    def _match_score(self, block: AssetBlock, conflict: Conflict) -> int:
        if block.guid and conflict.guid and block.guid.lower() == conflict.guid.lower():
            return 3
        if block.legacy_id == conflict.legacy_id:
            return 2
        if block.name and block.name.lower() == conflict.asset_name.lower():
            return 1
        return 0

    def _match_owner_score(self, block: AssetBlock, conflict: Conflict) -> int:
        score = 0
        if block.legacy_id == conflict.legacy_id:
            score += 4
        if block.name and conflict.existing_owner_name and block.name.lower() == conflict.existing_owner_name.lower():
            score += 2
        if block.asset_type and conflict.existing_owner_type and block.asset_type.lower() == conflict.existing_owner_type.lower():
            score += 1
        return score

    def _match_method(self, block: AssetBlock, conflict: Conflict) -> str:
        if block.guid and conflict.guid and block.guid.lower() == conflict.guid.lower():
            return "guid"
        if block.legacy_id == conflict.legacy_id:
            return "legacy_id"
        return "name"

    def _parse_asset_file(self, dat_path: Path) -> List[AssetBlock]:
        raw_text = dat_path.read_text(encoding="utf-8-sig", errors="replace")
        lines = raw_text.splitlines(keepends=True)
        blocks: List[AssetBlock] = []
        current_indices: List[int] = []
        current_lines: List[str] = []
        start_index = 0
        for index, line in enumerate(lines):
            if line.strip() == "":
                if current_lines:
                    blocks.append(self._build_block(dat_path, current_indices, current_lines))
                    current_lines = []
                    current_indices = []
                start_index = index + 1
                continue
            current_indices.append(index)
            current_lines.append(line)
        if current_lines:
            blocks.append(self._build_block(dat_path, current_indices, current_lines))
        return blocks

    def _build_block(self, file_path: Path, line_indices: List[int], lines: List[str]) -> AssetBlock:
        block = AssetBlock(file_path=file_path, line_indices=line_indices, lines=lines)
        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("//") or stripped.startswith("#"):
                continue
            id_match = ID_LINE_PATTERN.match(stripped)
            if id_match and block.legacy_id is None:
                block.legacy_id = int(id_match.group("value"))
                block.id_key = id_match.group("key")
                continue
            guid_match = GUID_LINE_PATTERN.match(stripped)
            if guid_match and block.guid is None:
                block.guid = guid_match.group("value").strip()
                continue
            name_match = NAME_LINE_PATTERN.match(stripped)
            if name_match and block.name is None:
                block.name = name_match.group("value").strip()
                continue
            kv_match = KEY_VALUE_PATTERN.match(stripped)
            if kv_match and block.asset_type is None:
                key = kv_match.group("key").strip().lower()
                value = kv_match.group("value").strip()
                if key in {"type", "asset_type", "category", "item_type"}:
                    block.asset_type = value
        return block

    def patch_match(
        self,
        match: AssetMatch,
        new_value: int,
        make_override: bool = False,
        bundle_override_path: Optional[str] = None,
        bundle_override_master: str = "original.masterbundle",
    ) -> Optional[Path]:
        block = match.asset_block
        raw_text = block.file_path.read_text(encoding="utf-8-sig", errors="replace")
        lines = raw_text.splitlines(keepends=True)
        applied = False
        updated_lines: List[str] = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            if index in block.line_indices:
                if stripped.startswith("//") or stripped.startswith("#"):
                    updated_lines.append(line)
                    continue
                guid_match = GUID_LINE_PATTERN.match(stripped)
                if guid_match:
                    # remove GUID lines entirely for override patch content
                    applied = True
                    continue
                id_match = ID_LINE_PATTERN.match(stripped)
                if id_match:
                    current_id = int(id_match.group("value"))
                    if current_id == match.conflict.legacy_id:
                        indent = id_match.group("indent") or ""
                        key = id_match.group("key")
                        suffix = id_match.group("suffix") or ""
                        updated_lines.append(f"{indent}{key} {new_value}{suffix}\n")
                        applied = True
                        continue
            updated_lines.append(line)

        if not applied:
            return None

        if make_override and bundle_override_path:
            override_lines = [
                f"Master_Bundle_Override {bundle_override_master}\n",
                f"Bundle_Override_Path {bundle_override_path}\n",
            ]
            inserted = False
            result_lines: List[str] = []
            for line in updated_lines:
                stripped = line.strip()
                if not inserted and stripped and not stripped.startswith("//") and not stripped.startswith("#"):
                    result_lines.extend(override_lines)
                    inserted = True
                result_lines.append(line)
            if not inserted:
                result_lines.extend(override_lines)
            updated_lines = result_lines

        target_path = block.file_path
        if make_override:
            target_path.write_text("".join(updated_lines), encoding="utf-8-sig")
            return target_path

        target_path.write_text("".join(updated_lines), encoding="utf-8-sig")
        return target_path

    def has_asset_with_id(self, file_path: Path, legacy_id: int) -> bool:
        with file_path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("//") or line.startswith("#"):
                    continue
                match = ID_LINE_PATTERN.match(line)
                if match and int(match.group("value")) == legacy_id:
                    return True
        return False

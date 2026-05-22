from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set


@dataclass
class WorkshopMod:
    workshop_id: str
    path: Optional[Path]
    title: str
    display_name: str
    selected: bool = True
    patch_path: Optional[Path] = None
    original_masterbundle_name: Optional[str] = None
    is_virtual: bool = False
    hidden: bool = False


@dataclass
class Conflict:
    source_workshop_id: str
    asset_name: str
    asset_type: str
    guid: str
    legacy_id: int
    existing_owner_name: str
    existing_owner_type: str
    existing_owner_workshop_id: str
    new_id: Optional[int] = None
    resolution: Optional[str] = None
    note: Optional[str] = None


@dataclass
class AssetBlock:
    file_path: Path
    line_indices: List[int]
    lines: List[str]
    guid: Optional[str] = None
    legacy_id: Optional[int] = None
    id_key: Optional[str] = None
    name: Optional[str] = None
    asset_type: Optional[str] = None

    def describe(self) -> str:
        return (
            f"{self.file_path.name}: name={self.name!r} guid={self.guid!r} "
            f"legacy_id={self.legacy_id!r} type={self.asset_type!r}"
        )


@dataclass
class AssetMatch:
    conflict: Conflict
    asset_block: AssetBlock
    match_method: str


@dataclass
class AssignedID:
    workshop_id: str
    source_id: int
    target_id: int
    asset_name: str
    asset_type: str
    guid: str
    file_path: Path
    match_method: str


@dataclass
class FixReport:
    assignments: List[AssignedID] = field(default_factory=list)
    modified_files: Set[Path] = field(default_factory=set)
    backup_files: Set[Path] = field(default_factory=set)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    skipped_conflicts: List[str] = field(default_factory=list)

    def add_assignment(self, assignment: AssignedID) -> None:
        self.assignments.append(assignment)
        self.modified_files.add(assignment.file_path)

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def add_skip(self, message: str) -> None:
        self.skipped_conflicts.append(message)

    def summary(self) -> str:
        lines = [
            f"Assignments: {len(self.assignments)}",
            f"Modified files: {len(self.modified_files)}",
            f"Backed up files: {len(self.backup_files)}",
            f"Warnings: {len(self.warnings)}",
            f"Errors: {len(self.errors)}",
            f"Skipped conflicts: {len(self.skipped_conflicts)}",
        ]
        return "\n".join(lines)

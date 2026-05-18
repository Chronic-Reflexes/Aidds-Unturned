import csv
import json
from pathlib import Path
from typing import Iterable

from .models import AssignedID, FixReport


class ReportGenerator:
    def __init__(self, export_dir: Path):
        self.export_dir = export_dir
        self.export_dir.mkdir(parents=True, exist_ok=True)

    def write_mapping_text(self, assignments: Iterable[AssignedID], path: Path) -> None:
        lines = [f"{assignment.source_id} -> {assignment.target_id}" for assignment in assignments]
        path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def write_mapping_csv(self, assignments: Iterable[AssignedID], path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["WorkshopID", "AssetName", "AssetType", "SourceID", "TargetID", "FilePath", "MatchMethod"])
            for assignment in assignments:
                writer.writerow(
                    [
                        assignment.workshop_id,
                        assignment.asset_name,
                        assignment.asset_type,
                        assignment.source_id,
                        assignment.target_id,
                        str(assignment.file_path),
                        assignment.match_method,
                    ]
                )

    def write_mapping_json(self, assignments: Iterable[AssignedID], path: Path) -> None:
        records = [
            {
                "workshop_id": assignment.workshop_id,
                "asset_name": assignment.asset_name,
                "asset_type": assignment.asset_type,
                "source_id": assignment.source_id,
                "target_id": assignment.target_id,
                "file_path": str(assignment.file_path),
                "match_method": assignment.match_method,
            }
            for assignment in assignments
        ]
        path.write_text(json.dumps(records, indent=2), encoding="utf-8")

    def write_summary(self, report: FixReport, path: Path) -> None:
        lines = [
            "Unturned legacy ID fixer summary",
            "=================================",
            report.summary(),
            "",
            "Assignments:",
        ]
        for assignment in report.assignments:
            lines.append(
                f"{assignment.workshop_id}: {assignment.source_id} -> {assignment.target_id} "
                f"({assignment.asset_name}, {assignment.asset_type}) in {assignment.file_path}"
            )
        if report.warnings:
            lines.append("\nWarnings:")
            lines.extend(report.warnings)
        if report.errors:
            lines.append("\nErrors:")
            lines.extend(report.errors)
        if report.skipped_conflicts:
            lines.append("\nSkipped conflicts:")
            lines.extend(report.skipped_conflicts)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

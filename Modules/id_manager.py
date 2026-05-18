import csv
import heapq
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set


class IDManager:
    def __init__(self, reserve_reserved_ids: bool = True, min_allocatable_id: int = 3000):
        self.available_heap: List[int] = []
        self.used_ids: Set[int] = set()
        self.reserved_ids: Set[int] = set()
        self.reserve_reserved_ids = reserve_reserved_ids
        self.min_allocatable_id = min_allocatable_id

    def load_from_csv(self, csv_path: Path) -> None:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            sample = handle.read(8192)
            handle.seek(0)
            dialect = self._detect_dialect(sample)
            reader = csv.DictReader(handle, dialect=dialect)
            if not reader.fieldnames:
                raise ValueError("CSV file could not be parsed because headers were missing.")
            names = [name.lower() for name in reader.fieldnames if name]
            for row in reader:
                self._load_csv_row(row, names)
        heapq.heapify(self.available_heap)

    def _detect_dialect(self, sample: str) -> csv.Dialect:
        try:
            return csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            class FallbackDialect(csv.excel):
                delimiter = ","
            return FallbackDialect()

    def _load_csv_row(self, row: dict, names: List[str]) -> None:
        if not row:
            return
        legacy_id_str = None
        used_by = None
        reserved = None
        for key, value in row.items():
            if value is None:
                continue
            normalized = key.strip().lower()
            if "legacy" in normalized and legacy_id_str is None:
                legacy_id_str = value.strip()
            elif "used" in normalized and used_by is None:
                used_by = value.strip()
            elif "reserved" in normalized and reserved is None:
                reserved = value.strip()
        if not legacy_id_str:
            return
        try:
            legacy_id = int(legacy_id_str)
        except ValueError:
            return
        if self._row_is_available(used_by, reserved):
            self.available_heap.append(legacy_id)
        else:
            self.reserved_ids.add(legacy_id)

    @staticmethod
    def _row_is_available(used_by: Optional[str], reserved: Optional[str]) -> bool:
        if reserved and reserved.strip():
            reserved_normalized = reserved.strip().lower()
            if reserved_normalized in {
                "reserved",
                "reserved for vanilla",
                "vanilla",
                "yes",
                "true",
                "used",
            }:
                return False
        if not used_by or not used_by.strip():
            return True
        normalized = used_by.strip().lower()
        return normalized in {
            "",
            "none",
            "available",
            "unused",
            "---",
            "free",
            "unassigned",
            "unowned",
        }

    def mark_existing_ids(self, ids: Iterable[int]) -> None:
        for value in ids:
            self.used_ids.add(value)

    def allocate_id(self) -> int:
        while self.available_heap:
            candidate = heapq.heappop(self.available_heap)
            if candidate < self.min_allocatable_id:
                continue
            if candidate in self.used_ids:
                continue
            if self.reserve_reserved_ids and candidate in self.reserved_ids:
                continue
            self.used_ids.add(candidate)
            return candidate
        raise ValueError(
            f"No available IDs remain in the pool at or above {self.min_allocatable_id}."
        )

    def get_unused_available_ids(self) -> Iterator[int]:
        for candidate in sorted(set(self.available_heap)):
            if candidate < self.min_allocatable_id:
                continue
            if candidate not in self.used_ids and (not self.reserve_reserved_ids or candidate not in self.reserved_ids):
                yield candidate

    def summary(self) -> str:
        available = len([x for x in set(self.available_heap) if x not in self.used_ids])
        return f"Available IDs: {available}, Used IDs: {len(self.used_ids)}"

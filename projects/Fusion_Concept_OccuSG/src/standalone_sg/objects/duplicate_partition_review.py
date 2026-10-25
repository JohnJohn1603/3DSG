"""Compare same-frame duplicate partitions independently of canonical IDs."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class PartitionDelta:
    frame_index: int
    frame_id: str
    kind: str
    old_groups: tuple[tuple[str, ...], ...]
    candidate_groups: tuple[tuple[str, ...], ...]
    old_canonical_ids: tuple[str, ...]
    candidate_canonical_ids: tuple[str, ...]


def _groups(rows: Iterable[dict]) -> dict[tuple[int, str], dict[frozenset[str], str]]:
    grouped: dict[tuple[int, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in rows:
        frame = (int(row["frame_index"]), str(row["frame_id"]))
        observation = str(row["detection_id"])
        canonical = str(row.get("duplicate_of") or observation)
        grouped[frame][canonical].add(observation)
    return {
        frame: {frozenset(members): canonical for canonical, members in roots.items()}
        for frame, roots in grouped.items()
    }


def compare_duplicate_partitions(old_rows: Iterable[dict], candidate_rows: Iterable[dict]) -> list[PartitionDelta]:
    """Return group-level differences; equal memberships ignore root/alias direction."""
    old, candidate = _groups(old_rows), _groups(candidate_rows)
    result: list[PartitionDelta] = []
    for frame in sorted(set(old) | set(candidate)):
        old_map, new_map = old.get(frame, {}), candidate.get(frame, {})
        old_remaining, new_remaining = set(old_map), set(new_map)

        # Exact same membership is invariant to canonical selection. Report a
        # separate delta only when the representative changed.
        for members in sorted(old_remaining & new_remaining, key=lambda x: tuple(sorted(x))):
            old_remaining.remove(members)
            new_remaining.remove(members)
            if old_map[members] != new_map[members]:
                result.append(PartitionDelta(
                    frame[0], frame[1], "canonical_only",
                    (tuple(sorted(members)),), (tuple(sorted(members)),),
                    (old_map[members],), (new_map[members],),
                ))

        # Build connected components in the old/new overlap graph so a split,
        # expansion, or repartition is recorded once as one review case.
        while old_remaining or new_remaining:
            side = "old" if old_remaining else "new"
            seed = min(old_remaining if side == "old" else new_remaining, key=lambda x: tuple(sorted(x)))
            pending = [(side, seed)]
            old_component: set[frozenset[str]] = set()
            new_component: set[frozenset[str]] = set()
            while pending:
                current_side, members = pending.pop()
                if current_side == "old":
                    if members not in old_remaining or members in old_component:
                        continue
                    old_component.add(members)
                    pending.extend(("new", other) for other in new_remaining if members & other)
                else:
                    if members not in new_remaining or members in new_component:
                        continue
                    new_component.add(members)
                    pending.extend(("old", other) for other in old_remaining if members & other)
            old_remaining -= old_component
            new_remaining -= new_component
            old_union = set().union(*(set(x) for x in old_component)) if old_component else set()
            new_union = set().union(*(set(x) for x in new_component)) if new_component else set()
            if old_component and new_component and old_union == new_union:
                if len(old_component) > 1 and len(new_component) == 1:
                    kind = "membership_expansion_or_merge"
                elif len(old_component) == 1 and len(new_component) > 1:
                    kind = "split_or_released"
                else:
                    kind = "membership_repartition"
            elif old_component and not new_component:
                kind = "split_or_released"
            elif not old_component and new_component:
                kind = "membership_expansion_or_merge"
            else:
                kind = "membership_repartition"
            old_groups = tuple(sorted((tuple(sorted(x)) for x in old_component)))
            new_groups = tuple(sorted((tuple(sorted(x)) for x in new_component)))
            result.append(PartitionDelta(
                frame[0], frame[1], kind, old_groups, new_groups,
                tuple(sorted(old_map[x] for x in old_component)),
                tuple(sorted(new_map[x] for x in new_component)),
            ))
    return result

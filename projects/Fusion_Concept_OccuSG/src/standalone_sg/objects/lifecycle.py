"""Versioned object lifecycle event and active-snapshot contracts for M9/M10."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Literal, Mapping

from ..contracts.errors import ContractError


LifecycleKind = Literal["create", "update", "canonical_alias", "retire"]


@dataclass(frozen=True, slots=True)
class ObjectBackendCapabilities:
    protocol_version: int
    event_kinds: tuple[LifecycleKind, ...]
    backend_name: str = "unspecified_backend"
    snapshot_semantics: Literal["active_canonical_objects"] = "active_canonical_objects"
    history_semantics: Literal["incremental_events"] = "incremental_events"

    def __post_init__(self) -> None:
        if self.protocol_version != 1:
            raise ContractError("unsupported object lifecycle protocol version")
        if type(self.backend_name) is not str or not self.backend_name.strip():
            raise ContractError("object lifecycle backend_name must be a non-empty string")
        if len(set(self.event_kinds)) != len(self.event_kinds):
            raise ContractError("object lifecycle capabilities contain duplicate event kinds")
        if any(kind not in {"create", "update", "canonical_alias", "retire"} for kind in self.event_kinds):
            raise ContractError("object lifecycle capabilities contain an unknown event kind")
        if self.snapshot_semantics != "active_canonical_objects":
            raise ContractError("object backend snapshot must represent active canonical objects")
        if self.history_semantics != "incremental_events":
            raise ContractError("object backend history must be emitted as incremental events")


@dataclass(frozen=True, slots=True)
class ObjectLifecycleEvent:
    schema_version: int
    event_id: str
    kind: LifecycleKind
    object_id: str
    frame_id: str
    timestamp_s: float
    causal_revision: int
    provenance: str
    evidence_ids: tuple[str, ...] = ()
    target_id: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ContractError("unsupported object lifecycle event schema")
        for name in ("event_id", "object_id", "frame_id", "provenance"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ContractError(f"object lifecycle event {name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if self.kind not in {"create", "update", "canonical_alias", "retire"}:
            raise ContractError("unknown object lifecycle event kind")
        if isinstance(self.timestamp_s, bool) or not isinstance(self.timestamp_s, (int, float)) or not math.isfinite(float(self.timestamp_s)):
            raise ContractError("object lifecycle event timestamp must be finite")
        object.__setattr__(self, "timestamp_s", float(self.timestamp_s))
        if type(self.causal_revision) is not int or self.causal_revision < 1:
            raise ContractError("object lifecycle causal_revision must be a positive integer")
        evidence = tuple(self.evidence_ids)
        if any(type(item) is not str or not item.strip() for item in evidence):
            raise ContractError("object lifecycle evidence IDs must be non-empty strings")
        if len(set(evidence)) != len(evidence):
            raise ContractError("object lifecycle evidence IDs must be unique")
        object.__setattr__(self, "evidence_ids", tuple(item.strip() for item in evidence))
        if self.target_id is not None:
            if type(self.target_id) is not str or not self.target_id.strip():
                raise ContractError("object lifecycle target_id must be a non-empty string or None")
            object.__setattr__(self, "target_id", self.target_id.strip())
        if (self.kind == "canonical_alias") != (self.target_id is not None):
            raise ContractError("only canonical_alias events must specify target_id")
        if self.kind in {"create", "update"} and not self.evidence_ids:
            raise ContractError("create/update lifecycle events require observation evidence IDs")

    def to_record(self) -> dict:
        record = asdict(self)
        record["evidence_ids"] = list(self.evidence_ids)
        return record


SUPPORTED_OBJECT_LIFECYCLE_SCHEMA = 1


def empty_object_lifecycle_state() -> dict:
    return {
        "schema_version": SUPPORTED_OBJECT_LIFECYCLE_SCHEMA,
        "protocol_version": 1,
        "backend_name": None,
        "supported_event_kinds": [],
        "snapshot_semantics": "active_canonical_objects",
        "history_semantics": "append_only_events",
        "causal_revision": 0,
        "active_canonical_ids": [],
        "aliases": {},
        "retired_ids": [],
        "event_ids": [],
        "events": [],
    }


def validate_object_lifecycle_state(state: Mapping[str, object]) -> dict:
    if not isinstance(state, Mapping):
        raise ContractError("object_lifecycle must be a mapping")
    required = {"schema_version", "protocol_version", "backend_name", "supported_event_kinds",
                "snapshot_semantics", "history_semantics", "causal_revision",
                "active_canonical_ids", "aliases", "retired_ids", "event_ids", "events"}
    if set(state) != required:
        raise ContractError("object_lifecycle has an invalid schema")
    if state["schema_version"] != SUPPORTED_OBJECT_LIFECYCLE_SCHEMA:
        raise ContractError("unsupported object_lifecycle schema_version")
    if state["protocol_version"] != 1:
        raise ContractError("unsupported object_lifecycle protocol_version")
    backend_name=state["backend_name"]
    supported=state["supported_event_kinds"]
    if backend_name is None:
        if supported:
            raise ContractError("uninitialized object_lifecycle cannot declare capabilities")
    elif type(backend_name) is not str or not backend_name.strip():
        raise ContractError("object_lifecycle backend_name must be non-empty or None")
    if not isinstance(supported,(list,tuple)) or any(item not in {"create","update","canonical_alias","retire"} for item in supported):
        raise ContractError("object_lifecycle supported_event_kinds is invalid")
    if len(set(supported)) != len(supported):
        raise ContractError("object_lifecycle supported_event_kinds contains duplicates")
    if state["snapshot_semantics"] != "active_canonical_objects" or state["history_semantics"] != "append_only_events":
        raise ContractError("unsupported object_lifecycle snapshot/history semantics")
    revision = state["causal_revision"]
    if type(revision) is not int or revision < 0:
        raise ContractError("object_lifecycle causal_revision must be a non-negative integer")
    active = state["active_canonical_ids"]
    retired = state["retired_ids"]
    event_ids = state["event_ids"]
    events = state["events"]
    aliases = state["aliases"]
    for name, values in (("active_canonical_ids", active), ("retired_ids", retired), ("event_ids", event_ids)):
        if not isinstance(values, (list, tuple)) or any(type(value) is not str or not value for value in values):
            raise ContractError(f"object_lifecycle {name} must be an array of non-empty strings")
        if len(set(values)) != len(values):
            raise ContractError(f"object_lifecycle {name} contains duplicate values")
    if not isinstance(aliases, Mapping) or any(type(k) is not str or not k or type(v) is not str or not v for k, v in aliases.items()):
        raise ContractError("object_lifecycle aliases must map non-empty IDs to non-empty IDs")
    if not isinstance(events, (list, tuple)) or any(not isinstance(event, Mapping) for event in events):
        raise ContractError("object_lifecycle events must be an array of event records")
    parsed_events=[]
    for index,event in enumerate(events):
        try:
            parsed_events.append(ObjectLifecycleEvent(
                schema_version=event["schema_version"], event_id=event["event_id"], kind=event["kind"],
                object_id=event["object_id"], frame_id=event["frame_id"], timestamp_s=event["timestamp_s"],
                causal_revision=event["causal_revision"], provenance=event["provenance"],
                evidence_ids=tuple(event.get("evidence_ids", ())), target_id=event.get("target_id"),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError(f"object_lifecycle event[{index}] is malformed: {exc}") from exc
    if len({event.event_id for event in parsed_events}) != len(events):
        raise ContractError("object_lifecycle history contains duplicate event IDs")
    if [event.event_id for event in parsed_events] != list(event_ids):
        raise ContractError("object_lifecycle event_ids do not match append-only event history")
    if set(active) & set(aliases) or set(active) & set(retired) or set(aliases) & set(retired):
        raise ContractError("object_lifecycle active, alias, and retired IDs conflict")
    for alias in aliases:
        seen = set()
        current = alias
        while current in aliases:
            if current in seen:
                raise ContractError("object_lifecycle alias cycle")
            seen.add(current)
            current = aliases[current]
    if any(target not in set(active) | set(retired) for target in aliases.values()):
        raise ContractError("object_lifecycle alias target is neither active nor retired")
    revisions=[event.causal_revision for event in parsed_events]
    if any(right <= left for left,right in zip(revisions,revisions[1:])):
        raise ContractError("object_lifecycle event causal revisions are not strictly increasing")
    if events and revisions[-1] != revision:
        raise ContractError("object_lifecycle causal_revision does not match the last event")
    # Return JSON-native detached data so frozen graph contracts do not retain caller-owned containers.
    return {
        "schema_version": SUPPORTED_OBJECT_LIFECYCLE_SCHEMA,
        "protocol_version": 1,
        "backend_name": backend_name,
        "supported_event_kinds": list(supported),
        "snapshot_semantics": "active_canonical_objects",
        "history_semantics": "append_only_events",
        "causal_revision": revision,
        "active_canonical_ids": sorted(active),
        "aliases": dict(sorted(aliases.items())),
        "retired_ids": sorted(retired),
        "event_ids": list(event_ids),
        "events": [event.to_record() for event in parsed_events],
    }


def reconcile_object_lifecycle(state: Mapping[str, object], events: tuple[ObjectLifecycleEvent, ...],
                               new_active_ids: set[str], capabilities: ObjectBackendCapabilities,
                               *, frame_id: str, timestamp_s: float) -> dict:
    """Validate explicit backend transitions and produce an append-only ledger.

    This function applies only events supplied by a backend. It does not decide
    when objects should merge or retire.
    """
    current = validate_object_lifecycle_state(state)
    if current["backend_name"] not in (None, capabilities.backend_name):
        raise ContractError("object backend changed within a lifecycle sequence")
    if current["backend_name"] == capabilities.backend_name and tuple(current["supported_event_kinds"]) != capabilities.event_kinds:
        raise ContractError("object backend capabilities changed within a lifecycle sequence")
    old_active = set(current["active_canonical_ids"])
    if any(type(value) is not str or not value for value in new_active_ids):
        raise ContractError("backend snapshot contains an invalid active object ID")
    if len(new_active_ids) != len(set(new_active_ids)):
        raise ContractError("backend snapshot contains duplicate active object IDs")
    aliases = dict(current["aliases"])
    retired = set(current["retired_ids"])
    event_ids = set(current["event_ids"])
    history = list(current["events"])
    seen_in_batch: set[str] = set()
    created: set[str] = set()
    aliased: set[str] = set()
    retired_now: set[str] = set()
    last_revision = current["causal_revision"]

    for event in events:
        if not isinstance(event, ObjectLifecycleEvent):
            raise ContractError("backend lifecycle events must use ObjectLifecycleEvent v1")
        if event.kind not in capabilities.event_kinds:
            raise ContractError(f"backend emitted unsupported lifecycle capability `{event.kind}`")
        if event.event_id in event_ids or event.event_id in seen_in_batch:
            raise ContractError(f"duplicate object lifecycle event_id: {event.event_id}")
        if event.causal_revision <= last_revision:
            raise ContractError("object lifecycle causal_revision must increase monotonically")
        if event.frame_id != frame_id or event.timestamp_s > timestamp_s:
            raise ContractError("object lifecycle event is not causal for the current M9 frame")
        last_revision = event.causal_revision
        seen_in_batch.add(event.event_id)
        if event.kind == "create":
            if event.object_id in old_active or event.object_id in aliases or event.object_id in retired:
                raise ContractError("create event reuses an active, aliased, or retired object ID")
            if event.object_id not in new_active_ids:
                raise ContractError("create event object is absent from active backend snapshot")
            created.add(event.object_id)
        elif event.kind == "update":
            if event.object_id not in new_active_ids:
                raise ContractError("update event object is absent from active backend snapshot")
        elif event.kind == "canonical_alias":
            target = event.target_id
            if event.object_id == target:
                raise ContractError("canonical alias cannot point an object to itself")
            if event.object_id not in old_active or event.object_id in new_active_ids:
                raise ContractError("canonical alias source must transition from active to non-active")
            if target not in new_active_ids or target in aliases or target in retired:
                raise ContractError("canonical alias target must be an active canonical object")
            if event.object_id in aliases or event.object_id in retired:
                raise ContractError("canonical alias source is not an active canonical object")
            for prior_alias, prior_target in tuple(aliases.items()):
                if prior_target == event.object_id:
                    aliases[prior_alias] = target
            aliases[event.object_id] = target
            aliased.add(event.object_id)
        elif event.kind == "retire":
            if event.object_id not in old_active or event.object_id in new_active_ids:
                raise ContractError("retire event must remove an active object from the backend snapshot")
            if event.object_id in aliases or event.object_id in retired:
                raise ContractError("retire event object is not an active canonical object")
            retired.add(event.object_id)
            retired_now.add(event.object_id)
        event_ids.add(event.event_id)
        history.append(event.to_record())

    added = new_active_ids - old_active
    removed = old_active - new_active_ids
    if added != created:
        raise ContractError(f"backend active snapshot has unexplained additions: {sorted(added - created)}")
    if removed != aliased | retired_now:
        raise ContractError(f"backend active snapshot has unexplained removals: {sorted(removed - aliased - retired_now)}")
    state_out = {
        "schema_version": SUPPORTED_OBJECT_LIFECYCLE_SCHEMA,
        "protocol_version": capabilities.protocol_version,
        "backend_name": capabilities.backend_name,
        "supported_event_kinds": list(capabilities.event_kinds),
        "snapshot_semantics": "active_canonical_objects",
        "history_semantics": "append_only_events",
        "causal_revision": last_revision,
        "active_canonical_ids": sorted(new_active_ids),
        "aliases": aliases,
        "retired_ids": sorted(retired),
        "event_ids": [event["event_id"] for event in history],
        "events": history,
    }
    return validate_object_lifecycle_state(state_out)

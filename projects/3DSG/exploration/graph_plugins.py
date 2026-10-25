"""Loadable graph runtime plugins for the ObjectNav host.

Plugins own graph-session packet conversion and publication details. They do
not own the simulator, navigation loop, keyframe store or bounded scheduler.
An optional ``update_pair(observation, object_packet, navigation_packet)`` hook
lets providers that need a two-branch barrier dispatch both sessions together.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, Protocol, runtime_checkable

from .scene_graph_provider import GraphProviderCapabilities


@runtime_checkable
class LiveGraphPlugin(Protocol):
    provider_id: str
    capabilities: Any

    def update(self, observation: Any) -> Any: ...
    def publish(self, commit: Any) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class LegacyPairedGraphPlugin(Protocol):
    """Compatibility surface for providers with two injected mapper sessions."""

    backend_name: str
    object_session: Any
    navigation_session: Any

    def object_packet(self, observation: Any) -> Any: ...
    def navigation_packet(self, observation: Any) -> Any: ...
    def publish(self, commit: Any) -> None: ...
    def close(self) -> None: ...


def load_graph_plugin(spec: str, *, p2_client: Any, live_publisher: Any,
                      event_sink: Callable[[str, dict[str, Any]], None]
                      ) -> LiveGraphPlugin | LegacyPairedGraphPlugin:
    """Load ``python.module:factory`` and validate its runtime surface."""
    module_name, separator, attribute = str(spec).partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("graph plugin must be written as python.module:factory")
    module = import_module(module_name)
    factory = getattr(module, attribute, None)
    if not callable(factory):
        raise TypeError(f"graph plugin factory is not callable: {spec}")
    plugin = factory(p2_client=p2_client, live_publisher=live_publisher,
                     event_sink=event_sink)
    generic = callable(getattr(plugin, "update", None))
    required = (("provider_id", "capabilities", "publish", "close") if generic else
                ("backend_name", "object_session", "navigation_session",
                 "object_packet", "navigation_packet", "publish", "close"))
    missing = [name for name in required if not hasattr(plugin, name)]
    if missing:
        mode = "graph provider" if generic else "legacy paired graph plugin"
        raise TypeError(f"{mode} {spec} is missing: {', '.join(missing)}")
    if generic:
        capabilities = plugin.capabilities
        if not isinstance(capabilities, GraphProviderCapabilities):
            raise TypeError(f"graph provider {spec} capabilities must be GraphProviderCapabilities")
        if str(plugin.provider_id) != capabilities.provider_id:
            raise ValueError(f"graph provider {spec} ID differs from its declared capabilities")
    paired_update = getattr(plugin, "update_pair", None)
    if paired_update is not None and not callable(paired_update):
        raise TypeError(f"graph plugin {spec} update_pair must be callable when provided")
    return plugin

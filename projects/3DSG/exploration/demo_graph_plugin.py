"""Reference plugin: P2 masks lifted to provisional RGB-D object hypotheses."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .rgbd_object_graph import FrontierNavGraphSession, RGBDObjectGraphSession
from .scene_graph_provider import GraphProviderCapabilities, SceneGraphSnapshot


class ProvisionalRGBDGraphPlugin:
    backend_name = "yolo11n_p2+rgbd_object_hypotheses+frontier_nav"
    provider_id = backend_name

    def __init__(self, *, p2_client: Any, live_publisher: Any,
                 event_sink: Callable[[str, dict[str, Any]], None]) -> None:
        if p2_client is None:
            raise ValueError("provisional RGB-D graph plugin requires a P2 worker")
        self.p2_client = p2_client
        self.live_publisher = live_publisher
        self.event_sink = event_sink
        self.object_session = RGBDObjectGraphSession()
        self.navigation_session = FrontierNavGraphSession()
        self.capabilities = GraphProviderCapabilities(
            provider_id=self.provider_id,
            input_modalities=("rgb", "primary_axial_depth", "camera_pose", "p2_masks"),
            entity_types=("object_hypothesis", "robot", "frontier", "goal"),
            relation_types=(),
            supports_incremental_updates=True,
            supports_auxiliary_depth=False,
            metadata={
                "semantic_quality": "provisional",
                "room_membership": "unavailable",
                "provider_role": "reference_backend",
            },
        )
        self._revision = 0
        self._closed = False

    def perceive(self, observation):
        return self.p2_client.update_frame(
            frame_index=observation.keyframe.frame_index,
            frame_id=observation.keyframe.frame_id,
            timestamp_s=observation.packet.simulator_time_s,
            rgb_file=Path(observation.keyframe.payload_ref) / "rgb.npy",
        )

    @staticmethod
    def object_packet(observation):
        intr = observation.packet.intrinsics
        perception = observation.perception_result or {"detections": []}
        return {
            "run_id": observation.keyframe.run_id,
            "scene_id": observation.keyframe.scene_id,
            "frame_id": observation.keyframe.frame_id,
            "frame_index": observation.keyframe.frame_index,
            "depth_m": observation.packet.depth_m,
            "depth_min_m": observation.packet.depth_min_m,
            "depth_max_m": observation.packet.depth_max_m,
            "intrinsics": {"fx": intr.fx, "fy": intr.fy,
                           "cx": intr.cx, "cy": intr.cy,
                           "width": intr.width, "height": intr.height},
            "T_world_camera": observation.packet.T_world_camera.matrix,
            "detections": perception.get("detections", []),
        }

    @staticmethod
    def navigation_packet(observation):
        return {
            "run_id": observation.keyframe.run_id,
            "scene_id": observation.keyframe.scene_id,
            "frame_id": observation.keyframe.frame_id,
            "frame_index": observation.keyframe.frame_index,
            "T_world_camera": observation.packet.T_world_camera.matrix,
            "floor_z_m": observation.packet.floor_z_m,
            "keyframe_metadata": observation.keyframe.metadata or {},
        }

    def update(self, observation):
        """Return a provider-neutral graph snapshot for one admitted frame."""
        from .graph_bridge import GraphObservation

        perception = self.perceive(observation)
        enriched = GraphObservation(
            observation.keyframe, observation.rgb, observation.packet, perception)
        object_result = self.object_session.update_frame(self.object_packet(enriched))
        navigation_result = self.navigation_session.update_frame(self.navigation_packet(enriched))
        nodes = {str(node["id"]): node
                 for node in (*object_result["nodes"], *navigation_result["nodes"])}
        edges = {}
        for edge in (*object_result["edges"], *navigation_result["edges"]):
            key = (str(edge["source"]), str(edge["target"]), str(edge["relation"]))
            edges[key] = edge
        self._revision += 1
        return SceneGraphSnapshot(
            provider_id=self.provider_id,
            run_id=observation.keyframe.run_id,
            scene_id=observation.keyframe.scene_id,
            frame_id=observation.keyframe.frame_id,
            frame_index=observation.keyframe.frame_index,
            revision=self._revision,
            nodes=tuple(nodes.values()),
            edges=tuple(edges.values()),
            status="provisional",
            provenance={
                "source_frame_id": observation.packet.frame_id,
                "source_frame_index": observation.packet.frame_index,
                "evidence_modalities": ["rgb", "primary_axial_depth", "p2_masks"],
                "room_membership": "unavailable",
            },
        )

    def publish(self, commit):
        snapshot = commit.graph_snapshot
        if snapshot is None:
            # Compatibility path for older GraphCommit producers.
            nodes = {str(node["id"]): node
                     for node in (*commit.conceptgraphs_result["nodes"],
                                  *commit.occusg_result["nodes"])}
            edges = {}
            for edge in (*commit.conceptgraphs_result["edges"],
                         *commit.occusg_result["edges"]):
                key = (str(edge["source"]), str(edge["target"]), str(edge["relation"]))
                edges[key] = edge
            graph_payload = {
                "revision": commit.dispatch_sequence,
                "as_of_frame_id": commit.frame_id,
                "host_monotonic_s": commit.committed_monotonic_s,
                "backend": self.backend_name,
                "status": "live_provisional",
                "nodes": list(nodes.values()), "edges": list(edges.values()),
            }
        else:
            graph_payload = snapshot.to_dict()
            graph_payload.update({
                "revision": commit.dispatch_sequence,
                "host_monotonic_s": commit.committed_monotonic_s,
                "backend": snapshot.provider_id,
                "status": "live_provisional" if snapshot.status == "provisional" else snapshot.status,
            })
        self.live_publisher.publish_graph(graph_payload)
        self.event_sink("graph_revision_published", {
            "revision": commit.dispatch_sequence,
            "frame_id": commit.frame_id,
            "frame_index": commit.frame_index,
            "node_count": len(graph_payload["nodes"]),
            "edge_count": len(graph_payload["edges"]),
            "backend": self.provider_id,
        })

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.object_session.close()
        self.navigation_session.close()


def create_plugin(*, p2_client, live_publisher, event_sink):
    """Factory referenced by the demo CLI; deployments can supply another."""
    return ProvisionalRGBDGraphPlugin(
        p2_client=p2_client, live_publisher=live_publisher, event_sink=event_sink)

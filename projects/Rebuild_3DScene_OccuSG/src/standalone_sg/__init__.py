"""Small, ROS-free Phase 1 foundation for a standalone OccuSG pipeline."""

from .contracts.frames import CameraIntrinsics, PointCloud, Transform
from .geometry.backproject import backproject_depth, decode_depth_to_meters

__all__ = [
    "CameraIntrinsics",
    "PointCloud",
    "Transform",
    "backproject_depth",
    "decode_depth_to_meters",
]


from .pipeline import FramePacket, GraphDelta, GraphPatch, SceneGraphBuilder, SceneGraphSnapshot

__all__ += ["FramePacket", "GraphDelta", "GraphPatch", "SceneGraphBuilder", "SceneGraphSnapshot"]

"""Conservative source-derived room-to-replacement-region matching."""
from __future__ import annotations
from dataclasses import dataclass
from ..contracts.regions import CanonicalRegion, Polygon2D
from .geometry import intersection_area, polygon_area, point_in_polygon
from .membership import RoomEntity

@dataclass(frozen=True,slots=True)
class RelinkCandidate:
    old_region_id:int
    new_region_id:int
    score:float
    old_overlap_ratio:float
    member_support_ratio:float
    iou:float
    centroid_inside:bool

def select_relink(old_region_id:int,old_polygon:Polygon2D,old_centroid,member_ids,regions,entities,claimed,*,min_old_overlap_ratio=.65,min_member_support_ratio=.5,min_score_margin=.15):
    old_area=polygon_area(old_polygon)
    if old_area<=0:return None
    entity_map={e.entity_id:e for e in entities}
    candidates=[]
    for region in regions:
        rid=int(region.persistent_id)
        if rid in claimed:continue
        inter=intersection_area(old_polygon,region.polygon)
        old_ratio=inter/old_area
        if inter<=0 or old_ratio<min_old_overlap_ratio:continue
        valid=[entity_map[k] for k in member_ids if k in entity_map]
        if not valid:continue
        support=sum(point_in_polygon(e.center_xy,region.polygon) for e in valid)/len(valid)
        if support<min_member_support_ratio:continue
        centroid_inside=point_in_polygon(old_centroid,region.polygon)
        if not centroid_inside and support<.75:continue
        new_area=polygon_area(region.polygon); union=old_area+new_area-inter
        iou=inter/union if union>0 else 0.0
        score=.45*old_ratio+.35*support+.10*iou+.10*(1.0 if centroid_inside else 0.0)
        candidates.append(RelinkCandidate(old_region_id,rid,score,old_ratio,support,iou,centroid_inside))
    candidates.sort(key=lambda c:(-c.score,-c.old_overlap_ratio,-c.member_support_ratio,c.new_region_id))
    if not candidates:return None
    if len(candidates)>1 and candidates[0].score-candidates[1].score<min_score_margin:return None
    return candidates[0]

"""Small dependency-free polygon operations for M8 simple rings."""
from __future__ import annotations
import math
from functools import lru_cache
from typing import Sequence
from ..contracts.regions import Point2, Polygon2D, Ring2D, ring_area

_EPS=1e-10

def _cross(a,b,c): return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
def _on_segment(p,a,b,eps=_EPS):
    return abs(_cross(a,b,p))<=eps and min(a[0],b[0])-eps<=p[0]<=max(a[0],b[0])+eps and min(a[1],b[1])-eps<=p[1]<=max(a[1],b[1])+eps

def ring_covers(point: Point2, ring: Ring2D) -> bool:
    inside=False; x,y=point
    for i,a in enumerate(ring):
        b=ring[(i+1)%len(ring)]
        if _on_segment(point,a,b): return True
        if (a[1]>y)!=(b[1]>y) and x < (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]: inside=not inside
    return inside

def point_in_polygon(point: Point2, polygon: Polygon2D) -> bool:
    if not ring_covers(point,polygon.outer): return False
    # Covers includes ring boundary; strict hole interiors are excluded.
    for hole in polygon.holes:
        if ring_covers(point,hole) and not any(_on_segment(point,hole[i],hole[(i+1)%len(hole)]) for i in range(len(hole))):
            return False
    return True

def _distance_segment(p,a,b):
    dx,dy=b[0]-a[0],b[1]-a[1]; den=dx*dx+dy*dy
    if den==0:return math.dist(p,a)
    t=max(0.0,min(1.0,((p[0]-a[0])*dx+(p[1]-a[1])*dy)/den))
    return math.hypot(p[0]-(a[0]+t*dx),p[1]-(a[1]+t*dy))

def distance_to_polygon(point,polygon):
    if point_in_polygon(point,polygon): return 0.0
    return min(_distance_segment(point,ring[i],ring[(i+1)%len(ring)]) for ring in (polygon.outer,*polygon.holes) for i in range(len(ring)))

def _triangulate(ring):
    """Return the source ear-clipping result, cached by immutable ring geometry.

    Membership can compare the same navigation block against many regions and
    ``_ring_intersection_area`` previously retriangulated both inputs inside
    every triangle-pair loop.  The cache changes only execution, not the
    source-derived triangulation or intersection rules.
    """
    return _triangulate_cached(tuple(ring))


@lru_cache(maxsize=4096)
def _triangulate_cached(ring: tuple):
    points=list(ring)
    if len(points)>1 and points[0]==points[-1]: points.pop()
    if len(points)<3:return []
    indices=list(range(len(points)))
    if sum(points[i][0]*points[(i+1)%len(points)][1]-points[(i+1)%len(points)][0]*points[i][1] for i in range(len(points)))<0: indices.reverse()
    tris=[]; guard=0
    while len(indices)>3 and guard<len(points)*len(points):
        guard+=1; found=False
        for j,cur in enumerate(indices):
            prev=indices[j-1]; nxt=indices[(j+1)%len(indices)]
            a,b,c=points[prev],points[cur],points[nxt]
            if _cross(a,b,c)<=_EPS: continue
            if any(_point_in_triangle(points[k],a,b,c) for k in indices if k not in (prev,cur,nxt)): continue
            tris.append((a,b,c)); indices.pop(j); found=True; break
        if not found: return []
    if len(indices)==3: tris.append(tuple(points[i] for i in indices))
    return tuple(tris)

def _point_in_triangle(p,a,b,c):
    return _cross(a,b,p)>=-_EPS and _cross(b,c,p)>=-_EPS and _cross(c,a,p)>=-_EPS

def _clip_convex(subject,clip):
    out=list(subject)
    for i,a in enumerate(clip):
        b=clip[(i+1)%len(clip)]; inp=out; out=[]
        if not inp: break
        prev=inp[-1]
        for cur in inp:
            cin=_cross(a,b,cur)>=-_EPS; pin=_cross(a,b,prev)>=-_EPS
            if cin:
                if not pin: out.append(_intersection(prev,cur,a,b))
                out.append(cur)
            elif pin: out.append(_intersection(prev,cur,a,b))
            prev=cur
    return out

def _intersection(p1,p2,q1,q2):
    x1,y1=p1; x2,y2=p2; x3,y3=q1; x4,y4=q2
    den=(x1-x2)*(y3-y4)-(y1-y2)*(x3-x4)
    if abs(den)<_EPS:return p2
    det1=x1*y2-y1*x2; det2=x3*y4-y3*x4
    return ((det1*(x3-x4)-(x1-x2)*det2)/den,(det1*(y3-y4)-(y1-y2)*det2)/den)

def _ring_intersection_area(a,b):
    total=0.0
    for ta in _triangulate(a):
        for tb in _triangulate(b):
            clipped=_clip_convex(ta,tb)
            if len(clipped)>=3: total+=ring_area(tuple(clipped))
    return total

def intersection_area(a: Polygon2D,b: Polygon2D):
    value=_ring_intersection_area(a.outer,b.outer)
    for hole in a.holes: value-=_ring_intersection_area(hole,b.outer)
    for hole in b.holes: value-=_ring_intersection_area(a.outer,hole)
    for ha in a.holes:
        for hb in b.holes: value+=_ring_intersection_area(ha,hb)
    return max(0.0,value)

def polygon_area(p): return max(0.0,ring_area(p.outer)-sum(ring_area(h) for h in p.holes))

def covers_bounds(p,bounds):
    x0,y0,x1,y1=bounds
    return any(point_in_polygon(pt,p) for pt in ((x0,y0),(x1,y0),(x1,y1),(x0,y1))) or any(x0<=x<=x1 and y0<=y<=y1 for x,y in p.outer)

#include "MapConverter.hh"
#include <octomap/OcTree.h>

#include <cmath>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

struct Result { int value; double floor, ceiling, ox, oy; };
Result run(const std::vector<voxel>& input, const std::vector<double>& b, double d) {
  auto leaves=input; for (auto& v:leaves) { v.position.x+=d; v.position.y+=d; v.position.z+=d; }
  auto bounds=b; for (int k=0;k<6;k+=2) bounds[k]+=d, bounds[k+1]+=d;
  MapConverter m(0.05,1,0.3,5); m.updateMap(leaves,bounds);
  const int ix=static_cast<int>(std::floor((4.4+d-m.map.offsetX())/0.05));
  const int iy=static_cast<int>(std::floor((1.45+d-m.map.offsetY())/0.05));
  return {m.map.get(4.4+d,1.45+d),m.map.getHeight(ix,iy),m.map.getHeightTop(ix,iy),m.map.offsetX(),m.map.offsetY()};
}
int main(int argc,char**argv) {
  try {
    if(argc!=2) throw std::runtime_error("usage: jamb_alignment_sensitivity LEAVES.txt");
    std::ifstream in(argv[1]); std::string tag; std::size_t n{}; in>>tag>>n;
    if(!in||tag!="leaf_count") throw std::runtime_error("expected leaf_count N");
    octomap::OcTree tree(0.05);
    for(std::size_t i=0;i<n;++i){unsigned short x{},y{},z{};float l{};in>>x>>y>>z>>l;if(!in)throw std::runtime_error("bad row");tree.setNodeValue(octomap::OcTreeKey(x,y,z),l,true);}
    tree.updateInnerOccupancy(); std::vector<voxel> leaves;
    for(auto it=tree.begin_leafs(),end=tree.end_leafs();it!=end;++it){voxel v;v.position.x=it.getX();v.position.y=it.getY();v.position.z=it.getZ();v.halfSize=static_cast<float>(it.getSize()/2.0);v.occupied=tree.isNodeOccupied(*it);leaves.push_back(v);}
    double x0,y0,z0,x1,y1,z1;tree.getMetricMin(x0,y0,z0);tree.getMetricMax(x1,y1,z1);
    const std::vector<double>b{x0,x1,y0,y1,z0,z1};const auto a=run(leaves,b,0.0);constexpr double d=0.01;const auto c=run(leaves,b,d);
    std::cout<<"{\"control\":\"diagnostic-only translated reduced voxel evidence\",\"offset_xyz_m\":["<<d<<','<<d<<','<<d<<']'
      <<",\"baseline\":{\"value\":"<<a.value<<",\"floor\":"<<a.floor<<",\"ceiling\":"<<a.ceiling<<",\"origin_xy\":["<<a.ox<<','<<a.oy<<']'
      <<"},\"translated\":{\"value\":"<<c.value<<",\"floor\":"<<c.floor<<",\"ceiling\":"<<c.ceiling<<",\"origin_xy\":["<<c.ox<<','<<c.oy<<']'
      <<"},\"warning\":\"projection sensitivity only; no full-scene/camera replay and not a corrected baseline\"}\n";
  }catch(const std::exception&e){std::cerr<<e.what()<<'\n';return 1;}return 0;
}

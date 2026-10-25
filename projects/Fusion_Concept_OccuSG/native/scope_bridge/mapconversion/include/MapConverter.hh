#pragma once

#include "Map2D.hh"
#include "datatypes.hh"

#include <vector>

using namespace std;

struct ScopeBridgeHeightCell {
  int cell_x{0};
  int cell_y{0};
  double world_x{0.0};
  double world_y{0.0};
  int m3_value{-1};
  double z_floor{NAN};
  double z_ceiling{NAN};
  vector<heightRange> free_before_min_height;
  vector<heightRange> free_used_for_projection;
  vector<heightRange> occupied;
};

class MapConverter {
public:
  Map2D map;
  MapConverter(double resolution, int slopeEstimationSize, double minimumZ,
               int minimumOccupancy);
  ~MapConverter();
  // update global map in region of the voxels given
  void updateMap(vector<voxel> vMap, vector<double> minMax,
                 vector<ScopeBridgeHeightCell>* height_trace = nullptr);
  // get the min max values in x and y for given voxel list agins map frame.
  // Returns vector with <minX, maxX, minY, maxY, minZ, maxZ>
  vector<double> minMaxVoxel(vector<voxel> list);

private:
  int slopeEstimationSize;
  double resolution;
  int minOcc;
  double minZ;
};

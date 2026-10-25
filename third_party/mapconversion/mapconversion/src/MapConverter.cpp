
#include "MapConverter.hh"
#include "HeightRangeMap.hh"

#include <algorithm>
#include <cmath>

MapConverter::MapConverter(double resolution, int slopeEstimationSize,
                           double minimumZ, int minimumOccupancy) {
  MapConverter::resolution = resolution;
  MapConverter::slopeEstimationSize = slopeEstimationSize;
  MapConverter::minOcc = minimumOccupancy;
  MapConverter::minZ = minimumZ;
  Map2D m(resolution);
  MapConverter::map = m;
}

MapConverter::~MapConverter() {}

void MapConverter::updateMap(vector<voxel> vMap, vector<double> minMax) {
  if (vMap.size() == 0)
    return;

  // Check for NaN and infinity values - NaN can cause huge size calculations
  for (size_t i = 0; i < minMax.size(); ++i) {
    if (std::isnan(minMax[i])) {
      fprintf(stderr, "ERROR: NaN value in minMax at index %zu\n", i);
      return;
    }
    if (std::isinf(minMax[i])) {
      fprintf(stderr, "ERROR: Infinity value in minMax at index %zu\n", i);
      return;
    }
  }

  if (MapConverter::map.getResulution() <= 0)
    return;
  // get number of cells in grid
  int xSize = static_cast<int>(
                  std::ceil((minMax[1] - minMax[0]) /
                            MapConverter::map.getResulution())) +
              1;
  int ySize = static_cast<int>(
                  std::ceil((minMax[3] - minMax[2]) /
                            MapConverter::map.getResulution())) +
              1;
  if (xSize <= 0 || ySize <= 0)
    return;
  if (xSize > 100000 || ySize > 100000) {
    // Prevent segfaults from huge allocation due to NaN/invalid values
    fprintf(stderr, "ERROR: Unreasonably large map size: %d x %d\n", xSize, ySize);
    return;
  }
  // creat local maps
  HeightRangeMap hMap(xSize, ySize);

  // Initialize all posMap entries to prevent uninitialized data access
  for (int x = 0; x < xSize; x++) {
    for (int y = 0; y < ySize; y++) {
      hMap.posMap[x][y].x = x * resolution + minMax[0];
      hMap.posMap[x][y].y = y * resolution + minMax[2];
    }
  }
  for (voxel v : vMap) {
    // as voxels can be larger then a map cell, this loop goes through all cell
    // voxel occupies
    int sizeIndex = std::max(
        1, static_cast<int>(
               std::ceil((v.halfSize * 2.0) /
                         MapConverter::map.getResulution())));
    for (int x = 0; x < sizeIndex; x++) {
      for (int y = 0; y < sizeIndex; y++) {
        int posX = static_cast<int>(std::floor(
            (v.position.x - v.halfSize + resolution * x - minMax[0]) /
            resolution));
        int posY = static_cast<int>(std::floor(
            (v.position.y - v.halfSize + resolution * y - minMax[2]) /
            resolution));
        if (posX < 0 || posX >= xSize)
          continue; // if cell is outside local map grid
        if (posY < 0 || posY >= ySize)
          continue;
        // size of heigt range is slightly enlarged for better overlap detection
        heightRange hr = {v.position.z + v.halfSize + resolution * 0.001,
                          v.position.z - v.halfSize - resolution * 0.001};
        hMap.addRange(posX, posY, hr, v.occupied,
                      v.position.x - v.halfSize + resolution * x,
                      v.position.y - v.halfSize + resolution * y);
      }
    }
  }
  // remove free space that is smaller than robots is safety margin
  for (int x = 1; x < xSize - 1; x++) {
    for (int y = 1; y < ySize - 1; y++) {
      hMap.removeFreeRanges(x, y, minZ);
      int mapValue = 0;
      if (hMap.free[x][y].size() == 0) {
        hMap.posMap[x][y].x = x * resolution + minMax[0];
        hMap.posMap[x][y].y = y * resolution + minMax[2];
        mapValue = -1;
      }
      if (mapValue == -1 &&
          map.get(hMap.posMap[x][y].x, hMap.posMap[x][y].y) == -1)
        continue;

      // set free space and heigth map
      map.set(hMap.posMap[x][y].x, hMap.posMap[x][y].y, mapValue);
      if (mapValue == -1)
        continue;
      map.setHeight(hMap.posMap[x][y].x, hMap.posMap[x][y].y,
                    hMap.free[x][y].back().bottom);
      map.setHeightTop(hMap.posMap[x][y].x, hMap.posMap[x][y].y,
                       hMap.free[x][y][0].top);
    }
  }
  // check if there are a neighboring occupied cells
  for (int x = 2; x < xSize - 2; x++) {
    for (int y = 2; y < ySize - 2; y++) {
      // find free space with a unknown neighbour
      if (hMap.free[x][y].size() == 0)
        continue;

      for (auto d : DIRECTIONS) {
        int nx = x + d.x;
        int ny = y + d.y;
        // Bounds check before accessing neighbors
        if (nx < 0 || nx >= xSize || ny < 0 || ny >= ySize)
          continue;
        if (hMap.free[nx][ny].size() != 0)
          continue;
        map.set(hMap.posMap[nx][ny].x, hMap.posMap[nx][ny].y,
                hMap.getOccupation(nx, ny, -1, minOcc));
      }
    }
  }

  map.updateSlope(slopeEstimationSize, minMax[0], minMax[1], minMax[2],
                  minMax[3]);
}

vector<double> MapConverter::minMaxVoxel(vector<voxel> list) {
  vector<double> minMax(6);
  minMax[0] = INFINITY;
  minMax[1] = -INFINITY;
  minMax[2] = INFINITY;
  minMax[3] = -INFINITY;
  minMax[4] = INFINITY;
  minMax[5] = -INFINITY;
  for (voxel v : list) {
    // for reliebol size estimation of new regon, only smalest voxels in the
    // octree are used
    if (v.halfSize > resolution * .9)
      continue;
    minMax[0] = min(minMax[0], v.position.x);
    minMax[1] = max(minMax[1], v.position.x);
    minMax[2] = min(minMax[2], v.position.y);
    minMax[3] = max(minMax[3], v.position.y);
    minMax[4] = min(minMax[4], v.position.z);
    minMax[5] = max(minMax[5], v.position.z);
  }
  return minMax;
}

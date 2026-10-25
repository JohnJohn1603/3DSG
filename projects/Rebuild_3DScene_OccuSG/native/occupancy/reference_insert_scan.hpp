#pragma once

#include "occupancy_map.hpp"

namespace standalone_sg::occupancy {

struct ReferenceKeySets {
  std::vector<Key> free_keys;
  std::vector<Key> occupied_keys;
};

// Independent transcription of the pinned OccuSG insertScan key-set logic.
// It deliberately does not call OcTree::computeUpdate or OccupancyMap.
ReferenceKeySets reference_insert_scan(const octomap::OcTree& tree, const Scan& scan);

}  // namespace standalone_sg::occupancy


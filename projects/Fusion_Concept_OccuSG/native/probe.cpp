#include <array>
#include <iostream>

int main() {
  constexpr std::array<int, 3> phase{0, 1, 2};
  std::cout << "standalone-sg native probe: C++17 OK (" << phase.size() << " values)\n";
  return phase[1] == 1 ? 0 : 1;
}


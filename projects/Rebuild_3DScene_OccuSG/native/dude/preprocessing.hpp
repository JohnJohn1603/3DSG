#pragma once
#include <opencv2/core.hpp>
#include <cstdint>
#include <vector>

struct M4Preprocessed {
  cv::Mat image_cleaned;
  cv::Mat black_image;
  cv::Rect crop;
  float pixel_tau{};
};

M4Preprocessed m4_preprocess(const std::vector<std::int8_t>& values,
                             int width, int height, float resolution,
                             float threshold_m,
                             bool preserve_unknown_boundary = false);

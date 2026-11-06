#pragma once

#include <math.h>

constexpr float ZERO_DEADBAND_GRAMS = 2.0f;
constexpr float MAX_CAPACITY_GRAMS = 20000.0f;

// Convert only near-zero offsets to empty. Negative readings beyond the
// measured noise band and readings above capacity remain invalid.
inline bool normalizeWeight(float calculatedGrams, float &normalizedGrams) {
  if (!isfinite(calculatedGrams)) return false;

  if (calculatedGrams >= -ZERO_DEADBAND_GRAMS &&
      calculatedGrams <= ZERO_DEADBAND_GRAMS) {
    normalizedGrams = 0.0f;
  } else {
    normalizedGrams = calculatedGrams;
  }

  return normalizedGrams >= 0.0f && normalizedGrams <= MAX_CAPACITY_GRAMS;
}

// Accept a tare only when the complete averaging window stayed within the
// firmware's existing stability range. This does not change the zero
// deadband or calibration; it prevents averaging a moving/noisy platform.
inline bool tareWindowIsStable(long minimumRaw, long maximumRaw,
                               float calibrationFactor, float maxRangeGrams) {
  if (!isfinite(calibrationFactor) || calibrationFactor == 0.0f ||
      !isfinite(maxRangeGrams) || maxRangeGrams < 0.0f || minimumRaw > maximumRaw) {
    return false;
  }
  const float rangeGrams =
      fabsf(static_cast<float>(maximumRaw) - static_cast<float>(minimumRaw)) /
      fabsf(calibrationFactor);
  return isfinite(rangeGrams) && rangeGrams <= maxRangeGrams;
}

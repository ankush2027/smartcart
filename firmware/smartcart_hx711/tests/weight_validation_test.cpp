#include <assert.h>
#include <math.h>

#include "../weight_validation.h"

int main() {
  float grams = -1.0f;

  assert(normalizeWeight(-0.467f, grams));
  assert(grams == 0.0f);

  assert(normalizeWeight(-1.204f, grams));
  assert(grams == 0.0f);

  assert(normalizeWeight(1.9f, grams));
  assert(grams == 0.0f);

  assert(normalizeWeight(2.001f, grams));
  assert(fabsf(grams - 2.001f) < 0.0001f);

  assert(!normalizeWeight(-2.001f, grams));
  assert(normalizeWeight(4.333f, grams));
  assert(fabsf(grams - 4.333f) < 0.0001f);

  assert(normalizeWeight(MAX_CAPACITY_GRAMS, grams));
  assert(grams == MAX_CAPACITY_GRAMS);
  assert(!normalizeWeight(MAX_CAPACITY_GRAMS + 0.1f, grams));
  assert(!normalizeWeight(NAN, grams));

  // Tare uses the existing 2 g stability span across its 20 raw samples.
  assert(tareWindowIsStable(-100, 100, 107.1f, 2.0f));
  assert(!tareWindowIsStable(-108, 108, 107.1f, 2.0f));
  assert(!tareWindowIsStable(-100, 100, 0.0f, 2.0f));
  assert(!tareWindowIsStable(100, -100, 107.1f, 2.0f));
}

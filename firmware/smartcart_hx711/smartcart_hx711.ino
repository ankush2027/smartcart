#include <HX711.h>
#include "weight_validation.h"

#include <Arduino.h>
#include <HX711.h>
#include <math.h>

// Arduino Mega 2560 defaults. Change to match the documented wiring.
constexpr uint8_t HX711_DOUT_PIN = 3;
constexpr uint8_t HX711_SCK_PIN = 2;
constexpr uint8_t PROTOCOL_VERSION = 1;

// Define SMARTCART_CALIBRATION_FACTOR to the measured factor (raw counts per
// gram). The default deliberately refuses to emit weight measurements.
#define SMARTCART_CALIBRATION_FACTOR 107.1f
#ifndef SMARTCART_CALIBRATION_FACTOR
#define SMARTCART_CALIBRATION_FACTOR 1.0f
constexpr bool CALIBRATION_CONFIGURED = false;
#else
constexpr bool CALIBRATION_CONFIGURED = true;
#endif
constexpr float CALIBRATION_FACTOR = SMARTCART_CALIBRATION_FACTOR;
constexpr uint32_t REPORT_INTERVAL_MS = 100;
constexpr uint32_t ERROR_INTERVAL_MS = 1000;
constexpr uint32_t READY_INTERVAL_MS = 1000;
constexpr float STABILITY_RANGE_GRAMS = 2.0f;
constexpr uint8_t STABILITY_SAMPLE_COUNT = 3;
constexpr uint8_t RAW_AVERAGE_COUNT = 1;
constexpr uint8_t COMMAND_BUFFER_SIZE = 24;

HX711 scale;
float stableWindow[STABILITY_SAMPLE_COUNT];
uint8_t stableCount = 0;
uint8_t stableNext = 0;
uint32_t sequenceNumber = 0;
uint32_t lastSampleAt = 0;
uint32_t lastErrorAt = 0;
uint32_t lastReadyAt = 0;
bool tareComplete = false;
char commandBuffer[COMMAND_BUFFER_SIZE];
uint8_t commandLength = 0;

void emitError(const char *code) {
  Serial.print(F("{\"type\":\"error\",\"code\":\""));
  Serial.print(code);
  Serial.print(F("\",\"protocol_version\":"));
  Serial.print(PROTOCOL_VERSION);
  Serial.print(F(",\"sequence\":"));
  Serial.print(sequenceNumber++);
  Serial.print(F(",\"uptime_ms\":"));
  Serial.print(millis());
  Serial.println(F("}"));
}

void emitWeightOutOfRange(long raw, long tareOffset, float grams, float calibrationFactor) {
  Serial.print(F("{\"type\":\"error\",\"code\":\"weight_out_of_range\",\"protocol_version\":"));
  Serial.print(PROTOCOL_VERSION);
  Serial.print(F(",\"raw\":"));
  Serial.print(raw);
  Serial.print(F(",\"tare_offset\":"));
  Serial.print(tareOffset);
  Serial.print(F(",\"grams\":"));
  if (isfinite(grams)) {
    Serial.print(grams, 3);
  } else {
    // JSON has no NaN or Infinity values; preserve a valid JSON Lines record.
    Serial.print(F("null"));
  }
  Serial.print(F(",\"calibration_factor\":"));
  Serial.print(calibrationFactor, 4);
  Serial.print(F(",\"sequence\":"));
  Serial.print(sequenceNumber++);
  Serial.print(F(",\"uptime_ms\":"));
  Serial.print(millis());
  Serial.println(F("}"));
}

void emitReady(bool hasRequestId = false, uint32_t requestId = 0) {
  Serial.print(F("{\"type\":\"status\",\"status\":\"ready\",\"protocol_version\":"));
  Serial.print(PROTOCOL_VERSION);
  Serial.print(F(",\"sequence\":"));
  Serial.print(sequenceNumber++);
  Serial.print(F(",\"uptime_ms\":"));
  Serial.print(millis());
  if (hasRequestId) {
    Serial.print(F(",\"request_id\":"));
    Serial.print(requestId);
  }
  Serial.println(F("}"));
}

void emitTare(bool ok, const char *reason, uint8_t sampleCount = 0,
              long spreadCounts = 0, float spreadGrams = NAN) {
  Serial.print(F("{\"type\":\"tare\",\"ok\":"));
  Serial.print(ok ? F("true") : F("false"));
  Serial.print(F(",\"protocol_version\":"));
  Serial.print(PROTOCOL_VERSION);
  Serial.print(F(",\"reason\":\""));
  Serial.print(reason);
  Serial.print(F("\",\"sample_count\":"));
  Serial.print(sampleCount);
  Serial.print(F(",\"spread_counts\":"));
  if (sampleCount > 0) {
    Serial.print(spreadCounts);
  } else {
    Serial.print(F("null"));
  }
  Serial.print(F(",\"spread_grams\":"));
  if (sampleCount > 0 && isfinite(spreadGrams)) {
    Serial.print(spreadGrams, 3);
  } else {
    Serial.print(F("null"));
  }
  Serial.print(F(",\"stability_limit_grams\":"));
  Serial.print(STABILITY_RANGE_GRAMS, 3);
  Serial.print(F(",\"sequence\":"));
  Serial.print(sequenceNumber++);
  Serial.print(F(",\"uptime_ms\":"));
  Serial.print(millis());
  Serial.println(F("}"));
}

void clearStabilityWindow() {
  stableCount = 0;
  stableNext = 0;
}

void handleCommand(const char *command) {
  if (strncmp(command, "READY? ", 7) == 0) {
    const char *digits = command + 7;
    uint32_t requestId = 0;
    if (*digits == '\0') {
      emitError("invalid_ready_request");
      return;
    }
    while (*digits != '\0') {
      if (*digits < '0' || *digits > '9') {
        emitError("invalid_ready_request");
        return;
      }
      const uint8_t digit = static_cast<uint8_t>(*digits - '0');
      if (requestId > (0xFFFFFFFFUL - digit) / 10UL) {
        emitError("invalid_ready_request");
        return;
      }
      requestId = requestId * 10UL + digit;
      ++digits;
    }
    emitReady(true, requestId);
    return;
  }
  if (strcmp(command, "TARE") != 0) {
    emitTare(false, "unknown_command");
    return;
  }
  // Once a new tare is requested, no previous offset remains eligible if
  // readiness or stability validation fails.
  tareComplete = false;
  if (!scale.wait_ready_timeout(1000)) {
    emitTare(false, "hx711_not_ready");
    return;
  }
  // The conversion already in progress when the command arrived may predate
  // the tare window. Discard that one ready conversion, then measure a fresh
  // 20-reading window. This does not relax the existing 2 g spread limit.
  scale.read();
  // Bounded averaging: HX711::tare/read_average can wait indefinitely if
  // DOUT disconnects during the operation.
  int64_t rawSum = 0;
  long minimumRaw = 0;
  long maximumRaw = 0;
  for (uint8_t i = 0; i < 20; ++i) {
    if (!scale.wait_ready_timeout(500)) {
      tareComplete = false;
      const long spreadCounts = maximumRaw - minimumRaw;
      const float spreadGrams = i > 0
          ? fabsf(static_cast<float>(spreadCounts)) / fabsf(CALIBRATION_FACTOR)
          : NAN;
      emitTare(false, "hx711_disconnected_during_tare", i, spreadCounts, spreadGrams);
      return;
    }
    const long raw = scale.read();
    rawSum += raw;
    if (i == 0 || raw < minimumRaw) minimumRaw = raw;
    if (i == 0 || raw > maximumRaw) maximumRaw = raw;
  }
  const long spreadCounts = maximumRaw - minimumRaw;
  const float spreadGrams = fabsf(static_cast<float>(spreadCounts)) /
                            fabsf(CALIBRATION_FACTOR);
  if (!tareWindowIsStable(minimumRaw, maximumRaw, CALIBRATION_FACTOR,
                          STABILITY_RANGE_GRAMS)) {
    emitTare(false, "unstable_empty_platform", 20, spreadCounts, spreadGrams);
    return;
  }
  scale.set_offset(static_cast<long>(rawSum / 20));
  tareComplete = true;
  clearStabilityWindow();
  emitTare(true, "ok", 20, spreadCounts, spreadGrams);
}

void readCommands() {
  while (Serial.available() > 0) {
    const char value = static_cast<char>(Serial.read());
    if (value == '\r') continue;
    if (value == '\n') {
      commandBuffer[commandLength] = '\0';
      if (commandLength > 0) handleCommand(commandBuffer);
      commandLength = 0;
      continue;
    }
    if (commandLength + 1 >= COMMAND_BUFFER_SIZE) {
      commandLength = 0;
      emitTare(false, "command_too_long");
      continue;
    }
    commandBuffer[commandLength++] = value;
  }
}

bool isStable(float current) {
  stableWindow[stableNext] = current;
  stableNext = (stableNext + 1) % STABILITY_SAMPLE_COUNT;
  if (stableCount < STABILITY_SAMPLE_COUNT) stableCount++;
  if (stableCount < STABILITY_SAMPLE_COUNT) return false;
  float low = stableWindow[0];
  float high = stableWindow[0];
  for (uint8_t i = 1; i < STABILITY_SAMPLE_COUNT; ++i) {
    if (stableWindow[i] < low) low = stableWindow[i];
    if (stableWindow[i] > high) high = stableWindow[i];
  }
  return (high - low) <= STABILITY_RANGE_GRAMS;
}

void setup() {
  Serial.begin(115200);
  scale.begin(HX711_DOUT_PIN, HX711_SCK_PIN);
  emitReady();
  lastReadyAt = millis();
  if (!isfinite(CALIBRATION_FACTOR) || CALIBRATION_FACTOR == 0.0f) {
    emitError("invalid_calibration_factor");
  }
}

void loop() {
  readCommands();
  const uint32_t now = millis();
  if (static_cast<uint32_t>(now - lastReadyAt) >= READY_INTERVAL_MS) {
    emitReady();
    lastReadyAt = now;
  }
  if (static_cast<uint32_t>(now - lastSampleAt) < REPORT_INTERVAL_MS) return;
  lastSampleAt = now;

  if (!scale.is_ready()) {
    if (static_cast<uint32_t>(now - lastErrorAt) >= ERROR_INTERVAL_MS) {
      emitError("hx711_not_ready");
      lastErrorAt = now;
    }
    return;
  }
  if (!CALIBRATION_CONFIGURED || !isfinite(CALIBRATION_FACTOR) || CALIBRATION_FACTOR == 0.0f) {
    if (static_cast<uint32_t>(now - lastErrorAt) >= ERROR_INTERVAL_MS) {
      emitError("calibration_required");
      lastErrorAt = now;
    }
    return;
  }
  if (!tareComplete) {
    if (static_cast<uint32_t>(now - lastErrorAt) >= ERROR_INTERVAL_MS) {
      emitError("tare_required");
      lastErrorAt = now;
    }
    return;
  }

  const long raw = scale.read_average(RAW_AVERAGE_COUNT);
  const long tareOffset = scale.get_offset();
  const float calculatedGrams = (static_cast<float>(raw) - static_cast<float>(tareOffset))
                                / CALIBRATION_FACTOR;
  float grams = 0.0f;
  if (!normalizeWeight(calculatedGrams, grams)) {
    clearStabilityWindow();
    if (static_cast<uint32_t>(now - lastErrorAt) >= ERROR_INTERVAL_MS) {
      emitWeightOutOfRange(raw, tareOffset, calculatedGrams, CALIBRATION_FACTOR);
      lastErrorAt = now;
    }
    return;
  }

  const bool stable = isStable(grams);
  Serial.print(F("{\"type\":\"weight\",\"protocol_version\":"));
  Serial.print(PROTOCOL_VERSION);
  Serial.print(F(",\"grams\":"));
  Serial.print(grams, 1);
  Serial.print(F(",\"raw\":"));
  Serial.print(raw);
  Serial.print(F(",\"stable\":"));
  Serial.print(stable ? F("true") : F("false"));
  Serial.print(F(",\"sequence\":"));
  Serial.print(sequenceNumber++);
  Serial.print(F(",\"uptime_ms\":"));
  Serial.print(now);
  Serial.println(F(",\"calibrated\":true}"));
}

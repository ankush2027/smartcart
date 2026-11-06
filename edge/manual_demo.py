"""Validated, idempotent operator input for the camera/manual-weight demo."""
from dataclasses import dataclass
import math
from edge.detection.product_resolver import Detection

@dataclass(frozen=True)
class ManualSKU:
    sku: str
    yolo_class: str
    grams: int
    generic_aliases: tuple[str, ...] = ()

SKU_CATALOG = {
    "LIFEBUOY": ManualSKU("LIFEBUOY", "Lifebuoy", 40),
    "MAGGI_5": ManualSKU("MAGGI_5", "1 to 3 Noodles Rs 5", 30,
                         ("Maggi", "Noodles", "1 to 3 Noodles")),
    "MAGGI_10": ManualSKU("MAGGI_10", "1 to 3 Noodles Rs 10", 57,
                          ("Maggi", "Noodles", "1 to 3 Noodles")),
    "COCA_COLA": ManualSKU("COCA_COLA", "Coca Cola", 256),
    "LAYS_25G": ManualSKU("LAYS_25G", "Lays", 25),
}

class ManualInputRejected(ValueError):
    pass

class DetectionEpisodeGuard:
    """Allow one successful billing action per visible detection episode."""
    def __init__(self):
        self._spent = set()

    def available(self, detections):
        active = {d.yolo_class for d in detections}
        self._spent.intersection_update(active)
        return [d for d in detections if d.yolo_class not in self._spent]

    def consume(self, detected_label):
        self._spent.add(detected_label)

def build_manual_event(*, sku, signed_grams, detections, products, event_id):
    if not isinstance(sku, str) or sku.strip().upper() not in SKU_CATALOG:
        raise ManualInputRejected("Unsupported SKU")
    spec = SKU_CATALOG[sku.strip().upper()]
    try:
        delta = float(signed_grams)
    except (TypeError, ValueError):
        raise ManualInputRejected("Weight must be a signed number of grams") from None
    if not math.isfinite(delta) or delta == 0 or abs(delta) != spec.grams:
        raise ManualInputRejected(f"{spec.sku} requires exactly +{spec.grams}g or -{spec.grams}g")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ManualInputRejected("Event ID is required")
    row = next((row for row in products if row[5] == spec.yolo_class), None)
    if row is None:
        raise ManualInputRejected(f"SKU {spec.sku} is not present in the active catalogue")
    accepted = {spec.yolo_class, *spec.generic_aliases}
    found = [d for d in detections if d.yolo_class in accepted]
    if not found:
        raise ManualInputRejected(f"Selected SKU {spec.sku} does not match the detected product")
    representative = max(found, key=lambda d: d.confidence)
    confidence = representative.confidence
    if not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ManualInputRejected("Detection confidence is invalid")
    return {"sku": spec.sku, "product_id": row[0], "yolo_class": spec.yolo_class,
            "name": row[1], "weight_change": delta, "source": "manual_demo",
            "event_id": event_id.strip(), "detected_label": representative.yolo_class,
            "detections": [Detection(spec.yolo_class, confidence)]}

def submit_manual_event(*, session, sku, signed_grams, detections, products, event_id):
    event = build_manual_event(sku=sku, signed_grams=signed_grams, detections=detections,
                               products=products, event_id=event_id)
    process = session.process_add if event["weight_change"] > 0 else session.process_remove
    result = process(event["detections"], event["weight_change"],
                     source=event["source"], event_id=event["event_id"])
    return event, result

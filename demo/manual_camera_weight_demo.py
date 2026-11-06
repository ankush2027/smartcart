"""Camera guided manual weight demo using the normal CartSession billing path.

Manual entries are operator supplied grams. This tool does not read or emulate
HX711 telemetry.
"""
from __future__ import annotations

import argparse
import logging
import uuid

from edge.database.database import initialize_database
from edge.database.product_repository import get_active_products
from edge.detection.camera import open_camera
from edge.manual_demo import (DetectionEpisodeGuard, ManualInputRejected, SKU_CATALOG,
                               submit_manual_event)
from edge.firebase.client import get_reference, initialize
from edge.model.yolo_detector import DEFAULT_MODEL_PATH, YOLODetector
from edge.pairing.session_store import get_active_session_for_cart
from edge.runtime.cart_runtime import resume_session


def _product_rows():
    return list(get_active_products())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cart-id", required=True)
    parser.add_argument("--model", default=str(DEFAULT_MODEL_PATH))
    parser.add_argument("--camera-index", type=int, default=0)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    initialize_database()
    initialize()
    firebase_ref = get_reference("/")
    if get_active_session_for_cart(args.cart_id) is None:
        parser.error(f"No active session for cart {args.cart_id!r}; pair/start it using the existing app flow first")
    detector = YOLODetector(model_path=args.model)
    session = resume_session(args.cart_id, detector, firebase_ref)
    products = _product_rows()
    menu = []
    for sku, spec in SKU_CATALOG.items():
        row = next((row for row in products if row[5] == spec.yolo_class), None)
        if row is not None:
            menu.append((sku, spec.yolo_class, spec.grams, row[0], row[1]))
    if len(menu) != len(SKU_CATALOG):
        available = {row[5] for row in products}
        missing = sorted({spec.yolo_class for spec in SKU_CATALOG.values()} - available)
        parser.error("Active catalogue is missing required camera classes: " + ", ".join(missing))

    camera = open_camera(args.camera_index)
    import cv2
    print("================================")
    print("SMARTCART MANUAL DEMO")
    print("================================")
    print("Manual operator weights are used; no load-cell telemetry is read.")
    print("Press q in the camera window, or Ctrl-C here, to quit safely.")
    for number, (sku, _label, grams, _product_id, name) in enumerate(menu, 1):
        print(f"{number}. {name} [{sku}] — {grams}g")
    detection_guard = DetectionEpisodeGuard()
    try:
        while True:
            ok, frame = camera.read()
            if not ok:
                print("Rejected: camera frame unavailable")
                continue
            found = detector.detect(frame)
            available = detection_guard.available(found)
            for detection in found:
                print(f"Detected: {detection.yolo_class} confidence={detection.confidence:.1%}")
            cv2.imshow("SmartCart manual weight demo", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
            if not available:
                if found:
                    print("Waiting for the last accepted product to leave the frame before another event.")
                continue
            try:
                sku = input("Select SKU (number or SKU; Enter skips): ").strip().upper()
                if not sku:
                    continue
                if sku.isdigit() and 1 <= int(sku) <= len(menu):
                    sku = menu[int(sku) - 1][0]
                choices = [item for item in menu if item[0] == sku]
                if not choices:
                    print(f"Rejected: unsupported SKU {sku!r}")
                    continue
                _, label, demo_grams, product_id, name = choices[0]
                print(f"Selected {sku}: {name}; model class {label}")
                action = input("Action [A]DD / [R]EMOVE (Enter cancels): ").strip().upper()
                if action not in ("A", "ADD", "R", "REMOVE"):
                    print("Cancelled: choose A or R.")
                    continue
                sign = 1 if action in ("A", "ADD") else -1
                print(f"Expected demo weight: {demo_grams}g")
                raw = input(f"Enter signed weight ({'+' if sign > 0 else '-'}{demo_grams}): ").strip()
                delta = float(raw)
                if delta * sign <= 0:
                    raise ManualInputRejected("Weight sign does not match the selected action")
                event_id = str(uuid.uuid4())
                event, result = submit_manual_event(
                    session=session, sku=sku, signed_grams=delta, detections=available,
                    products=products, event_id=event_id)
                if result.success:
                    detection_guard.consume(event["detected_label"])
                    verb = "added" if delta > 0 else "removed"
                    print(f"SUCCESS: {name} {verb}.")
                    print(f"Customer cart updated. event_id={event['event_id']}; source={event['source']}")
                else:
                    print(f"Rejected: {result.reason}; source=manual_demo event_id={event_id}")
            except (ManualInputRejected, ValueError) as error:
                print(f"Rejected: invalid manual weight input ({error}); source=manual_demo")
            except EOFError:
                print("Input closed; shutting down safely.")
                break
            except KeyboardInterrupt:
                print("\nOperator requested exit.")
                break
    finally:
        camera.release()
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

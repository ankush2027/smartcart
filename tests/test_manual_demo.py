"""Focused tests for manual_demo validation, idempotency and live projections."""
import json
import uuid

import pytest

from edge.billing.billing import get_bill, get_session_log
from edge.cart_session import CartSession
from edge.database import database
from edge.database.database import initialize_database
from edge.database.product_repository import add_product, get_active_products
from edge.detection.product_resolver import Detection, ProductInfo, ProductResolver
from edge.manual_demo import (DetectionEpisodeGuard, ManualInputRejected, SKU_CATALOG, build_manual_event,
                              submit_manual_event)
from tests.test_firebase_sync import _FakeReference


@pytest.fixture
def demo(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "manual-demo.sqlite3")
    initialize_database()
    for sku, spec in SKU_CATALOG.items():
        add_product(sku, sku, 5.0, spec.grams, yolo_class=spec.yolo_class)
    rows = get_active_products()
    products = [ProductInfo(row[0], row[5], row[1], row[3], row[4]) for row in rows]
    return rows, products


def detected(spec):
    # The operator selects the exact Maggi SKU even when vision reports generic Maggi.
    name = "Maggi" if spec.sku.startswith("MAGGI_") else spec.yolo_class
    return [Detection(name, .93)]


@pytest.mark.parametrize("sku", list(SKU_CATALOG))
def test_sku_maps_to_selected_catalog_product(demo, sku):
    rows, _ = demo
    spec = SKU_CATALOG[sku]
    event = build_manual_event(sku=sku, signed_grams=spec.grams,
                               detections=detected(spec), products=rows,
                               event_id=str(uuid.uuid4()))
    assert event["sku"] == sku
    assert event["yolo_class"] == spec.yolo_class
    assert event["product_id"] == next(row[0] for row in rows if row[5] == spec.yolo_class)
    assert event["weight_change"] == spec.grams
    assert event["source"] == "manual_demo"


@pytest.mark.parametrize("sku", list(SKU_CATALOG))
def test_each_sku_adds_and_removes_through_normal_billing(demo, sku):
    rows, products = demo
    spec = SKU_CATALOG[sku]
    session = CartSession(ProductResolver(products))
    _, added = submit_manual_event(session=session, sku=sku, signed_grams=spec.grams,
                                   detections=detected(spec), products=rows,
                                   event_id=f"{sku}-add")
    _, removed = submit_manual_event(session=session, sku=sku, signed_grams=-spec.grams,
                                     detections=detected(spec), products=rows,
                                     event_id=f"{sku}-remove")
    assert added.success and removed.success
    assert get_bill(session.session_id) == []
    assert [row[3] for row in get_session_log(session.session_id)] == ["ADD", "REMOVE"]


def test_invalid_weight_or_detection_is_rejected(demo):
    rows, _ = demo
    with pytest.raises(ManualInputRejected):
        build_manual_event(sku="LIFEBUOY", signed_grams=39,
                           detections=[Detection("Lifebuoy", .9)], products=rows,
                           event_id="event-1")
    with pytest.raises(ManualInputRejected):
        build_manual_event(sku="MAGGI_10", signed_grams=57,
                           detections=[Detection("Lays", .9)], products=rows,
                           event_id="event-2")


def test_removing_missing_product_is_rejected(demo):
    _, products = demo
    session = CartSession(ProductResolver(products))
    spec = SKU_CATALOG["LIFEBUOY"]
    result = session.process_remove([Detection(spec.yolo_class, .95)], -spec.grams,
                                    source="manual_demo", event_id="remove-empty")
    assert not result.success
    assert get_bill(session.session_id) == []
    assert get_session_log(session.session_id) == []


def test_replayed_event_id_does_not_add_a_second_unit(demo):
    rows, products = demo
    session = CartSession(ProductResolver(products))
    spec = SKU_CATALOG["LIFEBUOY"]
    args = dict(session=session, sku=spec.sku, signed_grams=spec.grams,
                detections=detected(spec), products=rows, event_id="same-event")
    _, first = submit_manual_event(**args)
    _, replay = submit_manual_event(**args)
    assert first.success
    assert not replay.success and "Duplicate event" in replay.reason
    assert get_bill(session.session_id)[0][2] == 1
    assert len(get_session_log(session.session_id)) == 1


def test_source_weight_and_id_are_persisted_and_published(demo):
    rows, products = demo
    root = _FakeReference()
    session = CartSession(ProductResolver(products), firebase_ref=root)
    spec = SKU_CATALOG["LIFEBUOY"]
    event_id = "visible-in-backend-event"
    _, result = submit_manual_event(session=session, sku=spec.sku, signed_grams=40,
                                    detections=detected(spec), products=rows,
                                    event_id=event_id)
    assert result.success
    details = json.loads(get_session_log(session.session_id)[0][5])
    assert details == {"source": "manual_demo", "event_id": event_id, "weight_change": 40.0}
    event = root.node(f"sessions/{session.session_id}/event_log").last_push_data
    assert event["source"] == "manual_demo"
    assert event["event_id"] == event_id
    assert event["weight_change"] == 40.0
    assert root.node(f"sessions/{session.session_id}/bill_items/1").last_set_data["quantity"] == 1
    _, removed = submit_manual_event(session=session, sku=spec.sku, signed_grams=-40,
                                    detections=detected(spec), products=rows,
                                    event_id="visible-in-backend-remove")
    assert removed.success
    remove_event = root.node(f"sessions/{session.session_id}/event_log").last_push_data
    assert remove_event["event_type"] == "REMOVE"
    assert remove_event["source"] == "manual_demo"
    assert remove_event["event_id"] == "visible-in-backend-remove"
    assert remove_event["weight_change"] == -40.0
    assert root.node(f"sessions/{session.session_id}").last_update_data["total_amount"] == 0.0


def test_detection_episode_guard_blocks_repeat_until_product_leaves_frame():
    guard = DetectionEpisodeGuard()
    first = Detection("Lifebuoy", .9)
    assert guard.available([first]) == [first]
    guard.consume("Lifebuoy")
    assert guard.available([first]) == []
    assert guard.available([]) == []
    assert guard.available([first]) == [first]

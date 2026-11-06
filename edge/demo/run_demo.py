"""
edge/demo/run_demo.py — Explicit software-demo runner.

SAFETY AND DEMO NOTICE
----------------------
1. EXPLICIT SOFTWARE-DEMO STARTUP: This runner mounts the explicit demo composition.
2. FakeProvider is for development/demo only and does NOT process real payments.
3. No real Razorpay credentials or networks are contacted.
4. Physical load-cell/HX711 input is NOT implemented in this milestone.
   Simulated weight input remains active.
5. SQLite database is isolated (default: data/smartcart_demo.db) to protect canonical user data.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import sys
import threading

from edge.demo.composition import create_demo_app, create_demo_payment_service
from edge.demo.prepare_demo_db import DEFAULT_DEMO_DB_PATH, prepare_demo_database
from edge.firebase import client as firebase_client
from edge.runtime.cart_runtime import resume_session
from edge.runtime.supervisor import CartSupervisor
from edge.runtime.weight_source import StdinSimulatedWeightSource, create_weight_source

logger = logging.getLogger("smartcart.demo")


def print_banner(cart_id: str, db_path: Path, host: str, port: int) -> None:
    print("=" * 64)
    print("      SMARTCART — EXPLICIT SOFTWARE-DEMO COMPOSITION")
    print("=" * 64)
    print("WARNING:")
    print("  - FakeProvider is for development/demo only and does NOT process real payments.")
    print("  - Physical load-cell/HX711 input is not implemented in this milestone.")
    print("  - Simulated weight events remain active.")
    print("=" * 64)
    print(f"  Cart ID       : {cart_id}")
    print(f"  Database      : {db_path}")
    print(f"  API Address   : http://{host}:{port}")
    print("=" * 64)
    print("Available Endpoints:")
    print("  POST /pair-cart")
    print("  POST /sessions/<id>/checkout")
    print("  POST /sessions/<id>/checkouts/<id>/cancel")
    print("  POST /sessions/<id>/checkouts/<id>/payment")
    print("  GET  /sessions/<id>/checkouts/<id>/payment")
    print("  POST /demo/sessions/<id>/checkouts/<id>/simulate-payment-success")
    print("=" * 64)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run explicit SmartCart software-only demo.")
    parser.add_argument("--cart-id", default="CART-001", help="Cart identifier to supervise (default: CART-001)")
    parser.add_argument("--host", default="0.0.0.0", help="API listen host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000, help="API listen port (default: 5000)")
    parser.add_argument("--db-path", default=str(DEFAULT_DEMO_DB_PATH), help="Isolated SQLite database path")
    parser.add_argument("--prepare-db", action="store_true", help="Prepare/seed isolated demo database before startup")
    parser.add_argument("--with-supervisor", action="store_true", help="Start background CartSupervisor for --cart-id")
    parser.add_argument("--software-input", action="store_true", help="Demo console with synthetic motion/vision; requires supervisor")
    parser.add_argument("--reconcile-demo-projections", action="store_true", help="Repair this dedicated demo cart from SQLite; stop ALL namespace writers first")
    args = parser.parse_args(argv)
    if args.software_input and not args.with_supervisor:
        parser.error('--software-input requires --with-supervisor')

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Use the same Admin configuration as normal startup before any requests
    # or runtime workers can run. Never log SDK errors containing credentials.
    try:
        firebase_client.initialize()
        firebase_ref = firebase_client.get_reference("/")
    except Exception:
        logger.error(
            "Demo startup failed: check SMARTCART_FIREBASE_URL and "
            "GOOGLE_APPLICATION_CREDENTIALS for valid backend Firebase configuration."
        )
        return 1

    db_path = Path(args.db_path).resolve()
    os.environ["SMARTCART_DATABASE_PATH"] = str(db_path)

    if args.prepare_db or not db_path.exists():
        logger.info("Preparing isolated demo database at %s...", db_path)
        prepare_demo_database(db_path)

    if args.reconcile_demo_projections:
        from edge.demo.reconcile import reconcile_demo_cart
        try:
            removed, rebuilt = reconcile_demo_cart(args.cart_id, root=firebase_ref)
            logger.info('Demo repair: removed %d active orphans; rebuilt %d sessions', removed, rebuilt)
        except Exception:
            logger.error('Demo projection reconciliation failed; startup stopped. Stop all writers and inspect identities/configuration.')
            return 1

    print_banner(args.cart_id, db_path, args.host, args.port)

    # 1. Initialize demo payment composition
    payment_service, fake_provider = create_demo_payment_service(firebase_ref=firebase_ref)

    # 2. Build demo Flask application
    app = create_demo_app(
        payment_service=payment_service,
        fake_provider=fake_provider,
    )

    supervisor = None
    sup_thread = None

    if args.with_supervisor:
        logger.info("Initializing CartSupervisor for %r...", args.cart_id)
        console = None
        if args.software_input:
            from edge.demo.software_input import SoftwareConsole, SoftwareDemoRuntime
            console = SoftwareConsole()
            logger.warning('Software fixtures enabled: synthetic motion/recognition, real resolver/billing. No camera or YOLO accuracy test.')
            threading.Thread(target=console.read, daemon=True).start()

        def runtime_factory(session_id: int):
            if console is not None:
                return SoftwareDemoRuntime(cart_id=args.cart_id, session_id=session_id,
                                           firebase_ref=firebase_ref, console=console)
            from edge.detection.camera import open_camera
            from edge.detection.mog2 import MOG2Processor
            from edge.model.yolo_detector import YOLODetector, DEFAULT_MODEL_PATH
            from edge.runtime.cart_runtime import CartRuntime

            detector = YOLODetector(model_path=DEFAULT_MODEL_PATH)
            session = resume_session(args.cart_id, detector, firebase_ref=firebase_ref)
            motion = MOG2Processor()
            camera = open_camera(0)
            weights = StdinSimulatedWeightSource(500.0)

            return CartRuntime(
                camera=camera,
                motion=motion,
                weights=weights,
                detector=detector,
                session=session,
                cart_id=args.cart_id,
            )

        supervisor = CartSupervisor(
            cart_id=args.cart_id,
            runtime_factory=runtime_factory,
            use_cart_lock=True,
        )
        sup_thread = threading.Thread(target=supervisor.run_forever, daemon=True)
        sup_thread.start()
        logger.info("CartSupervisor background thread started.")

    from waitress import serve
    logger.info("Starting Waitress HTTP server on %s:%d...", args.host, args.port)
    try:
        serve(app, host=args.host, port=args.port, threads=4, expose_tracebacks=False)
    except KeyboardInterrupt:
        logger.info("Demo server interrupted by user.")
    finally:
        if supervisor is not None:
            supervisor.request_stop()
            if sup_thread is not None:
                sup_thread.join(timeout=3.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

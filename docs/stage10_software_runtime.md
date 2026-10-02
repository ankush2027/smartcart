# Stage 10 — software automatic cart runtime

This is integration of existing components, not a second billing path. Stage 10 uses a real OpenCV camera/MOG2 boundary and explicitly simulated weight input. No HX711/GPIO implementation is included. Model injection is test-only; the CLI never substitutes a fake model.

## Start

Run from the authoritative backend checkout with its Python dependencies installed:

```sh
export SMARTCART_FIREBASE_URL='https://<approved-database-instance>'
export GOOGLE_APPLICATION_CREDENTIALS='/absolute/path/to/service-account.json'
python -m edge.runtime.run_cart \
  --cart-id CART-001 \
  --model /absolute/path/to/best.pt \
  --camera-index 0 \
  --simulate-weight \
  --initial-weight 500
```

All values above are placeholders/examples. Use the same backend checkout/SQLite file as the separately running pairing API (`python -m edge.api.run_server`). The runtime does not start that server, assign a customer, create a session, or complete a session on exit. Pair the authenticated customer through `POST /pair-cart` first: an allowed cart with no active session now gets a fresh owned session automatically. `SMARTCART_ALLOWED_CARTS` defaults to `CART-001`; see [pairing configuration](pairing_endpoint.md#allowed-physical-carts). Then start this runtime to resume that session. Missing, completed-only, or multiple active sessions still fail closed. A local advisory lock prevents two cooperating runtimes from owning the same database/cart on this host.

Completion preserves the old session and makes the cart logically available for a new pairing. A running runtime does not switch session IDs: dispatch rejects a changed active session and halts. Perform controlled shutdown/restart before serving the next customer's session. Runtime turnover, checkout/payment and Firebase repair remain separate work. Neither app close/logout nor runtime exit completes an unpaid session.

`--simulate-weight` is mandatory and startup visibly reports SIMULATED WEIGHT. In the terminal enter signed gram deltas, e.g. `75` or `-75`, while moving the corresponding item in camera view. Existing timed confirmation requires both signals within one second. The simulation starts at `--initial-weight` grams (default 500); choose a baseline that supports the intended simulated removals. Subthreshold changes accumulate against the last emitted-event baseline. Invalid/non-finite input or negative simulated totals stop the runtime. EOF stops it; Ctrl-C/SIGTERM requests graceful shutdown. No manual Python ADD/REMOVE call is needed per event.

## One component flow

```text
OpenCV frame → MOG2Processor → movement event
       └→ existing FrameBuffer / MotionPipeline / EventOrchestrator
simulated/injected WeightEvent ────────────────────────┘
       ↓ confirmed ADD/REMOVE + selected unannotated frame
bounded in-process queue → one serial worker → existing YOLODetector
       → CartSession.process_add/process_remove
       → existing ProductResolver → CartState + SQLite billing → existing Firebase sync
```

The MOG2 extraction preserves history 500, variance threshold 50, shadows, 5×5 morphology, area >500, five qualifying frames and once-per-gesture behavior. The standalone MOG2 demo remains. Recognition frames are copied before buffering and never annotated. The underlying MotionPipeline nearest-frame API retains its 30-frame capacity and two-second match age; confirmation remains the original one-second rule. Runtime recognition now uses the bounded post-movement policy described below.

The runtime accepts injected camera, motion processor, weight source, detector, CartSession, result callback and clock. A weight source implements `poll()` returning a WeightEvent or None and `close()`. Future physical sampling replaces this adapter, not the orchestration or billing implementation.

## Model and catalog requirements

The configured model path must be an existing file. `YOLODetector` exposes the loaded model vocabulary. Before billing, canonical `edge.database.product_repository` rows must contain valid finite prices and positive expected weights, unique nonempty YOLO classes, and exactly match the model's class vocabulary. No remapping, catalog seeding or guessed classes occur. Extra/missing/duplicate classes stop startup. The actual model loader also rejects unusable model files.

The repository's default `data/model_info/best.pt` is absent in this checkout. The observed local catalog is a Stage 8 test catalog; it has not been modified. Supply a real approved model and matching measured product catalog separately before a live run. No real inference or accuracy claim follows from the offline tests.

## Resume and session safety

Runtime looks up the unique active session for the configured cart through the existing session store. CartSession attaches to that session and reconstructs quantities/classes from SQLite bill items joined to canonical products. Missing/completed sessions, invalid quantities/classes, missing product joins and duplicate bill lines fail resume. No Firebase state is used for hydration and no customer UID is assigned.

Each dispatch rechecks the active session. Existing billing ADD/REMOVE now check authoritative active state inside a `BEGIN IMMEDIATE` transaction. Bill item, event log and total commit together. Completion takes the same transaction boundary, so a completed session cannot be mutated by a racing ADD/REMOVE. CartSession restores its prior in-memory REMOVE state if SQLite raises. No resolver algorithm, pairing transaction, Firebase schema or Flutter code changes are involved.

## Outcomes, duplicates and failures

- `committed`: the local mutation completed and configured synchronization returned.
- `unresolved`: no product decision or missing ADD frame; neither billing nor CartState changes.
- `rejected`: invalid/stale event, changed session, recognition failure or unsuccessful local operation.
- `ignored`: replayed/out-of-order movement or weight timestamp; no CartSession call.
- `committed_sync_failed`: local billing committed but the subsequent synchronization path raised. Never replay the ADD/REMOVE.

The existing Firebase exception type still propagates from CartSession, preserving callers/tests. A new `last_mutation_committed` property records the outcome of the most recent process call, including one that raises. The runtime's single worker inspects it immediately; it is not a multi-caller or durable transaction receipt.

A confirmation consumes its movement/weight timestamp identity before inference. Already-consumed or older channel timestamps cannot dispatch again. Events predating this runtime start, arriving from the future, or older than the configurable 30-second dispatch age limit are rejected. This is replay/staleness protection at dispatch, not a replacement confirmation algorithm. Timestamps must share the existing naive local datetime convention; clock jumps can cause safe rejection. The runtime does not persist/replay its queue on restart, and it cannot identify a replay whose upstream timestamps were falsified or regenerated.

The worker halts on unresolved, rejected or committed-sync-failed outcomes and discards queued follow-up events. Inspect/reconcile the physical/software cart and Firebase before restarting; do not repeat a committed physical operation to repair synchronization. There is no durable Firebase outbox, automatic retry or exactly-once distributed delivery claim. Firebase writes remain separate bill/metadata/event operations, so partial downstream state is possible. A startup does not automatically reconcile earlier failed Firebase writes.

## Concurrency and cleanup

One acquisition loop runs camera/MOG2/weight polling. One worker runs YOLO, resolution, CartSession and Firebase sequentially. The queue is bounded to eight confirmations; overflow explicitly halts instead of blocking acquisition indefinitely or silently dropping events. Expired queued events are rejected. Single-item gestures and one authoritative billing writer per cart are assumed; the lock does not prevent unrelated/manual writers or another host accessing a shared database.

Shutdown requests camera release and weight-source close, and joins acquisition, dispatch and cleanup threads within one shared deadline (five seconds by default; injectable for tests). Normal shutdown still waits for clean completion. A stuck dependency or cleanup error raises `ShutdownError`; the CLI returns failure and retains its cart lock until process exit. Runtime instances cannot be restarted. Threads are daemon threads: no unsafe thread termination is attempted, and an uninterruptible native call may remain alive until the process exits. Exit the process and reconcile SQLite/Firebase before restarting. In-flight SQLite/Firebase work may still finish; it is never retried. Inference returning after a shutdown timeout cannot start billing. A native extension holding the Python GIL indefinitely can still prevent Python supervision from running; process-level intervention is then required. There is no distributed queue, multiprocess inference service, pagination or schema extension.

## Validation and Stage 11

Tests use temporary SQLite databases, synthetic events, fake camera/weight boundaries and fake Firebase references. They exercise the existing YOLO adapter with an injected model, existing orchestration, actual resolver/CartSession/billing, resume, completion races, no-single-channel billing, raw frames, duplicates/stale events, rollback, committed Firebase failure without replay, queue backpressure, worker isolation, lock ownership and cleanup. Existing tests are retained unchanged.

Stage 11 still needs the real weight adapter, wiring/calibration/tare, filtering/stabilization, physical baseline recovery and measured timing/performance. Raspberry Pi deployment, load-cell operation, camera placement, lighting, real-world YOLO accuracy and full physical-cart validation are not proved here. REMOVE remains the existing weight-plus-before-CartState resolver contract; similar weights and multiple simultaneous units may remain unresolved. The runtime intentionally stops rather than inventing an answer.

## Implementation checkpoint

- Blocking-fix focused run: `python -m pytest tests/test_cart_runtime.py -q` — **44 passed** in 3.31 seconds.
- Full suite after blocking fixes: `python -m pytest tests -q` — **490 passed**, 0 failed (446 existing + 44 runtime cases), 5.55 seconds. Both runs used the existing project virtual environment.
- Syntax compilation of all 62 Python files under `edge/` and `tests/` passed without writing bytecode. No repository-configured lint/static-tool command was found.
- `python -B -m edge.runtime.run_cart --help` and `git diff --check` passed.
- Runtime wiring, injected detector/YOLO adapter, automatic CartSession dispatch and SQLite/Firebase integration are verified through offline tests with temporary SQLite and fake external boundaries.
- Real YOLO inference: NOT YET VERIFIED. `best.pt`: absent / pending. Raspberry Pi and physical-cart operation: NOT VERIFIED. HX711/load cell: NOT PART OF STAGE 10.
- Backend branch/HEAD remains `main` / `ca522a9`; changes are uncommitted. Flutter, Firebase schema/rules, secure pairing, ProductResolver, EventOrchestrator, MotionPipeline and FrameBuffer were not modified. No commit or push.


### Total consistency and shutdown regression checks

`calculate_total(session_id)` retains its float API (zero for a missing session),
but now only reads the authoritative committed `sessions.total_amount`. It no
longer recalculates or persists totals; ADD/REMOVE/completion maintain totals in
their existing atomic transaction. Completed sessions remain readable, never
rewritten by this helper. CartSession synchronizes committed metadata directly.
A concurrent read can observe an earlier committed snapshot, but cannot overwrite
a newer bill or total. Tests interleave a paused reader with committed ADD and
REMOVE operations and verify items, logs and totals remain consistent.

Injected blocking-camera, detector and cleanup tests exercise the shared shutdown
deadline, release-assisted clean shutdown, prevention of late inference billing,
and retained cart ownership after an uncertain shutdown.


## Event-aware recognition selection

Physical saved-frame evidence showed that movement-confirmation images could
precede product entry, while a +0.518-second image improved visibility but was
blurred. Runtime and diagnostic now share `RecognitionFrames` and the independent
`FrameSelector`. The inclusive candidate window defaults to movement +0.3 through
+0.8 seconds, encompassing the useful +0.5-second region. Both CLIs expose
`--recognition-window-start`, `--recognition-window-end`, and
`--recognition-wait` (default 1.5 seconds from correlation). The previous
`--recognition-delay` flag remains an alias for window start.

Acquisition/MOG2/stdin/correlation continue while the window fills, without sleeps
or continuous inference. Once a frame timestamp reaches the window end, the
selector scores eligible frames retained in the existing 30-frame buffer using
variance of the grayscale Laplacian, downscaled to a maximum dimension of 320
pixels. Highest score wins; equal scores choose the earliest timestamp, then
buffer order for identical timestamps. Scoring is bounded by buffer capacity and
image dimensions, and never edits the source image. It runs briefly on the
acquisition thread after the window completes, outside the pending-state lock.
YOLO runs once on the selected original frame through the existing worker.

Sharpness is not a guarantee of product visibility or correct recognition.
There is no minimum quality threshold or product-specific heuristic. One retained
candidate is sufficient. High frame rates or late correlation can evict earlier
candidates; selection considers only the retained eligible frames. No buffer
capacity/FPS assumption or recognition threshold is changed.

Pending selections remain bounded to eight and protected by a short metadata
lock. Original movement/weight timestamps and correlation delta are retained
alongside window bounds, selected timestamp/delta, score and candidate count.
Duplicate/out-of-order confirmations cannot queue another selection. Zero
candidates, scoring errors, overflow or deadline expiry halt without dispatching
the affected event. There is no early-frame fallback or automatic retry.

The supervisor checks the monotonic deadline every 0.1 seconds, including while
camera/scoring is blocked, then uses existing bounded shutdown/cleanup. Stop
cancels pending selection. Scoring returning after cancellation cannot dispatch.
Already-dispatched work retains existing completion semantics. Native calls
holding the GIL indefinitely remain a process-level interruption limitation.

The no-billing diagnostic uses exactly the same selector, prints temporal
metadata and candidate scores, saves the selected original pixels as lossless
PNG, infers once, and exits. Its synchronous camera read retains the existing
native-call interruption limitation; expiry is rejected when control returns.
No database or Firebase module is imported. Visual verification is required
before any separately approved physical billing retry.

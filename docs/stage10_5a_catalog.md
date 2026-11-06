# Stage 10.5A prototype catalog

`configs/prototype_catalog.json` is the explicit user-approved six-class definition.
Prices are prototype INR selling prices. All barcodes are NULL. IDs are allocated
by SQLite, never equated to model class indices or reused from Stage 8.

The five supplied gram values are printed NET weights used only as INITIAL
calibration values: noodles 50/25 g, BRU 3.7 g, Lays 25 g, Lifebuoy 40 g.
They are NOT measured packaged weights. Recalibration requires physical samples.
Coca-Cola Zero Sugar is a plastic 250 ml bottle; volume is NOT mass. The user
has approved 250 g solely as a **temporary prototype assumption**, recorded as
`temporary_prototype_assumption`. It is NOT measured packaged weight and is NOT
a conversion from 250 ml. Replace it during Stage 11 physical load-cell calibration. All six complete rows are required
by runtime; no partial five-row install or weakened vocabulary check is offered.

BRU remains 3.7 g. Validation warns that it is below the current 10 g event
boundary; catalog membership does not make single-unit BRU events operational.
The threshold and resolver tolerance remain unchanged. Noodles 25 g and Lays
25 g also have identical weight bands: REMOVE is ambiguous when both are in the
cart. This stage does not manufacture a disambiguation rule.

## Validate and install

From the backend root, validation alone (no SQLite access):

```sh
python -m edge.database.catalog_install
```

It now exits 0 with a complete six-product catalog, while reporting initial
calibration, BRU and temporary-assumption warnings. The temporary assumption is
allowed only for Coca Cola at exactly 250 g. Installation is eligible on a safe
empty database; the existing Stage 8 database remains protected and unchanged.
After Stage 11 measurement, update the value and `weight_basis` to
`measured_packaged`. Provenance is a human assertion: validation cannot prove a
number was physically measured.

Explicit installation, ONLY after the separate data-transition approval:

```sh
python -m edge.database.catalog_install --install
```

An optional `--catalog PATH` selects a reviewed definition. Installation uses the
canonical connection and one BEGIN IMMEDIATE transaction. An identical active
catalog is a no-op preserving IDs. Default installation refuses replacement;
Stage 10.5B adds the explicitly approved `--install --retire-stage8` transition.
The installer never initializes schema, completes sessions or syncs Firebase.

## Stage 10.5B approved active/retired transition

The canonical initializer adds `products.is_active INTEGER NOT NULL DEFAULT 1`
without recreating tables. Existing products initially remain active. Historical
repository APIs retain their six-column return shape and include retired rows;
`get_active_products()` supplies only active rows to runtime validation.

Before mutation, verify an SQLite backup and stop writers. Complete the old empty
session through CartSession with Firebase configured. Completion commits SQLite
first, then synchronizes metadata; a Firebase failure must be reported and never
replayed as a second completion or rolled back locally.

After completion, the explicit installer transition requires the original
Stage 8 row (ID 1 and unchanged metadata), no other products, and no active
sessions. It atomically retires ID 1 and inserts six new active rows. It never
changes historical IDs, metadata, events or sessions. Any insert failure rolls
back retirement too. An identical active catalog is a safe no-op on rerun.

```sh
python -m edge.database.catalog_install --install --retire-stage8
```

Runtime still demands exact equality of ACTIVE SQLite and model vocabularies.
Retired products remain readable for historical billing/event interpretation.
The lifecycle flag is local SQLite state: no Firebase product deletion, new
Firebase field, Flutter change or pairing change is required. No new session is
started automatically; a subsequent runtime requires a new securely paired
session through the existing lifecycle.

## Model and validation scope

Continue external `--model /Users/Ankush/Desktop/smart-cart/data/model_info/best.pt`.
Approved SHA-256: `13413bbf0a7c774b6b037bce10be2e16a9d3bbfd30dc2a365840f600e08fd0b3`.
The catalog validator checks that pinned identity and exact class vocabulary;
it does not load/hash the artifact. Verify the actual external file separately.
The existing runtime loads and checks model vocabulary; no inference/accuracy
claim or model copy follows from this installer. Weights remain gitignored.

Installation tests use temporary databases and the approved prototype catalog,
including its explicitly assumed Coca-Cola 250 g value. A separate future
measurement validation test uses synthetic data only inside its fixture.

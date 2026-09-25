# RTDB authorization and session discovery contract

SQLite remains authoritative. The backend Admin SDK synchronizes read-only
client data into RTDB. This change updates repository rules only; it does not
change deployed Firebase rules or Flutter code.

## Customer reads

Use exactly this collection query, with the authenticated Firebase UID:

    /sessions.orderByChild("customer_uid").equalTo(auth.uid)

The `/sessions` read predicate requires authentication and either the trusted
manager role or BOTH `query.orderByChild === 'customer_uid'` and
`query.equalTo === auth.uid`. `.indexOn: ["customer_uid"]` indexes this query.
Unrestricted customer collection reads and another UID's query remain denied.
No duplicate UID-to-session mapping is needed. The server filters the query;
Flutter must never download global sessions and filter ownership locally.

Firebase explicitly documents this query-rule pattern:
https://firebase.google.com/docs/database/security/rules-conditions#query-based_rules
Rules are not filters: a denied unfiltered parent read cannot be rescued by
child ownership rules. A permitted parent read cascades, so the customer grant
at that parent is deliberately limited to the owner-equality query. Direct
known-session reads independently require ownership or the manager role.
There are no grants below the session record that could expose foreign fields.
The root stays unreadable, and history remains protected by the same ownership.

## Active-session selection after the owner query

Inspect only the caller-owned result set, never a global session collection.
Do not add limitToFirst/limitToLast or paginate a partial result and claim it
proves that an active session is unique. Such limits can conceal conflicts.

- Zero owned records with absent/null end_time: no active session.
- Exactly one: use that record's Firebase key as the session ID.
- More than one: return a controlled `multiple_active_sessions` conflict, clear
  any selected cart, and require trusted backend/operator reconciliation.
  Do not choose arbitrarily, choose the newest, or merge bills.
- Any present non-null end_time denotes completed and is not a candidate.
  A malformed timestamp/record must produce a controlled data error, not be
  reclassified as active. Missing/malformed ownership must not be substituted
  with the caller UID; reject inconsistent data if encountered.

Completed sessions remain in Firebase. Future Flutter alignment implements
this selection; no client implementation or new persisted status field is
introduced here. The backend should normally maintain at most one active
session per UID, but current assignment helpers do not enforce that invariant.

## Manager, products and writes

The sole trusted manager claim is `auth.token.role === 'manager'`. The former
boolean `manager` claim alone grants no manager access. The role grant permits
session collection and individual-session reads needed by monitoring. It does
not grant a root read. Claims must be assigned by trusted admin tooling.

No current Stage 7 customer/manager repository needs `/products`: bill items
already contain names and prices. Customer product reads remain denied. The
existing manager-only product-read scope is preserved with the standardized
role claim; no new product permissions are introduced.

Every client write remains false, including managers: totals, bill items,
events, customer_uid, cart_id, session completion, products, deletes and parent
updates. Admin SDK bypass of client rules is unchanged.

## Ownership path verified and runtime gap

`create_session(cart_id=...)` persists the physical cart ID and leaves
customer_uid unassigned. `assign_customer_to_session(session_id, uid)` persists
ownership to SQLite. `CartSession.assign_customer(uid)` calls that helper and
then `_sync_session_meta()`, which sends the stored customer_uid/cart_id along
with session metadata through Firebase `update()`. Subsequent sync uses SQLite
ownership rather than inventing it in Flutter. The direct billing helper alone
does not synchronize Firebase. A Firebase reference must be configured; sync
exceptions propagate after SQLite has committed, so assignment must not be
reported as discoverable until sync succeeds (retry from SQLite on failure).

The repository contains no production caller of CartSession.assign_customer
outside the helper path/tests. The existing helpers trust the supplied UID;
they do not verify an ID token, authorize access to a physical cart, prevent
reassignment/completed-session assignment, or enforce one active session per
customer. Thus the storage/sync contract works, but a trusted runtime pairing
step is still required. It must authenticate/verify the caller's Firebase ID
token server-side, authorize the specific physical cart/session, validate an
active unassigned/authorized session, enforce the active-session invariant,
and call the backend assignment+sync path. No QR scanner, public assignment
endpoint, Flutter ownership write, or insecure UID inference is added here.

## Validation and deployment boundary

Existing pytest tests cover SQLite ownership and Firebase sync. Added tests
inspect the actual rule tree and evaluate its limited boolean/strict-equality
predicates offline for the allowed/denied cases. They are rule-contract tests,
not the Firebase rules compiler, query engine or a deployed-rules assertion.
No approved emulator workflow existed in this checkout, and no production
Firebase connection is required by these tests.

Before production deployment, review this rules file and run the same allow/
deny matrix against the RTDB emulator, including unfiltered and wrong-UID
queries, direct owned/foreign reads, nested reads, malformed ownership, old
claims, and all client writes. After explicit deployment authorization, open
Firebase Console for smartcart-16807 → Realtime Database → the configured
instance → Rules, compare current deployed rules with the reviewed
configs/firebase_rules.json, replace with that reviewed file and Publish.
Then verify with authorized customer and role=manager test accounts. Do not
represent this local change as deployed. Flutter Stage 7 query alignment and
trusted runtime pairing are separate follow-up tasks.

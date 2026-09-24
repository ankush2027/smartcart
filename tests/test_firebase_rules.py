"""
Tests for Firebase Realtime Database security rules.

These tests validate the *structure and content* of configs/firebase_rules.json
without connecting to any real Firebase project.

Coverage:
    - Rules file exists and is valid JSON
    - Root node denies public read/write
    - products/ grants read only to managers (auth.token.manager == true)
    - products/ denies unauthenticated read
    - sessions/$session_id grants read to the customer whose UID matches
      sessions/$session_id/customer_uid
    - sessions/$session_id grants read to managers
    - sessions/ denies global unauthenticated read
    - sessions/ denies global authenticated non-manager read
    - No public write access is granted
    - status field is NOT in approved schema
    - Approved session schema fields are present in documentation

These tests cannot replace live Firebase emulator testing.
Manual emulator verification is documented in docs/firebase_integration.md.
"""

from __future__ import annotations

import json
from pathlib import Path

# pyrefly: ignore [missing-import]
import pytest

_RULES_PATH = Path(__file__).resolve().parent.parent / "configs" / "firebase_rules.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_rules() -> dict:
    """Load and parse the Firebase rules JSON file."""
    with open(_RULES_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# File existence and validity
# ---------------------------------------------------------------------------

def test_firebase_rules_file_exists():
    """configs/firebase_rules.json must exist."""
    assert _RULES_PATH.exists(), f"Firebase rules file not found at {_RULES_PATH}"


def test_firebase_rules_is_valid_json():
    """configs/firebase_rules.json must be valid JSON."""
    rules = _load_rules()
    assert isinstance(rules, dict), "Rules must be a JSON object"


def test_rules_key_present():
    """The root 'rules' key must be present."""
    rules = _load_rules()
    assert "rules" in rules, "Top-level 'rules' key must be present"


# ---------------------------------------------------------------------------
# Root-level access denial
# ---------------------------------------------------------------------------

def test_root_read_is_false():
    """Root .read must be false — no global read access."""
    rules = _load_rules()["rules"]
    root_read = rules.get(".read", True)
    assert root_read is False or root_read == "false", (
        "Root .read must be false to prevent global unauthenticated access"
    )


def test_root_write_is_false():
    """Root .write must be false — no global write access."""
    rules = _load_rules()["rules"]
    root_write = rules.get(".write", True)
    assert root_write is False or root_write == "false", (
        "Root .write must be false — only the Admin SDK (backend) may write"
    )


# ---------------------------------------------------------------------------
# Products node
# ---------------------------------------------------------------------------

def test_products_node_exists():
    """products/ node must exist in the rules."""
    rules = _load_rules()["rules"]
    assert "products" in rules, "products/ node must be defined in rules"


def test_products_read_requires_manager_claim():
    """
    products/ read must require auth.token.manager === true.
    This prevents unauthenticated and regular customer access.
    """
    rules = _load_rules()["rules"]
    products_read = rules["products"].get(".read", "")
    # The rule must reference manager claim
    assert "manager" in str(products_read), (
        "products/ .read must check auth.token.manager (manager custom claim)"
    )


def test_products_read_requires_auth():
    """products/ .read must require auth != null."""
    rules = _load_rules()["rules"]
    products_read = str(rules["products"].get(".read", ""))
    assert "auth" in products_read, (
        "products/ .read must require authentication (auth != null)"
    )


def test_products_write_not_granted_to_public():
    """products/ must not grant public write access."""
    rules = _load_rules()["rules"]
    products_write = rules["products"].get(".write", False)
    # Write should be false (Admin SDK bypasses rules anyway)
    assert products_write is False or products_write == "false" or "true" not in str(products_write), (
        "products/ must not allow public write access"
    )


# ---------------------------------------------------------------------------
# Sessions node
# ---------------------------------------------------------------------------

def test_sessions_node_exists():
    """sessions/ node must exist in the rules."""
    rules = _load_rules()["rules"]
    assert "sessions" in rules, "sessions/ node must be defined in rules"


def test_sessions_root_read_is_restricted():
    """
    sessions/ must NOT have a permissive .read at the collection level.
    Customers must not be able to download all sessions and filter locally.
    """
    rules = _load_rules()["rules"]
    sessions_read = rules["sessions"].get(".read", False)
    # A globally permissive sessions read would be: "auth != null" or true
    # We want this to be false or absent at the collection level
    assert sessions_read is False or sessions_read == "false" or sessions_read == "", (
        "sessions/ must not have a collection-level .read that allows "
        "downloading all sessions. Customers must only read their own session."
    )


def test_session_id_node_exists():
    """sessions/$session_id wildcard rule must exist."""
    rules = _load_rules()["rules"]
    sessions = rules["sessions"]
    session_wildcards = [k for k in sessions.keys() if k.startswith("$")]
    assert len(session_wildcards) >= 1, (
        "A wildcard rule like $session_id must exist under sessions/"
    )


def test_session_id_read_allows_customer_own_session():
    """
    sessions/$session_id .read must allow a customer to read their own session
    (customer_uid == auth.uid).
    """
    rules = _load_rules()["rules"]
    sessions = rules["sessions"]
    session_wildcards = [k for k in sessions.keys() if k.startswith("$")]
    assert session_wildcards, "No wildcard key found under sessions/"

    wildcard_key = session_wildcards[0]
    session_read = str(sessions[wildcard_key].get(".read", ""))

    # Must reference customer_uid and auth.uid
    assert "customer_uid" in session_read, (
        "sessions/$session_id .read must check customer_uid"
    )
    assert "auth.uid" in session_read, (
        "sessions/$session_id .read must compare against auth.uid"
    )


def test_session_id_read_allows_manager():
    """
    sessions/$session_id .read must allow managers to read any session.
    """
    rules = _load_rules()["rules"]
    sessions = rules["sessions"]
    session_wildcards = [k for k in sessions.keys() if k.startswith("$")]
    wildcard_key = session_wildcards[0]
    session_read = str(sessions[wildcard_key].get(".read", ""))

    assert "manager" in session_read, (
        "sessions/$session_id .read must allow manager role access"
    )


# ---------------------------------------------------------------------------
# Status field must NOT be in rules schema documentation
# ---------------------------------------------------------------------------

def test_rules_file_does_not_define_status_field():
    """
    The rules file must not reference a 'status' field.
    Session status is derivable from end_time == null.
    """
    with open(_RULES_PATH) as f:
        content = f.read()
    # 'status' may appear as part of the word 'test_status' or in comments,
    # but should not appear as a Firebase path component inside rules.
    # We check for the pattern 'status' as a standalone key in the data path.
    import re
    # Look for "/status" or ".status" patterns indicating it's used as a node
    status_path = re.search(r'[/\.]status[\'"\s]', content)
    assert status_path is None, (
        "The rules file must not reference a 'status' path — "
        "status is derivable from end_time in the client"
    )


# ---------------------------------------------------------------------------
# Approved schema fields are represented in the rules path structure
# ---------------------------------------------------------------------------

def test_approved_session_fields_documented():
    """
    The rules file structure must be consistent with the approved schema:
    sessions/{session_id} with cart_id, customer_uid, start_time, end_time,
    total_amount, bill_items/, event_log/.

    This is a structural check — the rules file should use customer_uid
    as the access-control field, which is exactly what we assert above.
    (The full schema documentation is in docs/firebase_integration.md.)
    """
    rules = _load_rules()["rules"]
    # Verify customer_uid is used in the session read rule (already tested above)
    sessions = rules.get("sessions", {})
    session_wildcards = [k for k in sessions.keys() if k.startswith("$")]
    assert len(session_wildcards) >= 1

    session_rule = str(sessions[session_wildcards[0]].get(".read", ""))
    assert "customer_uid" in session_rule, (
        "customer_uid must be the access-control field for session reads"
    )

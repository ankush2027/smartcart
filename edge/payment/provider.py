"""Trusted adapter contract. Client evidence is never a verification result.

Adapters must authenticate/fetch authoritative facts. Recovery must be safe even
concurrently with an original, still-running create request: absence alone is
not permission to redispatch. No implementation here performs network I/O.
"""
from dataclasses import dataclass
from typing import Protocol


class ProviderUnavailable(Exception):
    """Outcome unknown; retain the reservation and recover the same attempt."""


class OrderRejected(Exception):
    """Authoritative terminal rejection: no successful/payable order exists."""


class InvalidEvidence(Exception):
    """Untrusted/malformed evidence; no authority to change payment state."""


@dataclass(frozen=True)
class OrderRequest:
    payment_attempt_id: str
    checkout_id: int
    provider_scope: str
    amount_paise: int
    currency: str


@dataclass(frozen=True)
class ProviderOrder:
    payment_attempt_id: str
    checkout_id: int
    provider_scope: str
    amount_paise: int
    currency: str
    order_id: str


@dataclass(frozen=True)
class ProviderPayment:
    payment_attempt_id: str
    checkout_id: int
    provider_scope: str
    amount_paise: int
    currency: str
    order_id: str
    payment_id: str
    status: str  # Only CAPTURED authorizes verification in this core.


class PaymentProvider(Protocol):
    scope: str

    def create_order(self, request: OrderRequest) -> ProviderOrder: ...

    def recover_order(self, request: OrderRequest) -> ProviderOrder | None:
        """Return the same order, or None when unresolved; never blind retry."""
        ...

    def fetch_payment(self, payment_id: str) -> ProviderPayment: ...

    def verify_client_evidence(self, order_id: str, evidence: dict) -> str:
        """Authenticate evidence and return an ID for independent retrieval."""
        ...

    def verify_webhook(self, body: bytes, signature: str) -> tuple[str, str]:
        """Future internal use: authenticate raw bytes, return event/payment IDs."""
        ...

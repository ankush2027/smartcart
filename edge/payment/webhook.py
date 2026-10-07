"""Provider-neutral authentication and durable ingestion; no HTTP route."""
import time

from .provider import InvalidEvidence
from . import webhook_store


class WebhookIngestor:
    def __init__(self, provider, *, clock=time.time):
        self.provider, self.clock = provider, clock

    def ingest(self, body, signature, *, event_id):
        if type(body) is not bytes or not 0 < len(body) <= 1048576:
            raise InvalidEvidence('Invalid webhook body')
        try:
            event = self.provider.authenticate_webhook(body, signature, event_id=event_id)
            webhook_store.validate(event, body)
            if event.provider_scope != self.provider.scope or event.event_id != event_id:
                raise InvalidEvidence()
        except Exception:
            raise InvalidEvidence('Webhook authentication failed') from None
        return webhook_store.insert_authenticated(event, body, clock=self.clock)

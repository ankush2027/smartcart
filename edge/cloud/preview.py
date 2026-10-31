"""Bounded single-process preview admission. No IP/header identity is trusted."""
from dataclasses import dataclass
import json
import math
import threading
import time

PREVIEW_KEYS = ('SMARTCART_PREVIEW_UIDS', 'SMARTCART_RATE_GLOBAL_PER_MINUTE',
                'SMARTCART_RATE_GLOBAL_BURST', 'SMARTCART_RATE_USER_PER_MINUTE',
                'SMARTCART_RATE_USER_BURST')


def approved_uids(values):
    if not isinstance(values, (list, tuple, set, frozenset)) or len(values) > 100:
        raise ValueError('Preview configuration requires at most 100 Firebase UIDs')
    if any(not isinstance(uid, str) or not 1 <= len(uid) <= 128 or uid != uid.strip()
           or any(ord(ch) < 32 for ch in uid) for uid in values):
        raise ValueError('Preview configuration contains an invalid Firebase UID')
    return frozenset(values)


@dataclass(frozen=True)
class RateLimits:
    global_per_minute: int = 120
    global_burst: int = 40
    user_per_minute: int = 30
    user_burst: int = 10

    def __post_init__(self):
        for value, maximum in ((self.global_per_minute, 6000), (self.global_burst, 500),
                               (self.user_per_minute, 1000), (self.user_burst, 100)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Rate limits must be positive integers within supported bounds')


def preview_settings(environment):
    try:
        users = approved_uids(json.loads(environment.get('SMARTCART_PREVIEW_UIDS', '[]')))
        defaults = RateLimits()
        names = ('global_per_minute', 'global_burst', 'user_per_minute', 'user_burst')
        limits = RateLimits(**{name: int(environment.get(key, getattr(defaults, name)))
                              for name, key in zip(names, PREVIEW_KEYS[1:])})
        return users, limits
    except (ValueError, TypeError):
        raise ValueError('Invalid preview UID JSON or rate-limit configuration') from None


class PreviewLimiter:
    """One shared bucket plus <=100 UID buckets; no attacker-controlled key growth.

    An initial burst refills continuously. Shared admission precedes routing and
    authentication, including health and errors. In open-customer mode, per-user
    buckets are allocated only for verified UIDs and evicted when idle, keeping
    memory bounded independently of customer registration volume. Counters reset
    on process restart; they are not shared across replicas. Locking makes
    concurrent admission atomic.
    """
    def __init__(self, users, limits=None, *, clock=time.monotonic):
        self.users = approved_uids(users)
        self.limits = limits or RateLimits()
        self.clock = clock
        self.lock = threading.Lock()
        now = clock()
        self.shared = [float(self.limits.global_burst), now]
        self.per_user = {uid: [float(self.limits.user_burst), now] for uid in self.users}

    def _take(self, bucket, per_minute, burst):
        now = self.clock()
        bucket[0] = min(float(burst), bucket[0] + max(0, now - bucket[1]) * per_minute / 60)
        bucket[1] = max(now, bucket[1])
        if bucket[0] >= 1:
            bucket[0] -= 1
            return 0
        return max(1, math.ceil((1 - bucket[0]) * 60 / per_minute))

    def global_retry_after(self):
        with self.lock:
            return self._take(self.shared, self.limits.global_per_minute, self.limits.global_burst)

    def user_retry_after(self, uid):
        with self.lock:
            now = self.clock()
            bucket = self.per_user.get(uid)
            if bucket is None:
                # Keep buckets for only the previous minute of activity. An
                # authenticated but unbounded UID population cannot grow memory.
                cutoff = now - 60
                self.per_user = {key: state for key, state in self.per_user.items()
                                 if state[1] >= cutoff}
                bucket = self.per_user.setdefault(uid, [float(self.limits.user_burst), now])
            return self._take(bucket, self.limits.user_per_minute, self.limits.user_burst)

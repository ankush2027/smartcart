/// Proposed client presentation fields, not a final backend contract.
class ManagedCart {
  const ManagedCart({
    required this.sessionId,
    required this.status,
    required this.itemCount,
    required this.totalPaise,
    this.updatedAt,
    this.cartId,
    this.customerUid,
    this.startTime,
    this.endTime,
  });
  final String sessionId;
  final String status;
  final int itemCount;
  final int totalPaise;

  /// Optional: RTDB has no freshness timestamp. Never synthesize one.
  final DateTime? updatedAt;
  final String? cartId;
  final String? customerUid;
  final DateTime? startTime;
  final DateTime? endTime;
}

class ManagerAlert {
  const ManagerAlert({
    required this.id,
    required this.severity,
    required this.status,
    required this.message,
    required this.timestamp,
    required this.sessionId,
  });
  final String id;

  /// Repository-supplied labels; Flutter does not classify conditions.
  final String severity;
  final String status;
  final String message;
  final DateTime timestamp;
  final String sessionId;
}

class ManagerEvent {
  const ManagerEvent({
    required this.id,
    required this.label,
    required this.message,
    required this.timestamp,
    required this.sessionId,
    this.eventType,
    this.productId,
  });
  final String? eventType;
  final String? productId;
  final String id;
  final String label;
  final String message;
  final DateTime timestamp;
  final String sessionId;
}

class ManagerOverview {
  ManagerOverview({
    required List<ManagedCart> activeCarts,
    required List<ManagerAlert> alerts,
    required List<ManagerEvent> events,
  }) : activeCarts = List.unmodifiable(activeCarts),
       alerts = List.unmodifiable(alerts),
       events = List.unmodifiable(events);

  /// Repository supplies the active-cart and current-alert collections.
  final List<ManagedCart> activeCarts;
  final List<ManagerAlert> alerts;
  final List<ManagerEvent> events;
}

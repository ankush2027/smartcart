import '../../features/customer/models/customer_cart.dart';
import '../../features/manager/models/manager_overview.dart';
import 'repository_failure.dart';

const _invalid = RepositoryFailure(RepositoryFailureCode.invalidData);

/// Decimal half-up rounding of the received number's decimal representation.
/// No binary floating-point multiplication is used; all domain money is paise.
int rupeesToPaise(Object? value) {
  if (value is! num || !value.isFinite || value < 0) throw _invalid;
  final parts = value.toString().toLowerCase().split('e');
  final decimal = parts.first.split('.');
  final fraction = decimal.length == 2 ? decimal.last : '';
  final coefficient = BigInt.parse('${decimal.first}$fraction');
  final power =
      (parts.length == 2 ? int.parse(parts.last) : 0) + 2 - fraction.length;
  BigInt paise;
  if (power >= 0) {
    paise = coefficient * BigInt.from(10).pow(power);
  } else {
    final divisor = BigInt.from(10).pow(-power);
    paise = coefficient ~/ divisor;
    if ((coefficient % divisor) * BigInt.two >= divisor) paise += BigInt.one;
  }
  // Keep values exactly representable on both Dart VM and web.
  if (paise > BigInt.from(9007199254740991)) throw _invalid;
  return paise.toInt();
}

Map<String, Object?> _record(Object? value) {
  if (value is! Map || value.keys.any((key) => key is! String)) throw _invalid;
  return value.cast<String, Object?>();
}

/// RTDB can decode dense numeric-key children as lists. Preserve their keys.
Map<String, Object?> children(Object? value) {
  if (value == null) return {};
  if (value is List) {
    return {
      for (var i = 0; i < value.length; i++)
        if (value[i] != null) '$i': value[i],
    };
  }
  return _record(value);
}

String _text(Object? value) {
  if (value is! String || value.trim().isEmpty) throw _invalid;
  return value;
}

String? _optionalText(Object? value) => value == null ? null : _text(value);

/// Backend SQLite datetime('now') values have UTC semantics without a suffix.
/// Explicit ISO timezone offsets are honored. Reject overflowed calendar dates.
DateTime _time(Object? value) {
  final raw = _text(value);
  final match = RegExp(
    r'^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})?$',
  ).firstMatch(raw);
  if (match == null) throw _invalid;
  final year = int.parse(match[1]!);
  final month = int.parse(match[2]!);
  final day = int.parse(match[3]!);
  final hour = int.parse(match[4]!);
  final minute = int.parse(match[5]!);
  final second = int.parse(match[6]!);
  final calendar = DateTime.utc(year, month, day);
  if (calendar.year != year ||
      calendar.month != month ||
      calendar.day != day ||
      hour > 23 ||
      minute > 59 ||
      second > 59) {
    throw _invalid;
  }
  final zone = match[8];
  if (zone != null &&
      zone != 'Z' &&
      (int.parse(zone.substring(1, 3)) > 23 ||
          int.parse(zone.substring(4)) > 59)) {
    throw _invalid;
  }
  final parsed = DateTime.tryParse(zone == null ? '${raw}Z' : raw);
  if (parsed == null) throw _invalid;
  return parsed.toUtc();
}

int _quantity(Object? value) {
  if (value is! num ||
      !value.isFinite ||
      value <= 0 ||
      value > 9007199254740991 ||
      value != value.truncateToDouble()) {
    throw _invalid;
  }
  return value.toInt();
}

class SessionData {
  SessionData({required this.cart, required List<ManagerEvent> events})
    : events = List.unmodifiable(events);
  final CustomerCart cart;
  final List<ManagerEvent> events;
}

SessionData parseSession(String sessionId, Object? value) {
  final record = _record(value);
  final start = _time(record['start_time']);
  final end = record['end_time'] == null ? null : _time(record['end_time']);
  if (end != null && end.isBefore(start)) throw _invalid;
  final items = children(record['bill_items']).entries.map((entry) {
    final item = _record(entry.value);
    return CartItem(
      product: CartProduct(id: entry.key, name: _text(item['product_name'])),
      quantity: _quantity(item['quantity']),
      unitPricePaise: rupeesToPaise(item['price']),
      lineTotalPaise: rupeesToPaise(item['total']),
    );
  }).toList();
  final subtotal = items.fold(0, (sum, item) => sum + item.lineTotalPaise);
  final count = items.fold(0, (sum, item) => sum + item.quantity);
  if (subtotal > 9007199254740991 || count > 9007199254740991) throw _invalid;
  final cart = CustomerCart(
    displayId: sessionId,
    cartId: _optionalText(record['cart_id']),
    customerUid: _optionalText(record['customer_uid']),
    startTime: start,
    endTime: end,
    status: end == null ? CartStatus.active : CartStatus.completed,
    items: items,
    // Presentation subtotal only; never substitutes for total_amount.
    subtotalPaise: subtotal,
    totalPaise: rupeesToPaise(record['total_amount']),
  );
  final events = <ManagerEvent>[];
  // Activity is optional. A malformed event never invalidates a valid bill.
  Map<String, Object?> eventChildren;
  try {
    eventChildren = children(record['event_log']);
  } on RepositoryFailure {
    eventChildren = {};
  }
  for (final entry in eventChildren.entries) {
    try {
      final event = _record(entry.value);
      final type = _text(event['event_type']);
      final product = event['product_id'];
      final String productId;
      if (product is String) {
        productId = _text(product);
      } else if (product is num &&
          product.isFinite &&
          product >= 0 &&
          product <= 9007199254740991 &&
          product == product.truncateToDouble()) {
        productId = product.toInt().toString();
      } else {
        throw _invalid;
      }
      events.add(
        ManagerEvent(
          id: entry.key,
          sessionId: sessionId,
          eventType: type,
          productId: productId,
          timestamp: _time(event['timestamp']),
          label: switch (type) {
            'ADD' => 'Product added',
            'REMOVE' => 'Product removed',
            _ => 'Cart activity',
          },
          message: 'Product $productId',
        ),
      );
    } on RepositoryFailure {
      continue;
    }
  }
  events.sort((a, b) => a.timestamp.compareTo(b.timestamp));
  return SessionData(cart: cart, events: events);
}

ManagerOverview parseManagerOverview(Object? value) {
  final sessions = children(value).entries
      .map((entry) => parseSession(entry.key, entry.value))
      .toList();
  final events = sessions.expand((session) => session.events).toList()
    ..sort((a, b) => b.timestamp.compareTo(a.timestamp));
  return ManagerOverview(
    activeCarts: [
      for (final session in sessions)
        if (session.cart.status == CartStatus.active)
          _managedCart(session.cart),
    ],
    completedCarts: [
      for (final session in sessions)
        if (session.cart.status == CartStatus.completed)
          _managedCart(session.cart),
    ],
    alerts: const [],
    events: events,
  );
}

ManagedCart _managedCart(CustomerCart cart) => ManagedCart(
  sessionId: cart.displayId,
  cartId: cart.cartId,
  customerUid: cart.customerUid,
  startTime: cart.startTime,
  endTime: cart.endTime,
  status: cart.status == CartStatus.active ? 'Active' : 'Completed',
  itemCount: cart.itemCount,
  totalPaise: cart.totalPaise,
  items: cart.items,
);

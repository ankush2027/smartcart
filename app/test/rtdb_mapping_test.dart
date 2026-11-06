import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/core/data/repository_failure.dart';
import 'package:smartcart/core/data/session_mapper.dart';
import 'package:smartcart/features/customer/models/customer_cart.dart';

Map<String, Object?> sessionFixture({
  String? owner = 'customer-uid',
  bool completed = false,
}) => {
  'cart_id': 'cart-9',
  'customer_uid': owner,
  'start_time': '2026-09-23 10:00:00',
  'end_time': completed ? '2026-09-23 10:20:00' : null,
  'total_amount': 30.15,
  'bill_items': {
    '12': {
      'product_name': 'Snack',
      'quantity': 2,
      'price': 10.25,
      'total': 20.5,
    },
    '14': {'product_name': 'Drink', 'quantity': 1.0, 'price': 10, 'total': 10},
  },
  'event_log': <String, Object?>{
    '-push-add': {
      'timestamp': '2026-09-23 10:01:00',
      'event_type': 'ADD',
      'product_id': 12,
    },
    '-push-remove': {
      'timestamp': '2026-09-23T10:02:00Z',
      'event_type': 'REMOVE',
      'product_id': 14.0,
    },
  },
};

void main() {
  test('active session preserves metadata, child IDs, quantities and authoritative totals', () {
    final result = parseSession('session-42', sessionFixture());
    final cart = result.cart;
    expect(cart.displayId, 'session-42');
    expect(cart.cartId, 'cart-9');
    expect(cart.customerUid, 'customer-uid');
    expect(cart.startTime, DateTime.utc(2026, 9, 23, 10));
    expect(cart.endTime, isNull);
    expect(cart.status, CartStatus.active);
    expect(cart.items.first.product.id, '12');
    expect(cart.items.first.product.name, 'Snack');
    expect(cart.items.first.quantity, 2);
    expect(cart.items.first.unitPricePaise, 1025);
    expect(cart.items.first.lineTotalPaise, 2050);
    expect(cart.itemCount, 3);
    expect(cart.subtotalPaise, 3050);
    expect(cart.totalPaise, 3015); // Never recompute the backend total.
    expect(() => cart.items.clear(), throwsUnsupportedError);
  });

  test('completed session derives status exclusively from end_time', () {
    final fixture = sessionFixture(completed: true)..['status'] = 'active';
    final cart = parseSession('42', fixture).cart;
    expect(cart.status, CartStatus.completed);
    expect(cart.endTime, DateTime.utc(2026, 9, 23, 10, 20));
    expect(
      parseSession('42', sessionFixture()..remove('end_time')).cart.status,
      CartStatus.active,
    );
  });

  test(
    'empty bill/events and unassigned optional identity fields are supported',
    () {
      final fixture = sessionFixture(owner: null)
        ..remove('cart_id')
        ..remove('bill_items')
        ..remove('event_log');
      final data = parseSession('42', fixture);
      expect(data.cart.customerUid, isNull);
      expect(data.cart.cartId, isNull);
      expect(data.cart.items, isEmpty);
      expect(data.cart.itemCount, 0);
      expect(data.cart.subtotalPaise, 0);
      expect(data.cart.totalPaise, 3015);
      expect(data.events, isEmpty);
    },
  );

  for (final entry in <num, int>{
    0: 0,
    10: 1000,
    10.25: 1025,
    1.005: 101,
    2.675: 268,
    0.0049: 0,
    0.005: 1,
    1e-7: 0,
    123.456: 12346,
    0.1 + 0.2: 30,
  }.entries) {
    test(
      'rupees ${entry.key} rounds decimal half-up to ${entry.value} paise',
      () {
        expect(rupeesToPaise(entry.key), entry.value);
      },
    );
  }
  test('whole-number doubles and signed zero are valid', () {
    expect(rupeesToPaise(10.0), 1000);
    expect(rupeesToPaise(-0.0), 0);
  });
  for (final value in [
    null,
    '10',
    true,
    -1,
    double.nan,
    double.infinity,
    1e20,
  ]) {
    test('invalid money is rejected: $value', () {
      expect(() => rupeesToPaise(value), throwsA(isA<RepositoryFailure>()));
    });
  }

  test('ADD/REMOVE retain push key, type, product and real timestamps without alerts', () {
    final events = parseSession('42', sessionFixture()).events;
    expect(events.map((e) => e.id), ['-push-add', '-push-remove']);
    expect(events.map((e) => e.eventType), ['ADD', 'REMOVE']);
    expect(events.map((e) => e.productId), ['12', '14']);
    expect(events.map((e) => e.label), ['Product added', 'Product removed']);
    expect(events.first.timestamp, DateTime.utc(2026, 9, 23, 10, 1));
    expect(events.first.sessionId, '42');
    expect(events.first.message, 'Product 12');
  });

  test(
    'malformed optional events are skipped and unknown types stay neutral',
    () {
      final fixture = sessionFixture();
      (fixture['event_log'] as Map<String, Object?>).addAll({
        'bad': {'timestamp': 'garbage', 'event_type': 'ADD', 'product_id': 1},
        'scalar': 42,
        'future': {
          'timestamp': '2026-09-23 10:03:00',
          'event_type': 'FUTURE',
          'product_id': '12',
        },
      });
      final events = parseSession('42', fixture).events;
      expect(events.length, 3);
      expect(events.last.label, 'Cart activity');
      fixture['event_log'] = 'malformed';
      expect(parseSession('42', fixture).events, isEmpty);
    },
  );

  test('dense numeric RTDB collections retain their numeric child keys', () {
    final fixture = sessionFixture()
      ..['bill_items'] = [
        null,
        {'product_name': 'Snack', 'quantity': 1, 'price': 1, 'total': 1},
      ];
    expect(parseSession('42', fixture).cart.items.single.product.id, '1');
    expect(
      parseManagerOverview([null, fixture]).activeCarts.single.sessionId,
      '1',
    );
  });

  test(
    'invalid required data and malformed bill never produce a partial bill',
    () {
      for (final entry in {
        'start_time': '2026-02-30 10:00:00',
        'end_time': 'invalid',
        'total_amount': null,
        'bill_items': {
          '12': {
            'product_name': 'Snack',
            'quantity': 0,
            'price': 10,
            'total': 10,
          },
        },
      }.entries) {
        expect(
          () => parseSession('42', sessionFixture()..[entry.key] = entry.value),
          throwsA(isA<RepositoryFailure>()),
        );
      }
      expect(() => parseSession('42', 10), throwsA(isA<RepositoryFailure>()));
    },
  );

  test(
    'manager maps active carts, real activity, no alerts and no fake updatedAt',
    () {
      final overview = parseManagerOverview({
        '42': sessionFixture(),
        '43': sessionFixture(completed: true),
      });
      expect(overview.activeCarts.length, 1);
      final cart = overview.activeCarts.single;
      expect(cart.sessionId, '42');
      expect(cart.cartId, 'cart-9');
      expect(cart.customerUid, 'customer-uid');
      expect(cart.itemCount, 3);
      expect(cart.totalPaise, 3015);
      expect(cart.status, 'Active');
      expect(cart.updatedAt, isNull);
      expect(cart.startTime, DateTime.utc(2026, 9, 23, 10));
      expect(overview.alerts, isEmpty);
      expect(overview.events.length, 4);
      expect(overview.events.first.eventType, 'REMOVE');
      expect(() => overview.events.clear(), throwsUnsupportedError);
      expect(parseManagerOverview(null).activeCarts, isEmpty);
    },
  );
}

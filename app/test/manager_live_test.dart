import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/app.dart';
import 'package:smartcart/core/data/repository_failure.dart';
import 'package:smartcart/core/data/session_mapper.dart';
import 'package:smartcart/features/auth/models/auth_user.dart';
import 'package:smartcart/features/customer/data/mock_cart_repository.dart';
import 'package:smartcart/features/manager/controllers/manager_controller.dart';
import 'package:smartcart/features/manager/data/firebase_manager_repository.dart';
import 'package:smartcart/features/manager/screens/manager_screen.dart';

import 'rtdb_mapping_test.dart' show sessionFixture;
import 'rtdb_repository_test.dart'
    show LiveAuth, FakeReader, manager, customer, flush;

class ManagerAuth extends LiveAuth {
  ManagerAuth() : super(manager);
  @override
  Future<void> signOut() async => change(null);
}

void main() {
  test('manager maps active and completed sessions with immutable bills and push IDs', () {
    final overview = parseManagerOverview({
      'active-key': sessionFixture(),
      'completed-key': sessionFixture(completed: true),
    });
    final active = overview.activeCarts.single;
    final completed = overview.completedCarts.single;
    expect(active.sessionId, 'active-key');
    expect(active.cartId, 'cart-9');
    expect(active.customerUid, 'customer-uid');
    expect(active.status, 'Active');
    expect(completed.status, 'Completed');
    expect(completed.endTime, DateTime.utc(2026, 9, 23, 10, 20));
    expect(completed.items.first.product.name, 'Snack');
    expect(completed.items.first.quantity, 2);
    expect(completed.items.first.unitPricePaise, 1025);
    expect(completed.items.first.lineTotalPaise, 2050);
    expect(completed.totalPaise, 3015);
    expect(active.itemCount, 3);
    expect(active.updatedAt, isNull);
    expect(overview.alerts, isEmpty);
    expect(overview.events.map((e) => e.id).toSet(), {
      '-push-add',
      '-push-remove',
    });
    expect(overview.events.map((e) => e.label).toSet(), {
      'Product added',
      'Product removed',
    });
    expect(() => completed.items.clear(), throwsUnsupportedError);
    expect(() => overview.completedCarts.clear(), throwsUnsupportedError);
  });

  test('optional absent values and malformed events remain safe', () {
    final fixture = sessionFixture(owner: null)
      ..remove('cart_id')
      ..remove('bill_items')
      ..['event_log'] = {
        'bad': {'timestamp': false},
        'null': null,
      };
    final overview = parseManagerOverview({'s': fixture});
    expect(overview.activeCarts.single.cartId, isNull);
    expect(overview.activeCarts.single.customerUid, isNull);
    expect(overview.activeCarts.single.items, isEmpty);
    expect(overview.events, isEmpty);
    expect(parseManagerOverview(null).activeCarts, isEmpty);
    expect(
      () => parseManagerOverview({'s': fixture..['cart_id'] = 7}),
      throwsA(isA<RepositoryFailure>()),
    );
  });

  test(
    'live new session, bill, total, ADD, REMOVE and completion use one read',
    () async {
      final auth = ManagerAuth();
      final reader = FakeReader();
      final repo = FirebaseManagerRepository(auth: auth, reader: reader);
      final controller = ManagerController(repo);
      await controller.load();
      reader.data.add(null);
      await flush();
      expect(controller.overview!.activeCarts, isEmpty);
      final fixture = sessionFixture()..['event_log'] = null;
      reader.data.add({'s': fixture});
      await flush();
      expect(controller.overview!.activeCarts.single.totalPaise, 3015);
      fixture['total_amount'] = 99.99;
      fixture['bill_items'] = {
        'p': {
          'product_name': 'Changed',
          'quantity': 3,
          'price': 2.25,
          'total': 6.75,
        },
      };
      fixture['event_log'] = {
        'push1': {
          'timestamp': '2026-09-23 10:01:00',
          'event_type': 'ADD',
          'product_id': 'p',
        },
      };
      reader.data.add({'s': fixture});
      await flush();
      expect(controller.overview!.activeCarts.single.totalPaise, 9999);
      expect(controller.overview!.activeCarts.single.items.single.quantity, 3);
      expect(controller.overview!.events.single.id, 'push1');
      fixture['event_log'] = {
        'push2': {
          'timestamp': '2026-09-23 10:02:00',
          'event_type': 'REMOVE',
          'product_id': 'p',
        },
      };
      fixture['end_time'] = '2026-09-23 10:20:00';
      reader.data.add({'s': fixture});
      await flush();
      expect(controller.overview!.activeCarts, isEmpty);
      expect(controller.overview!.completedCarts.single.sessionId, 's');
      expect(controller.overview!.events.single.label, 'Product removed');
      expect(reader.paths, ['sessions']);
      controller.dispose();
      await flush();
      expect(reader.active, 0);
      expect(controller.overview, isNull);
      await repo.dispose();
      await reader.close();
      await auth.changes.close();
    },
  );

  test('repository constructed before login binds to manager at subscription and retry recovers', () async {
    final auth = LiveAuth(null);
    final reader = FakeReader();
    final repo = FirebaseManagerRepository(auth: auth, reader: reader);
    auth.change(manager);
    final controller = ManagerController(repo);
    await controller.load();
    reader.data.addError(
      const RepositoryFailure(RepositoryFailureCode.permissionDenied),
    );
    await flush();
    expect(controller.state, ManagerLoadState.error);
    expect(controller.error, contains('access'));
    expect(controller.overview, isNull);
    await controller.load();
    reader.data.add({'new': sessionFixture()});
    await flush();
    expect(controller.state, ManagerLoadState.ready);
    expect(reader.paths.length, 2);
    controller.dispose();
    await repo.dispose();
    await reader.close();
    await auth.changes.close();
  });

  test('identity loss clears controller and new subscription cannot replay old data', () async {
    final auth = ManagerAuth();
    final reader = FakeReader();
    final repo = FirebaseManagerRepository(auth: auth, reader: reader);
    final controller = ManagerController(repo);
    await controller.load();
    final old = reader.data;
    old.add({'old': sessionFixture()});
    auth.change(const AuthUser(uid: 'manager-b', role: UserRole.manager));
    await flush();
    expect(controller.overview, isNull);
    expect(reader.active, 0);
    await controller.load();
    old.add({'stale': sessionFixture()});
    reader.data.add({'new': sessionFixture()});
    await flush();
    expect(controller.overview!.activeCarts.single.sessionId, 'new');
    auth.change(customer);
    await flush();
    expect(controller.overview, isNull);
    expect(reader.active, 0);
    await controller.load();
    await flush();
    expect(controller.state, ManagerLoadState.error);
    expect(reader.paths.length, 2);
    controller.dispose();
    await repo.dispose();
    await reader.close();
    await auth.changes.close();
  });

  test(
    'paused manager subscription rejects queued values after identity loss',
    () async {
      final auth = ManagerAuth();
      final reader = FakeReader();
      final repo = FirebaseManagerRepository(auth: auth, reader: reader);
      final values = <Object>[];
      final errors = <Object>[];
      final subscription = repo.watchOverview().listen(
        values.add,
        onError: errors.add,
      );
      subscription.pause();
      reader.data.add({'old': sessionFixture()});
      await flush();
      auth.change(null);
      subscription.resume();
      await flush();
      expect(values, isEmpty);
      expect(errors, isNotEmpty);
      expect(reader.active, 0);
      await subscription.cancel();
      await repo.dispose();
      await reader.close();
      await auth.changes.close();
    },
  );

  testWidgets('production manager has no mock fallback on Firebase failure', (
    tester,
  ) async {
    final auth = ManagerAuth();
    await tester.pumpWidget(
      SmartCartApp(authRepository: auth, cartRepository: MockCartRepository()),
    );
    auth.change(manager);
    await tester.pumpAndSettle();
    final screen = tester.widget<ManagerScreen>(find.byType(ManagerScreen));
    expect(screen.repository, isA<FirebaseManagerRepository>());
    expect(screen.repository.isDemo, isFalse);
    expect(find.text('Retry'), findsOneWidget);
    expect(find.textContaining('Development demo'), findsNothing);
    expect(find.text('Cart SC-001'), findsNothing);
    await tester.pumpWidget(const SizedBox.shrink());
    await tester.runAsync(auth.changes.close);
  });

  testWidgets(
    'live dashboard shows bills, completed sessions, neutral activity and logout clears',
    (tester) async {
      final auth = ManagerAuth();
      final reader = FakeReader();
      final repo = FirebaseManagerRepository(auth: auth, reader: reader);
      await tester.pumpWidget(
        SmartCartApp(
          authRepository: auth,
          cartRepository: MockCartRepository(),
          managerRepository: repo,
        ),
      );
      auth.change(manager);
      await tester.pump();
      expect(find.text('Loading manager data...'), findsOneWidget);
      reader.data.add(null);
      await tester.pumpAndSettle();
      expect(find.text('No active carts'), findsOneWidget);
      expect(find.text('No completed sessions'), findsOneWidget);
      expect(find.text('No recent activity'), findsOneWidget);
      reader.data.add({'s': sessionFixture()});
      await tester.pumpAndSettle();
      expect(find.text('Active Carts: 1'), findsOneWidget);
      expect(find.text('Cart ID: cart-9'), findsOneWidget);
      expect(find.textContaining('Snack · Quantity: 2'), findsOneWidget);
      expect(find.text('Alerts'), findsNothing);
      reader.data.add({
        's': sessionFixture(completed: true)..['bill_items'] = null,
      });
      await tester.pumpAndSettle();
      expect(find.text('Active Carts: 0'), findsOneWidget);
      expect(find.text('Completed Sessions: 1'), findsOneWidget);
      await tester.scrollUntilVisible(find.text('No bill items'), 250);
      expect(find.text('No bill items'), findsOneWidget);
      await tester.scrollUntilVisible(find.text('Product removed'), 250);
      expect(find.text('Product removed'), findsOneWidget);
      await tester.scrollUntilVisible(find.text('Logout'), 250);
      await tester.tap(find.text('Logout'));
      await tester.pumpAndSettle();
      expect(find.text('Welcome Back'), findsOneWidget);
      expect(reader.active, 0);
      reader.data.add({'stale': sessionFixture()});
      await tester.pump();
      expect(find.byType(ManagerScreen), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
      await tester.runAsync(repo.dispose);
      await tester.runAsync(reader.close);
      await tester.runAsync(auth.changes.close);
    },
  );
}

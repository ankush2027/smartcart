import 'package:smartcart/features/customer/data/mock_cart_repository.dart';

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/app.dart';
import 'package:smartcart/features/auth/data/in_memory_auth_repository.dart';
import 'package:smartcart/features/auth/models/auth_user.dart';
import 'package:smartcart/features/manager/controllers/manager_controller.dart';
import 'package:smartcart/features/manager/data/manager_repository.dart';
import 'package:smartcart/features/manager/data/mock_manager_repository.dart';
import 'package:smartcart/features/manager/models/manager_overview.dart';

class TestManagerRepository implements ManagerRepository {
  int calls = 0;
  Future<ManagerOverview> Function() response = () async => emptyOverview();
  @override
  bool get isDemo => false;
  @override
  Future<ManagerOverview> loadOverview() {
    calls++;
    return response();
  }
}

ManagerOverview emptyOverview() =>
    ManagerOverview(activeCarts: [], alerts: [], events: []);
Future<void> showManager(
  WidgetTester tester,
  ManagerRepository repository,
) async {
  final auth = InMemoryAuthRepository(delay: Duration.zero)
    ..selectedRole = UserRole.manager;
  await tester.runAsync(
    () => auth.signIn(email: 'demo@example.test', password: 'demo'),
  );
  await tester.pumpWidget(
    SmartCartApp(
      cartRepository: MockCartRepository(),
      authRepository: auth,
      managerRepository: repository,
    ),
  );
  await tester.pump();
}

Future<void> reveal(WidgetTester tester, String text) async {
  await tester.scrollUntilVisible(find.text(text), 250, maxScrolls: 60);
  await tester.ensureVisible(find.text(text));
  await tester.pumpAndSettle();
}

void main() {
  test(
    'mock provides immutable monitoring snapshots and explicit totals',
    () async {
      final data = await MockManagerRepository(delay: Duration.zero)
          .loadOverview();
      expect(data.activeCarts.length, 3);
      expect(data.alerts.length, 2);
      expect(data.events.length, 3);
      expect(data.activeCarts.map((c) => c.totalPaise), [18000, 9500, 31000]);
      expect(() => data.alerts.clear(), throwsUnsupportedError);
      expect(() => data.events.clear(), throwsUnsupportedError);
      expect(() => data.activeCarts.clear(), throwsUnsupportedError);
      final empty = await MockManagerRepository(
        scenario: MockManagerScenario.empty,
        delay: Duration.zero,
      ).loadOverview();
      expect(empty.activeCarts, isEmpty);
      expect(empty.alerts, isEmpty);
      expect(empty.events, isEmpty);
      await expectLater(
        MockManagerRepository(
          scenario: MockManagerScenario.failure,
          delay: Duration.zero,
        ).loadOverview(),
        throwsStateError,
      );
    },
  );
  test(
    'controller prevents duplicate loads and supports retry and disposal',
    () async {
      final repo = TestManagerRepository();
      final pending = Completer<ManagerOverview>();
      repo.response = () => pending.future;
      final controller = ManagerController(repo);
      final load = controller.load();
      expect(controller.state, ManagerLoadState.loading);
      await controller.load();
      expect(repo.calls, 1);
      pending.completeError(StateError('internal'));
      await load;
      expect(controller.state, ManagerLoadState.error);
      expect(controller.overview, isNull);
      repo.response = () async => emptyOverview();
      await controller.load();
      expect(repo.calls, 2);
      expect(controller.state, ManagerLoadState.ready);
      final last = Completer<ManagerOverview>();
      repo.response = () => last.future;
      final request = controller.load();
      controller.dispose();
      last.complete(emptyOverview());
      await request;
      expect(controller.overview, isNull);
    },
  );
  testWidgets('dashboard displays carts, supplied bills, alerts and activity', (
    tester,
  ) async {
    await showManager(tester, MockManagerRepository(delay: Duration.zero));
    await tester.pumpAndSettle();
    expect(find.text('Manager Dashboard'), findsOneWidget);
    expect(find.text('Active Carts: 3'), findsOneWidget);
    expect(find.text('Alerts: 2'), findsOneWidget);
    expect(find.text('Cart SC-001'), findsWidgets);
    expect(find.text('Items: 5'), findsOneWidget);
    expect(find.text('Bill: ₹180.00'), findsOneWidget);
    await reveal(tester, 'Bill: ₹310.00');
    expect(find.text('Status: Attention'), findsOneWidget);
    await reveal(tester, 'Unexpected cart event reported. Review cart.');
    expect(find.text('Attention required'), findsOneWidget);
    await reveal(tester, 'Product added');
    expect(find.text('Cart item list updated.'), findsOneWidget);
    await reveal(tester, 'Logout');
    await tester.tap(find.text('Logout'));
    await tester.pumpAndSettle();
    expect(find.text('Welcome Back'), findsOneWidget);
  });
  testWidgets('all empty collections have valid explanatory states', (
    tester,
  ) async {
    await showManager(
      tester,
      MockManagerRepository(
        scenario: MockManagerScenario.empty,
        delay: Duration.zero,
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('Active Carts: 0'), findsOneWidget);
    expect(find.text('Alerts: 0'), findsOneWidget);
    expect(find.text('No active carts'), findsOneWidget);
    await reveal(tester, 'No alerts');
    await reveal(tester, 'No recent activity');
    expect(find.text('Retry'), findsNothing);
  });
  testWidgets('loading, safe error and retry use repository state', (
    tester,
  ) async {
    final repo = TestManagerRepository();
    final pending = Completer<ManagerOverview>();
    repo.response = () => pending.future;
    await showManager(tester, repo);
    expect(find.text('Loading manager data...'), findsOneWidget);
    pending.completeError(StateError('private detail'));
    await tester.pumpAndSettle();
    expect(
      find.text('Unable to load manager data. Please try again.'),
      findsOneWidget,
    );
    expect(find.textContaining('private detail'), findsNothing);
    repo.response = () async => emptyOverview();
    await tester.tap(find.text('Retry'));
    await tester.pumpAndSettle();
    expect(repo.calls, 2);
    expect(find.text('No active carts'), findsOneWidget);
  });
  testWidgets('logout during load discards late manager results', (
    tester,
  ) async {
    final pending = Completer<ManagerOverview>();
    final repo = TestManagerRepository()..response = () => pending.future;
    await showManager(tester, repo);
    await tester.tap(find.text('Logout'));
    await tester.pumpAndSettle();
    pending.complete(emptyOverview());
    await tester.pumpAndSettle();
    expect(find.text('Welcome Back'), findsOneWidget);
    expect(find.text('Manager Dashboard'), findsNothing);
    expect(tester.takeException(), isNull);
  });
  testWidgets(
    'small screen and large text remain scrollable without overflow',
    (tester) async {
      tester.view.physicalSize = const Size(320, 480);
      tester.view.devicePixelRatio = 1;
      tester.platformDispatcher.textScaleFactorTestValue = 2;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
      await showManager(tester, MockManagerRepository(delay: Duration.zero));
      await tester.pumpAndSettle();
      await reveal(tester, 'Bill: ₹310.00');
      expect(find.text('Bill: ₹310.00').hitTestable(), findsOneWidget);
      await reveal(tester, 'Unexpected cart event reported. Review cart.');
      await reveal(tester, 'Logout');
      expect(find.text('Logout').hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );
}

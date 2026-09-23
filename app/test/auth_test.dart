import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/features/auth/controllers/auth_controller.dart';
import 'package:smartcart/features/auth/data/auth_repository.dart';
import 'package:smartcart/features/auth/data/in_memory_auth_repository.dart';
import 'package:smartcart/features/auth/models/auth_user.dart';

class ControlledRepository implements AuthRepository {
  final result = Completer<AuthUser>();
  int calls = 0;
  bool failSignOut = false;
  @override
  AuthUser? currentUser;
  @override
  Future<AuthUser> signIn({required String email, required String password}) {
    calls++;
    return result.future;
  }

  @override
  Future<void> signOut() async {
    if (failSignOut) throw StateError('private detail');
    currentUser = null;
  }
}

void main() {
  test('demo repository uses selected role, clears session, and never infers roles from email', () async {
    final repository = InMemoryAuthRepository(delay: Duration.zero);
    expect(repository.currentUser, isNull);
    final customer = await repository.signIn(
      email: 'manager@example.test',
      password: 'demo',
    );
    expect(customer.role, UserRole.customer);
    repository.selectedRole = UserRole.manager;
    final manager = await repository.signIn(
      email: 'customer@example.test',
      password: 'demo',
    );
    expect(manager.role, UserRole.manager);
    expect(repository.currentUser, same(manager));
    await repository.signOut();
    expect(repository.currentUser, isNull);
    expect(repository.selectedRole, UserRole.customer);
    expect(InMemoryAuthRepository().currentUser, isNull);
  });

  test('invalid demo input produces an authentication failure', () async {
    final repository = InMemoryAuthRepository(delay: Duration.zero);
    await expectLater(
      repository.signIn(email: 'invalid', password: 'demo'),
      throwsA(isA<AuthFailure>()),
    );
    await expectLater(
      repository.signIn(email: 'demo@example.test', password: ' '),
      throwsA(isA<AuthFailure>()),
    );
    expect(repository.currentUser, isNull);
  });

  test(
    'controller exposes loading, blocks duplicate submission, and masks errors',
    () async {
      final repository = ControlledRepository();
      final controller = AuthController(repository);
      addTearDown(controller.dispose);
      final pending = controller.signIn(
        email: 'demo@example.test',
        password: 'demo',
      );
      expect(controller.isBusy, isTrue);
      await controller.signIn(email: 'demo@example.test', password: 'demo');
      expect(repository.calls, 1);
      repository.result.completeError(StateError('private detail'));
      await pending;
      expect(controller.isBusy, isFalse);
      expect(controller.user, isNull);
      expect(controller.error, 'Unable to sign in. Please try again.');
    },
  );

  test(
    'controller reads current session and preserves it on sign-out failure',
    () async {
      final repository = ControlledRepository()
        ..currentUser = const AuthUser(role: UserRole.manager)
        ..failSignOut = true;
      final controller = AuthController(repository);
      addTearDown(controller.dispose);
      expect(controller.user!.role, UserRole.manager);
      expect(controller.supportsDevelopmentRoles, isFalse);
      await controller.signOut();
      expect(controller.user!.role, UserRole.manager);
      expect(controller.error, 'Unable to sign out. Please try again.');
      repository.failSignOut = false;
      await controller.signOut();
      expect(controller.user, isNull);
      expect(controller.error, isNull);
    },
  );

  test('controller can finish a pending request after disposal', () async {
    final repository = ControlledRepository();
    final controller = AuthController(repository);
    final pending = controller.signIn(
      email: 'demo@example.test',
      password: 'demo',
    );
    controller.dispose();
    repository.result.complete(const AuthUser(role: UserRole.customer));
    await pending;
  });
}

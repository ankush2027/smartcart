import 'dart:async';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_database/firebase_database.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/core/data/firebase_session_reader.dart';
import 'package:smartcart/core/data/repository_failure.dart';
import 'package:smartcart/features/auth/data/auth_repository.dart';
import 'package:smartcart/features/auth/models/auth_user.dart';
import 'package:smartcart/features/customer/data/firebase_cart_repository.dart';
import 'package:smartcart/features/customer/models/customer_cart.dart';
import 'package:smartcart/features/manager/data/firebase_manager_repository.dart';

import 'rtdb_mapping_test.dart' show sessionFixture;

class LiveAuth extends Fake implements AuthRepository, AuthStateSource {
  LiveAuth(this.currentUser);
  @override
  AuthUser? currentUser;
  final changes = StreamController<AuthUser?>.broadcast(sync: true);
  @override
  Stream<AuthUser?> get authStateChanges => changes.stream;
  void change(AuthUser? user) {
    currentUser = user;
    changes.add(user);
  }
}

class FakeReader implements SessionReader, ManagerSessionReader {
  final paths = <String>[];
  final channels = <StreamController<Object?>>[];
  int active = 0;
  StreamController<Object?> get data => channels.last;
  Stream<Object?> _open(String path) {
    paths.add(path);
    final channel = StreamController<Object?>.broadcast(
      sync: true,
      onListen: () => active++,
      onCancel: () => active--,
    );
    channels.add(channel);
    return channel.stream;
  }

  @override
  Stream<Object?> watchOwnedSessions(String uid) =>
      _open('sessions:customer_uid=$uid');
  @override
  Stream<Object?> watchSessions() => _open('sessions');
  Future<void> close() async {
    for (final channel in channels) {
      await channel.close();
    }
  }
}

class FakeDatabase extends Fake implements FirebaseDatabase {
  final paths = <String?>[];
  final reference = FakeReference();
  @override
  DatabaseReference ref([String? path]) {
    paths.add(path);
    return reference;
  }
}

class FakeReference extends Fake implements DatabaseReference {
  final query = FakeQuery();
  @override
  Query orderByChild(String path) {
    query.order = path;
    return query;
  }

  @override
  Stream<DatabaseEvent> get onValue =>
      throw StateError('Unrestricted read used');
}

class FakeQuery extends Fake implements Query {
  String? order;
  Object? equal;
  final events = StreamController<DatabaseEvent>();
  @override
  Query equalTo(Object? value, {String? key}) {
    equal = value;
    return this;
  }

  @override
  Stream<DatabaseEvent> get onValue => events.stream;
}

class FakeSnapshot extends Fake implements DataSnapshot {
  FakeSnapshot(this.value);
  @override
  final Object? value;
}

class FakeEvent extends Fake implements DatabaseEvent {
  FakeEvent(Object? value) : snapshot = FakeSnapshot(value);
  @override
  final DataSnapshot snapshot;
}

const customer = AuthUser(uid: 'customer-uid', role: UserRole.customer);
const other = AuthUser(uid: 'other-uid', role: UserRole.customer);
const manager = AuthUser(uid: 'manager-uid', role: UserRole.manager);
Future<void> flush() => Future<void>.delayed(Duration.zero);
Matcher failure(RepositoryFailureCode code) =>
    isA<RepositoryFailure>().having((e) => e.code, 'code', code);

void main() {
  late LiveAuth auth;
  late FakeReader reader;
  setUp(() {
    auth = LiveAuth(customer);
    reader = FakeReader();
  });
  tearDown(() async {
    await auth.changes.close();
    await reader.close();
  });
  FirebaseCartRepository cart() =>
      FirebaseCartRepository(auth: auth, reader: reader);

  final scenarios = <String, Map<String, Object?>>{
    'zero owned': {},
    'completed only': {'old': sessionFixture(completed: true)},
    'multiple completed': {
      'old': sessionFixture(completed: true),
      'older': sessionFixture(completed: true),
    },
    'one active': {'actual-session-key': sessionFixture()},
    'active plus history': {
      'newer-completed': sessionFixture(completed: true),
      'actual-session-key': sessionFixture(),
    },
  };
  for (final entry in scenarios.entries) {
    test('discovery: ${entry.key}', () async {
      final repo = cart();
      final result = repo.loadCurrentCart();
      await flush();
      reader.data.add(entry.value);
      final value = await result;
      if (entry.key.contains('active')) {
        expect(value!.displayId, 'actual-session-key');
        expect(value.customerUid, customer.uid);
        expect(value.itemCount, 3);
        expect(value.totalPaise, 3015);
      } else {
        expect(value, isNull);
      }
      expect(reader.paths, ['sessions:customer_uid=customer-uid']);
      await flush();
      expect(reader.active, 0);
      await repo.dispose();
    });
  }
  test(
    'multiple active sessions conflict without choosing or writing',
    () async {
      final repo = cart();
      final result = expectLater(
        repo.loadCurrentCart(),
        throwsA(failure(RepositoryFailureCode.multipleActiveSessions)),
      );
      await flush();
      reader.data.add({'a': sessionFixture(), 'b': sessionFixture()});
      await result;
      await repo.dispose();
    },
  );
  for (final owner in [null, 'other-uid']) {
    test(
      'foreign/missing ownership rejected, including history: $owner',
      () async {
        final repo = cart();
        final result = expectLater(
          repo.loadCurrentCart(),
          throwsA(failure(RepositoryFailureCode.permissionDenied)),
        );
        await flush();
        reader.data.add({
          'valid': sessionFixture(),
          'foreign': sessionFixture(owner: owner, completed: true),
        });
        await result;
        await repo.dispose();
      },
    );
  }
  for (final key in ['', '../sessions', 'a/b', 'a.b', 'a#b', 'a\u0000b']) {
    test('malformed returned session key rejected: ${key.length}', () async {
      final repo = cart();
      final result = expectLater(
        repo.loadCurrentCart(),
        throwsA(failure(RepositoryFailureCode.invalidData)),
      );
      await flush();
      reader.data.add({key: sessionFixture()});
      await result;
      await repo.dispose();
    });
  }
  test('malformed completed time is invalid rather than active', () async {
    final repo = cart();
    final result = expectLater(
      repo.loadCurrentCart(),
      throwsA(failure(RepositoryFailureCode.invalidData)),
    );
    await flush();
    reader.data.add({'s': sessionFixture()..['end_time'] = 'invalid'});
    await result;
    await repo.dispose();
  });
  test(
    'billing updates, completion and later owned session remain live',
    () async {
      final repo = cart();
      final values = <CustomerCart?>[];
      final sub = repo.watchCurrentCart().listen(values.add);
      await flush();
      reader.data.add(null);
      await flush();
      reader.data.add({'s': sessionFixture()});
      await flush();
      reader.data.add({'s': sessionFixture()..['total_amount'] = 40});
      await flush();
      reader.data.add({'s': sessionFixture(completed: true)});
      await flush();
      reader.data.add({
        's': sessionFixture(completed: true),
        'next': sessionFixture(),
      });
      await flush();
      expect(values.map((v) => v?.totalPaise), [null, 3015, 4000, null, 3015]);
      expect(values.last!.displayId, 'next');
      expect(reader.paths.length, 1);
      await sub.cancel();
      await repo.dispose();
    },
  );
  test(
    'sharing replays current state and cancellation releases both listeners',
    () async {
      final repo = cart();
      final a = <CustomerCart?>[];
      final b = <CustomerCart?>[];
      final first = repo.watchCurrentCart().listen(a.add);
      await flush();
      reader.data.add({'s': sessionFixture()});
      await flush();
      final second = repo.watchCurrentCart().listen(b.add);
      await flush();
      expect(a.length, 1);
      expect(b.length, 1);
      expect(reader.paths.length, 1);
      await first.cancel();
      expect(reader.active, 1);
      await second.cancel();
      expect(reader.active, 0);
      expect(auth.changes.hasListener, isFalse);
      final fresh = <CustomerCart?>[];
      final third = repo.watchCurrentCart().listen(fresh.add);
      await flush();
      expect(fresh, isEmpty);
      expect(reader.paths.length, 2);
      await third.cancel();
      await repo.dispose();
    },
  );
  test(
    'UID switch clears state, cancels old query and starts new owned query',
    () async {
      final repo = cart();
      final values = <CustomerCart?>[];
      final sub = repo.watchCurrentCart().listen(values.add);
      await flush();
      reader.data.add({'old': sessionFixture()});
      await flush();
      final old = reader.data;
      auth.change(other);
      old.add({'queued-old': sessionFixture()});
      await flush();
      expect(old.hasListener, isFalse);
      expect(values.last, isNull);
      expect(reader.paths, [
        'sessions:customer_uid=customer-uid',
        'sessions:customer_uid=other-uid',
      ]);
      reader.data.add({'new': sessionFixture(owner: other.uid)});
      await flush();
      expect(values.last!.displayId, 'new');
      expect(values.last!.customerUid, other.uid);
      expect(reader.active, 1);
      await sub.cancel();
      await repo.dispose();
    },
  );
  test(
    'queued data before a UID switch cannot leak into new identity',
    () async {
      final repo = cart();
      final values = <CustomerCart?>[];
      final sub = repo.watchCurrentCart().listen(values.add);
      await flush();
      reader.data.add({'old': sessionFixture()});
      auth.change(other);
      await flush();
      expect(values.whereType<CustomerCart>(), isEmpty);
      reader.data.add({'new': sessionFixture(owner: other.uid)});
      await flush();
      expect(values.whereType<CustomerCart>().single.customerUid, other.uid);
      await sub.cancel();
      await repo.dispose();
    },
  );
  test(
    'logout clears cache, cancels read, and login restarts discovery',
    () async {
      final repo = cart();
      final values = <CustomerCart?>[];
      final sub = repo.watchCurrentCart().listen(values.add);
      await flush();
      reader.data.add({'s': sessionFixture()});
      await flush();
      auth.change(null);
      await flush();
      expect(values.last, isNull);
      expect(reader.active, 0);
      auth.change(other);
      await flush();
      expect(reader.paths.last, 'sessions:customer_uid=other-uid');
      await sub.cancel();
      await repo.dispose();
    },
  );
  test(
    'dispose closes and prevents reconnection with controlled failure',
    () async {
      final repo = cart();
      final result = expectLater(
        repo.loadCurrentCart(),
        throwsA(failure(RepositoryFailureCode.disposed)),
      );
      await flush();
      await repo.dispose();
      await result;
      expect(reader.active, 0);
      expect(auth.changes.hasListener, isFalse);
      await expectLater(
        repo.loadCurrentCart(),
        throwsA(failure(RepositoryFailureCode.disposed)),
      );
    },
  );
  test('transport errors stay safe', () async {
    final repo = cart();
    final result = expectLater(
      repo.loadCurrentCart(),
      throwsA(failure(RepositoryFailureCode.unavailable)),
    );
    await flush();
    reader.data.addError(StateError('private details'));
    await result;
    await repo.dispose();
  });
  test('manager data maps only after trusted app-level manager role', () async {
    auth.change(manager);
    final repo = FirebaseManagerRepository(auth: auth, reader: reader);
    final result = repo.loadOverview();
    reader.data.add({'42': sessionFixture()});
    final overview = await result;
    expect(reader.paths, ['sessions']);
    expect(overview.activeCarts.single.totalPaise, 3015);
    expect(overview.alerts, isEmpty);
    expect(overview.activeCarts.single.updatedAt, isNull);
    await repo.dispose();
  });

  test('customer cannot initiate manager collection read', () async {
    final repo = FirebaseManagerRepository(auth: auth, reader: reader);
    await expectLater(
      repo.loadOverview(),
      throwsA(failure(RepositoryFailureCode.permissionDenied)),
    );
    expect(reader.paths, isEmpty);
    await repo.dispose();
  });

  test('signed-out repository performs no reads', () async {
    auth.change(null);
    final repo = cart();
    await expectLater(
      repo.loadCurrentCart(),
      throwsA(failure(RepositoryFailureCode.unauthenticated)),
    );
    expect(reader.paths, isEmpty);
    await repo.dispose();
  });

  test('manager claim loss cancels collection listener', () async {
    auth.change(manager);
    final repo = FirebaseManagerRepository(auth: auth, reader: reader);
    final expectation = expectLater(
      repo.loadOverview(),
      throwsA(failure(RepositoryFailureCode.identityChanged)),
    );
    auth.change(const AuthUser(uid: 'manager-uid', role: UserRole.customer));
    await expectation;
    expect(reader.active, 0);
    await repo.dispose();
  });

  test(
    'manager rules denial stays a controlled failure, never a fallback read',
    () async {
      auth.change(manager);
      final repo = FirebaseManagerRepository(auth: auth, reader: reader);
      final expectation = expectLater(
        repo.loadOverview(),
        throwsA(failure(RepositoryFailureCode.permissionDenied)),
      );
      reader.data.addError(
        const RepositoryFailure(RepositoryFailureCode.permissionDenied),
      );
      await expectation;
      expect(reader.paths, ['sessions']);
      await repo.dispose();
    },
  );

  test(
    'SDK customer read uses exactly orderByChild(customer_uid).equalTo(uid)',
    () async {
      final db = FakeDatabase();
      final transport = FirebaseSessionReader(database: db);
      final result = transport.watchOwnedSessions(customer.uid).first;
      expect(db.paths, ['sessions']);
      expect(db.reference.query.order, 'customer_uid');
      expect(db.reference.query.equal, customer.uid);
      final fixture = {'s': sessionFixture()};
      db.reference.query.events.add(FakeEvent(fixture));
      expect(await result, same(fixture));
      expect(db.reference.query.events.hasListener, isFalse);
      await db.reference.query.events.close();
    },
  );
  for (final code in ['permission-denied', 'network-error']) {
    test('SDK safe error mapping: $code', () async {
      final db = FakeDatabase();
      final transport = FirebaseSessionReader(database: db);
      final result = expectLater(
        transport.watchOwnedSessions(customer.uid).first,
        throwsA(
          failure(
            code == 'permission-denied'
                ? RepositoryFailureCode.permissionDenied
                : RepositoryFailureCode.unavailable,
          ),
        ),
      );
      db.reference.query.events.addError(
        FirebaseException(
          plugin: 'firebase_database',
          code: code,
          message: 'private',
        ),
      );
      await result;
      await db.reference.query.events.close();
    });
  }
}

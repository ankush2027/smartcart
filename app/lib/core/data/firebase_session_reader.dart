import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_database/firebase_database.dart';

import 'repository_failure.dart';

/// Read-only transport boundary. No DatabaseReference escapes this layer.
abstract interface class SessionReader {
  Stream<Object?> watchOwnedSessions(String authenticatedUid);
}

abstract interface class ManagerSessionReader {
  Stream<Object?> watchSessions();
}

class FirebaseSessionReader implements SessionReader, ManagerSessionReader {
  FirebaseSessionReader({FirebaseDatabase? database})
    : _database = database ?? FirebaseDatabase.instance;

  final FirebaseDatabase _database;

  @override
  Stream<Object?> watchOwnedSessions(String authenticatedUid) {
    if (authenticatedUid.isEmpty) {
      throw const RepositoryFailure(RepositoryFailureCode.unauthenticated);
    }
    return _values(
      _database
          .ref('sessions')
          .orderByChild('customer_uid')
          .equalTo(authenticatedUid),
    );
  }

  /// The deployed contract permits this collection read for role=manager.
  @override
  Stream<Object?> watchSessions() => _values(_database.ref('sessions'));

  Stream<Object?> _values(Query ref) => ref.onValue
      .map<Object?>((event) => event.snapshot.value)
      .handleError((Object error) {
        throw RepositoryFailure(
          error is FirebaseException && error.code == 'permission-denied'
              ? RepositoryFailureCode.permissionDenied
              : RepositoryFailureCode.unavailable,
        );
      });
}

void validateSessionKey(String key) {
  if (key.isEmpty || RegExp(r'[.#$\[\]/\x00-\x1f\x7f]').hasMatch(key)) {
    throw const RepositoryFailure(RepositoryFailureCode.invalidData);
  }
}

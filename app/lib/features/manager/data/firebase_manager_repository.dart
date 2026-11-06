import '../../../core/data/repository_failure.dart';
import '../../../core/data/firebase_session_reader.dart';
import '../../../core/data/scoped_realtime_read.dart';
import '../../../core/data/session_mapper.dart';
import '../../auth/data/auth_repository.dart';
import '../../auth/models/auth_user.dart';
import '../models/manager_overview.dart';
import 'manager_repository.dart';

/// Collection reads authorized by the deployed role=manager contract.
/// Firebase rules remain the actual data-access boundary.
class FirebaseManagerRepository implements RealtimeManagerRepository {
  FirebaseManagerRepository({required this.auth, this.reader});

  final AuthRepository auth;
  final ManagerSessionReader? reader;
  final _reads = <ScopedRealtimeRead<ManagerOverview>>{};
  bool _disposed = false;
  @override
  bool get isDemo => false;
  @override
  Stream<ManagerOverview> watchOverview() => Stream<ManagerOverview>.multi((
    listener,
  ) {
    if (_disposed) {
      listener.addError(
        const RepositoryFailure(RepositoryFailureCode.disposed),
      );
      listener.close();
      return;
    }
    // Bind on subscription, not app startup. Retry and new login get fresh
    // reads, while previously invalidated reads never recover their authority.
    final read = ScopedRealtimeRead<ManagerOverview>(
      auth: auth,
      role: UserRole.manager,
      source: () => (reader ?? FirebaseSessionReader()).watchSessions(),
      map: (value, _) => parseManagerOverview(value),
    );
    _reads.add(read);
    final subscription = read.stream.listen(
      listener.addSync,
      onError: listener.addErrorSync,
      onDone: listener.closeSync,
    );
    listener.onPause = subscription.pause;
    listener.onResume = subscription.resume;
    listener.onCancel = () async {
      _reads.remove(read);
      await subscription.cancel();
      await read.dispose();
    };
  });
  @override
  Future<ManagerOverview> loadOverview() => watchOverview().first;
  @override
  Future<void> dispose() async {
    _disposed = true;
    await Future.wait(_reads.toList().map((read) => read.dispose()));
    _reads.clear();
  }
}

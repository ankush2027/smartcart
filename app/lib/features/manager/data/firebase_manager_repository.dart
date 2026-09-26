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
  FirebaseManagerRepository({
    required AuthRepository auth,
    ManagerSessionReader? reader,
  }) {
    _read = ScopedRealtimeRead<ManagerOverview>(
      auth: auth,
      role: UserRole.manager,
      source: () => (reader ?? FirebaseSessionReader()).watchSessions(),
      map: (value, _) => parseManagerOverview(value),
    );
  }

  late final ScopedRealtimeRead<ManagerOverview> _read;
  @override
  bool get isDemo => false;
  @override
  Stream<ManagerOverview> watchOverview() => _read.stream;
  @override
  Future<ManagerOverview> loadOverview() => _read.first();
  @override
  Future<void> dispose() => _read.dispose();
}

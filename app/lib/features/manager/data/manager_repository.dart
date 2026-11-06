import '../models/manager_overview.dart';

abstract interface class ManagerRepository {
  bool get isDemo;

  /// Supplies monitoring data, including already-determined alerts and totals.
  Future<ManagerOverview> loadOverview();
}

/// Live production monitoring; snapshot repositories remain injectable for tests.
abstract interface class RealtimeManagerRepository
    implements ManagerRepository {
  Stream<ManagerOverview> watchOverview();
  Future<void> dispose();
}

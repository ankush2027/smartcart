import '../models/manager_overview.dart';

abstract interface class ManagerRepository {
  bool get isDemo;

  /// Supplies monitoring data, including already-determined alerts and totals.
  Future<ManagerOverview> loadOverview();
}

/// Optional capability for Stage 9; production still uses mock snapshots.
abstract interface class RealtimeManagerRepository
    implements ManagerRepository {
  Stream<ManagerOverview> watchOverview();
  Future<void> dispose();
}

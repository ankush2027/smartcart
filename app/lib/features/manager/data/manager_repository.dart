import '../models/manager_overview.dart';

abstract interface class ManagerRepository {
  bool get isDemo;

  /// Supplies monitoring data, including already-determined alerts and totals.
  Future<ManagerOverview> loadOverview();
}

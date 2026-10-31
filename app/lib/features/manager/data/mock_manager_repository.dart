import '../models/manager_overview.dart';
import 'manager_repository.dart';

enum MockManagerScenario { populated, empty, failure }

/// Development fixtures only. No detection, classification, or billing rules.
class MockManagerRepository implements ManagerRepository {
  MockManagerRepository({
    this.scenario = MockManagerScenario.populated,
    this.delay = const Duration(milliseconds: 400),
    DateTime? snapshotTime,
  }) : _snapshotTime = snapshotTime ?? DateTime.now();
  final MockManagerScenario scenario;
  final Duration delay;
  final DateTime _snapshotTime;
  @override
  bool get isDemo => true;

  @override
  Future<ManagerOverview> loadOverview() async {
    await Future<void>.delayed(delay);
    if (scenario == MockManagerScenario.failure) {
      throw StateError('Simulated manager data failure');
    }
    if (scenario == MockManagerScenario.empty) {
      return ManagerOverview(activeCarts: [], alerts: [], events: []);
    }
    final recent = _snapshotTime.subtract(const Duration(minutes: 2));
    return ManagerOverview(
      activeCarts: [
        ManagedCart(
          sessionId: 'SC-001',
          status: 'Active',
          itemCount: 5,
          totalPaise: 18000,
          updatedAt: _snapshotTime,
        ),
        ManagedCart(
          sessionId: 'SC-002',
          status: 'Active',
          itemCount: 3,
          totalPaise: 9500,
          updatedAt: recent,
        ),
        ManagedCart(
          sessionId: 'SC-003',
          status: 'Attention',
          itemCount: 8,
          totalPaise: 31000,
          updatedAt: recent,
        ),
      ],
      alerts: [
        ManagerAlert(
          id: 'demo-alert-1',
          severity: 'Attention required',
          status: 'Open',
          message: 'Unexpected cart event reported. Review cart.',
          timestamp: recent,
          sessionId: 'SC-003',
        ),
        ManagerAlert(
          id: 'demo-alert-2',
          severity: 'Notice',
          status: 'Open',
          message: 'Cart status update delayed. Review connection status.',
          timestamp: recent,
          sessionId: 'SC-002',
        ),
      ],
      events: [
        ManagerEvent(
          id: 'demo-event-1',
          label: 'Product added',
          message: 'Cart item list updated.',
          timestamp: _snapshotTime,
          sessionId: 'SC-001',
        ),
        ManagerEvent(
          id: 'demo-event-2',
          label: 'Attention event',
          message: 'A review request was received.',
          timestamp: recent,
          sessionId: 'SC-003',
        ),
        ManagerEvent(
          id: 'demo-event-3',
          label: 'Session started',
          message: 'Shopping session opened.',
          timestamp: _snapshotTime.subtract(const Duration(minutes: 5)),
          sessionId: 'SC-002',
        ),
      ],
    );
  }
}

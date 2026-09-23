import 'package:flutter/material.dart';

import '../../auth/controllers/auth_controller.dart';
import '../../customer/models/customer_cart.dart' show formatRupees;
import '../controllers/manager_controller.dart';
import '../data/manager_repository.dart';
import '../widgets/monitoring_card.dart';

class ManagerScreen extends StatefulWidget {
  const ManagerScreen({
    super.key,
    required this.auth,
    required this.repository,
  });
  final AuthController auth;
  final ManagerRepository repository;
  @override
  State<ManagerScreen> createState() => _ManagerScreenState();
}

class _ManagerScreenState extends State<ManagerScreen> {
  late final ManagerController _manager;
  @override
  void initState() {
    super.initState();
    _manager = ManagerController(widget.repository)..load();
  }

  @override
  void dispose() {
    _manager.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: Listenable.merge([_manager, widget.auth]),
    builder: (context, child) => Scaffold(
      appBar: AppBar(title: const Text('SmartCart')),
      body: SafeArea(
        child: Align(
          alignment: Alignment.topCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 720),
            child: ListView(
              padding: const EdgeInsets.all(24),
              children: [
                Text(
                  'Manager Dashboard',
                  style: Theme.of(context).textTheme.headlineMedium,
                ),
                const SizedBox(height: 12),
                if (_manager.isDemo) ...[
                  const Text(
                    'Development demo • Sample monitoring data. No live SmartCart connection.',
                  ),
                  const SizedBox(height: 24),
                ],
                ..._content(context),
                const SizedBox(height: 24),
                if (widget.auth.error != null)
                  Semantics(
                    liveRegion: true,
                    child: Text(
                      widget.auth.error!,
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.error,
                      ),
                    ),
                  ),
                OutlinedButton.icon(
                  onPressed: widget.auth.isBusy ? null : widget.auth.signOut,
                  icon: const Icon(Icons.logout),
                  label: Text(widget.auth.isBusy ? 'Signing out…' : 'Logout'),
                ),
              ],
            ),
          ),
        ),
      ),
    ),
  );

  List<Widget> _heading(BuildContext context, String title) => [
    const SizedBox(height: 24),
    Text(title, style: Theme.of(context).textTheme.titleLarge),
    const SizedBox(height: 12),
  ];

  List<Widget> _content(BuildContext context) {
    switch (_manager.state) {
      case ManagerLoadState.loading:
        return [
          const Center(child: CircularProgressIndicator()),
          const SizedBox(height: 16),
          const Text('Loading manager data...'),
        ];
      case ManagerLoadState.error:
        return [
          Semantics(
            liveRegion: true,
            child: const Text('Unable to load manager data. Please try again.'),
          ),
          const SizedBox(height: 16),
          FilledButton(onPressed: _manager.load, child: const Text('Retry')),
        ];
      case ManagerLoadState.ready:
        final overview = _manager.overview!;
        return [
          Card.filled(
            margin: EdgeInsets.zero,
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Wrap(
                spacing: 32,
                runSpacing: 16,
                children: [
                  Text(
                    'Active Carts: ${overview.activeCarts.length}',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                  Text(
                    'Alerts: ${overview.alerts.length}',
                    style: Theme.of(context).textTheme.titleMedium,
                  ),
                ],
              ),
            ),
          ),
          ..._heading(context, 'Active Carts'),
          if (overview.activeCarts.isEmpty) const Text('No active carts'),
          ...overview.activeCarts.map(
            (cart) => MonitoringCard(
              title: 'Cart ${cart.sessionId}',
              lines: [
                'Status: ${cart.status}',
                'Items: ${cart.itemCount}',
                'Bill: ${formatRupees(cart.totalPaise)}',
                'Updated: ${formatManagerTime(context, cart.updatedAt)}',
              ],
            ),
          ),
          ..._heading(context, 'Alerts'),
          const Text('Repository-provided notices for review.'),
          const SizedBox(height: 12),
          if (overview.alerts.isEmpty) const Text('No alerts'),
          ...overview.alerts.map(
            (alert) => MonitoringCard(
              icon: Icons.info_outline,
              title: alert.severity,
              lines: [
                'Cart ${alert.sessionId} · ${alert.status}',
                alert.message,
                formatManagerTime(context, alert.timestamp),
              ],
            ),
          ),
          ..._heading(context, 'Recent Activity'),
          if (overview.events.isEmpty) const Text('No recent activity'),
          ...overview.events.map(
            (event) => MonitoringCard(
              icon: Icons.history,
              title: event.label,
              lines: [
                'Cart ${event.sessionId}',
                event.message,
                formatManagerTime(context, event.timestamp),
              ],
            ),
          ),
        ];
    }
  }
}

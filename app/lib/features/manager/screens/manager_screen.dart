import 'package:flutter/material.dart';

import '../../auth/controllers/auth_controller.dart';
import '../../customer/models/customer_cart.dart' show formatRupees;
import '../controllers/manager_controller.dart';
import '../data/manager_repository.dart';
import '../models/manager_overview.dart';
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
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    return ListenableBuilder(
      listenable: Listenable.merge([_manager, widget.auth]),
      builder: (context, child) => Scaffold(
        appBar: AppBar(
          title: Row(
            children: [
              Container(
                padding: const EdgeInsets.all(6),
                decoration: BoxDecoration(
                  color: colorScheme.primaryContainer,
                  borderRadius: BorderRadius.circular(8),
                ),
                child: Icon(
                  Icons.storefront_outlined,
                  size: 18,
                  color: colorScheme.primary,
                ),
              ),
              const SizedBox(width: 10),
              const Text('SmartCart'),
            ],
          ),
          actions: [
            Container(
              margin: const EdgeInsets.only(right: 16),
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
              decoration: BoxDecoration(
                color: colorScheme.secondaryContainer.withValues(alpha: 0.5),
                borderRadius: BorderRadius.circular(20),
                border: Border.all(
                  color: colorScheme.secondary.withValues(alpha: 0.2),
                ),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(
                    Icons.admin_panel_settings_outlined,
                    size: 14,
                    color: colorScheme.secondary,
                  ),
                  const SizedBox(width: 5),
                  Text(
                    'Manager',
                    style: TextStyle(
                      fontSize: 12,
                      fontWeight: FontWeight.w700,
                      color: colorScheme.secondary,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
        body: SafeArea(
          child: Align(
            alignment: Alignment.topCenter,
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 720),
              child: ListView(
                padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 24),
                children: [
                  Text(
                    'Manager Dashboard',
                    style: theme.textTheme.headlineMedium?.copyWith(
                      fontWeight: FontWeight.w800,
                      letterSpacing: -0.5,
                    ),
                  ),
                  const SizedBox(height: 12),
                  if (_manager.isDemo) ...[
                    Container(
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: const Color(0xFFFFF8E1),
                        borderRadius: BorderRadius.circular(12),
                        border: Border.all(color: const Color(0xFFFFE082)),
                      ),
                      child: Row(
                        children: [
                          const Icon(
                            Icons.info_outline_rounded,
                            size: 18,
                            color: Color(0xFFF57F17),
                          ),
                          const SizedBox(width: 10),
                          Expanded(
                            child: Text(
                              'Development demo • Sample monitoring data. No live SmartCart connection.',
                              style: theme.textTheme.bodySmall?.copyWith(
                                color: const Color(0xFF5D4037),
                                fontWeight: FontWeight.w500,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(height: 20),
                  ],
                  ..._content(context),
                  const SizedBox(height: 28),
                  if (widget.auth.error != null) ...[
                    Container(
                      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
                      margin: const EdgeInsets.only(bottom: 16),
                      decoration: BoxDecoration(
                        color: colorScheme.error.withValues(alpha: 0.08),
                        borderRadius: BorderRadius.circular(10),
                        border: Border.all(
                          color: colorScheme.error.withValues(alpha: 0.2),
                        ),
                      ),
                      child: Row(
                        children: [
                          Icon(
                            Icons.error_outline_rounded,
                            size: 18,
                            color: colorScheme.error,
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: Semantics(
                              liveRegion: true,
                              child: Text(
                                widget.auth.error!,
                                style: TextStyle(
                                  color: colorScheme.error,
                                  fontWeight: FontWeight.w500,
                                ),
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ],
                  OutlinedButton.icon(
                    onPressed: widget.auth.isBusy ? null : widget.auth.signOut,
                    style: OutlinedButton.styleFrom(
                      padding: const EdgeInsets.symmetric(vertical: 14),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(12),
                      ),
                    ),
                    icon: const Icon(Icons.logout, size: 18),
                    label: Text(widget.auth.isBusy ? 'Signing out…' : 'Logout'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  List<Widget> _heading(BuildContext context, String title) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    IconData sectionIcon;
    switch (title) {
      case 'Active Carts':
        sectionIcon = Icons.shopping_cart_checkout_rounded;
        break;
      case 'Completed Sessions':
        sectionIcon = Icons.task_alt_rounded;
        break;
      case 'Alerts':
        sectionIcon = Icons.warning_amber_rounded;
        break;
      case 'Recent Activity':
        sectionIcon = Icons.history_rounded;
        break;
      default:
        sectionIcon = Icons.list_alt_rounded;
    }

    return [
      const SizedBox(height: 28),
      Row(
        children: [
          Container(
            padding: const EdgeInsets.all(6),
            decoration: BoxDecoration(
              color: colorScheme.primaryContainer.withValues(alpha: 0.5),
              borderRadius: BorderRadius.circular(8),
            ),
            child: Icon(sectionIcon, size: 18, color: colorScheme.primary),
          ),
          const SizedBox(width: 10),
          Text(
            title,
            style: theme.textTheme.titleLarge?.copyWith(
              fontWeight: FontWeight.w800,
              letterSpacing: -0.3,
            ),
          ),
        ],
      ),
      const SizedBox(height: 14),
    ];
  }

  List<Widget> _content(BuildContext context) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    switch (_manager.state) {
      case ManagerLoadState.loading:
        return [
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 40),
            child: Column(
              children: [
                Container(
                  padding: const EdgeInsets.all(16),
                  decoration: BoxDecoration(
                    color: colorScheme.primaryContainer.withValues(alpha: 0.5),
                    shape: BoxShape.circle,
                  ),
                  child: const CircularProgressIndicator(strokeWidth: 3),
                ),
                const SizedBox(height: 20),
                const Text(
                  'Loading manager data...',
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 15, fontWeight: FontWeight.w500),
                ),
              ],
            ),
          ),
        ];
      case ManagerLoadState.error:
        return [
          Container(
            padding: const EdgeInsets.all(24),
            decoration: BoxDecoration(
              color: colorScheme.surface,
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: colorScheme.error.withValues(alpha: 0.3)),
            ),
            child: Column(
              children: [
                Icon(Icons.cloud_off_rounded, size: 40, color: colorScheme.error),
                const SizedBox(height: 12),
                Semantics(
                  liveRegion: true,
                  child: Text(
                    _manager.error ??
                        'Unable to load manager data. Please try again.',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      color: colorScheme.error,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ),
                const SizedBox(height: 18),
                FilledButton(
                  onPressed: _manager.load,
                  child: const Text('Retry'),
                ),
              ],
            ),
          ),
        ];
      case ManagerLoadState.ready:
        final overview = _manager.overview!;
        return [
          Card(
            elevation: 0,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(20),
              side: BorderSide(
                color: colorScheme.outline.withValues(alpha: 0.8),
                width: 1.2,
              ),
            ),
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Wrap(
                spacing: 24,
                runSpacing: 16,
                children: [
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 12,
                    ),
                    decoration: BoxDecoration(
                      color: colorScheme.primaryContainer.withValues(alpha: 0.4),
                      borderRadius: BorderRadius.circular(14),
                      border: Border.all(
                        color: colorScheme.primary.withValues(alpha: 0.2),
                      ),
                    ),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(
                          Icons.shopping_cart_checkout_rounded,
                          size: 20,
                          color: colorScheme.primary,
                        ),
                        const SizedBox(width: 10),
                        Text(
                          'Active Carts: ${overview.activeCarts.length}',
                          style: theme.textTheme.titleMedium?.copyWith(
                            fontWeight: FontWeight.w700,
                            color: colorScheme.primary,
                          ),
                        ),
                      ],
                    ),
                  ),
                  Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 16,
                      vertical: 12,
                    ),
                    decoration: BoxDecoration(
                      color: _manager.isDemo
                          ? const Color(0xFFFFF3E0)
                          : const Color(0xFFE8F0FE),
                      borderRadius: BorderRadius.circular(14),
                      border: Border.all(
                        color: _manager.isDemo
                            ? const Color(0xFFFFCC80)
                            : const Color(0xFFBBDEFB),
                      ),
                    ),
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        Icon(
                          _manager.isDemo
                              ? Icons.warning_amber_rounded
                              : Icons.task_alt_rounded,
                          size: 20,
                          color: _manager.isDemo
                              ? const Color(0xFFE65100)
                              : const Color(0xFF1967D2),
                        ),
                        const SizedBox(width: 10),
                        Text(
                          _manager.isDemo
                              ? 'Alerts: ${overview.alerts.length}'
                              : 'Completed Sessions: ${overview.completedCarts.length}',
                          style: theme.textTheme.titleMedium?.copyWith(
                            fontWeight: FontWeight.w700,
                            color: _manager.isDemo
                                ? const Color(0xFFE65100)
                                : const Color(0xFF1967D2),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ),
            ),
          ),
          ..._heading(context, 'Active Carts'),
          if (overview.activeCarts.isEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 4),
              child: const Text('No active carts'),
            ),
          ...overview.activeCarts.map((cart) => _sessionCard(context, cart)),
          if (!_manager.isDemo) ...[
            ..._heading(context, 'Completed Sessions'),
            if (overview.completedCarts.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 4),
                child: const Text('No completed sessions'),
              ),
            ...overview.completedCarts.map(
              (cart) => _sessionCard(context, cart),
            ),
          ],
          if (_manager.isDemo) ...[
            ..._heading(context, 'Alerts'),
            Text(
              'Repository-provided notices for review.',
              style: theme.textTheme.bodyMedium?.copyWith(
                color: colorScheme.onSurfaceVariant,
              ),
            ),
            const SizedBox(height: 12),
            if (overview.alerts.isEmpty)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 4),
                child: const Text('No alerts'),
              ),
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
          ],
          ..._heading(context, 'Recent Activity'),
          if (overview.events.isEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8, horizontal: 4),
              child: const Text('No recent activity'),
            ),
          ...overview.events.map(
            (event) => MonitoringCard(
              icon: Icons.history,
              title: event.label,
              lines: [
                _manager.isDemo
                    ? 'Cart ${event.sessionId}'
                    : 'Session ${event.sessionId}',
                event.message,
                if (!_manager.isDemo && event.eventType != null)
                  'Event: ${event.eventType}',
                formatManagerTime(context, event.timestamp),
              ],
            ),
          ),
        ];
    }
  }

  Widget _sessionCard(BuildContext context, ManagedCart cart) => MonitoringCard(
    title: _manager.isDemo
        ? 'Cart ${cart.sessionId}'
        : 'Session ${cart.sessionId}',
    lines: [
      if (!_manager.isDemo) ...[
        'Cart ID: ${cart.cartId ?? "Not assigned"}',
        'Customer UID: ${cart.customerUid ?? "Not assigned"}',
        if (cart.startTime != null)
          'Started: ${formatManagerTime(context, cart.startTime!)}',
        if (cart.endTime != null)
          'Completed: ${formatManagerTime(context, cart.endTime!)}',
      ],
      'Status: ${cart.status}',
      'Items: ${cart.itemCount}',
      'Bill: ${formatRupees(cart.totalPaise)}',
      if (cart.updatedAt != null)
        'Updated: ${formatManagerTime(context, cart.updatedAt!)}',
      if (!_manager.isDemo) ...[
        if (cart.items.isEmpty) 'No bill items',
        for (final item in cart.items)
          '${item.product.name} · Quantity: ${item.quantity} · Unit: ${formatRupees(item.unitPricePaise)} · Line: ${formatRupees(item.lineTotalPaise)}',
      ],
    ],
  );
}

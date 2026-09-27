import 'package:flutter/material.dart';

/// Shared presentation for repository-provided cart, alert, and event entries.
class MonitoringCard extends StatelessWidget {
  const MonitoringCard({
    super.key,
    required this.title,
    required this.lines,
    this.icon = Icons.shopping_cart_outlined,
  });
  final String title;
  final List<String> lines;
  final IconData icon;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final colorScheme = theme.colorScheme;

    // Detect card intent based on icon
    final isAlert = icon == Icons.info_outline || icon == Icons.warning_amber_rounded;
    final isHistory = icon == Icons.history;

    final Color iconBg = isAlert
        ? const Color(0xFFFFF3E0)
        : isHistory
            ? const Color(0xFFE8F0FE)
            : colorScheme.primaryContainer.withValues(alpha: 0.6);

    final Color iconColor = isAlert
        ? const Color(0xFFE65100)
        : isHistory
            ? const Color(0xFF1967D2)
            : colorScheme.primary;

    return Card(
      margin: const EdgeInsets.only(bottom: 12),
      elevation: 0,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: BorderSide(
          color: isAlert
              ? const Color(0xFFFFCC80)
              : colorScheme.outline.withValues(alpha: 0.8),
          width: 1.2,
        ),
      ),
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Container(
                  padding: const EdgeInsets.all(8),
                  decoration: BoxDecoration(
                    color: iconBg,
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Icon(icon, size: 20, color: iconColor),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(
                    title,
                    style: theme.textTheme.titleMedium?.copyWith(
                      fontWeight: FontWeight.w700,
                      color: colorScheme.onSurface,
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 12),
            for (final line in lines) ...[
              const SizedBox(height: 5),
              Text(
                line,
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: line.startsWith('Bill: ')
                      ? colorScheme.primary
                      : line.startsWith('Status: Attention')
                          ? const Color(0xFFC62828)
                          : colorScheme.onSurfaceVariant,
                  fontWeight: (line.startsWith('Bill: ') ||
                          line.startsWith('Status: Attention'))
                      ? FontWeight.w700
                      : FontWeight.w400,
                  height: 1.35,
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

String formatManagerTime(BuildContext context, DateTime timestamp) {
  final local = timestamp.toLocal();
  final labels = MaterialLocalizations.of(context);
  return '${labels.formatCompactDate(local)} · ${labels.formatTimeOfDay(TimeOfDay.fromDateTime(local), alwaysUse24HourFormat: MediaQuery.alwaysUse24HourFormatOf(context))}';
}

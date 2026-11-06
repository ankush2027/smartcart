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
  Widget build(BuildContext context) => Card(
    margin: const EdgeInsets.only(bottom: 12),
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Align(
            alignment: Alignment.centerLeft,
            child: Icon(icon, color: Theme.of(context).colorScheme.primary),
          ),
          const SizedBox(height: 8),
          Text(title, style: Theme.of(context).textTheme.titleMedium),
          for (final line in lines) ...[const SizedBox(height: 6), Text(line)],
        ],
      ),
    ),
  );
}

String formatManagerTime(BuildContext context, DateTime timestamp) {
  final local = timestamp.toLocal();
  final labels = MaterialLocalizations.of(context);
  return '${labels.formatCompactDate(local)} · ${labels.formatTimeOfDay(TimeOfDay.fromDateTime(local), alwaysUse24HourFormat: MediaQuery.alwaysUse24HourFormatOf(context))}';
}

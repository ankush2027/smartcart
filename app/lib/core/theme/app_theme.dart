import 'package:flutter/material.dart';

abstract final class AppTheme {
  static final ThemeData light = ThemeData(
    useMaterial3: true,
    colorScheme: ColorScheme.fromSeed(
      seedColor: const Color(0xFF176B5B),
      brightness: Brightness.light,
    ),
    scaffoldBackgroundColor: const Color(0xFFF6F8F7),
  );
}

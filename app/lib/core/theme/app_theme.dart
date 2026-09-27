import 'package:flutter/material.dart';

abstract final class AppTheme {
  static const Color primaryGreen = Color(0xFF0C6B58);
  static const Color primaryDark = Color(0xFF07483B);
  static const Color primaryLight = Color(0xFFE8F5F1);
  static const Color accentAmber = Color(0xFFE67E22);
  static const Color backgroundLight = Color(0xFFF5F7F6);
  static const Color cardSurface = Colors.white;
  static const Color borderLight = Color(0xFFE2E8E5);
  static const Color textMain = Color(0xFF1A221F);
  static const Color textMuted = Color(0xFF5F6E67);

  static final ThemeData light = ThemeData(
    useMaterial3: true,
    colorScheme: ColorScheme.fromSeed(
      seedColor: primaryGreen,
      primary: primaryGreen,
      onPrimary: Colors.white,
      primaryContainer: primaryLight,
      onPrimaryContainer: primaryDark,
      secondary: const Color(0xFF2A7C6E),
      onSecondary: Colors.white,
      surface: cardSurface,
      onSurface: textMain,
      onSurfaceVariant: textMuted,
      outline: borderLight,
      outlineVariant: const Color(0xFFEDF2EF),
      error: const Color(0xFFBA1A1A),
      brightness: Brightness.light,
    ),
    scaffoldBackgroundColor: backgroundLight,
    appBarTheme: const AppBarTheme(
      backgroundColor: cardSurface,
      foregroundColor: textMain,
      elevation: 0,
      scrolledUnderElevation: 1.5,
      surfaceTintColor: Colors.transparent,
      centerTitle: false,
      titleTextStyle: TextStyle(
        fontSize: 20,
        fontWeight: FontWeight.w700,
        color: textMain,
        letterSpacing: -0.3,
      ),
    ),
    cardTheme: CardThemeData(
      elevation: 0,
      color: cardSurface,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: const BorderSide(color: borderLight, width: 1),
      ),
      margin: EdgeInsets.zero,
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: cardSurface,
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
      labelStyle: const TextStyle(color: textMuted, fontSize: 15),
      hintStyle: const TextStyle(color: Color(0xFF9AA8A1), fontSize: 15),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: borderLight, width: 1.2),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: borderLight, width: 1.2),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: primaryGreen, width: 2),
      ),
      errorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: Color(0xFFBA1A1A), width: 1.2),
      ),
      focusedErrorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(12),
        borderSide: const BorderSide(color: Color(0xFFBA1A1A), width: 2),
      ),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: primaryGreen,
        foregroundColor: Colors.white,
        elevation: 0,
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 14),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        textStyle: const TextStyle(
          fontSize: 16,
          fontWeight: FontWeight.w600,
          letterSpacing: 0.1,
        ),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: primaryGreen,
        side: const BorderSide(color: borderLight, width: 1.2),
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 14),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(12)),
        textStyle: const TextStyle(
          fontSize: 15,
          fontWeight: FontWeight.w600,
        ),
      ),
    ),
    dividerTheme: const DividerThemeData(
      color: borderLight,
      thickness: 1,
      space: 24,
    ),
  );
}

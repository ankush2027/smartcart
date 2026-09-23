import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/app.dart';
import 'package:smartcart/features/launch/launch_screen.dart';

void main() {
  testWidgets('launch route displays the SmartCart foundation', (tester) async {
    await tester.pumpWidget(const SmartCartApp());
    await tester.pumpAndSettle();

    expect(find.byType(LaunchScreen), findsOneWidget);
    expect(find.text('SmartCart'), findsOneWidget);
    expect(find.text('Smart Shopping Cart'), findsOneWidget);
    expect(find.text('Automatic Billing System'), findsOneWidget);
    expect(find.text('Mobile Application'), findsOneWidget);
    expect(find.text('System Ready'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('launch content supports small screens and large text', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 480);
    tester.view.devicePixelRatio = 1;
    tester.platformDispatcher.textScaleFactorTestValue = 2;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);

    await tester.pumpWidget(const SmartCartApp());
    await tester.pumpAndSettle();
    await tester.ensureVisible(find.text('System Ready'));

    expect(find.text('System Ready').hitTestable(), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}

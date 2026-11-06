import 'dart:async';

import 'package:flutter/material.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../services/cart_qr.dart';

typedef CartCameraBuilder = Widget Function(ValueChanged<String?> onCode);

class CartQrScanner extends StatefulWidget {
  const CartQrScanner({super.key, required this.onCart, required this.onManual,
    this.cameraBuilder});
  final ValueChanged<String> onCart;
  final VoidCallback onManual;
  final CartCameraBuilder? cameraBuilder;

  @override
  State<CartQrScanner> createState() => _CartQrScannerState();
}

class _CartQrScannerState extends State<CartQrScanner> {
  final _gate = CartScanGate();
  String? _error;
  int _cameraGeneration = 0;

  void _accept(String? payload) {
    if (!mounted) return;
    try {
      final cart = _gate.accept(payload);
      if (cart != null) widget.onCart(cart);
    } on FormatException {
      setState(() => _error = 'This is not a SmartCart cart label. Scan CART-001 or enter its ID.');
    }
  }

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      const Text('Scan Cart QR Code'),
      const Text('Point the camera at the cart label. Download links are not cart IDs.'),
      const SizedBox(height: 12),
      SizedBox(height: 300, child: KeyedSubtree(
        key: ValueKey(_cameraGeneration),
        child: widget.cameraBuilder?.call(_accept) ?? _CartCamera(onCode: _accept),
      )),
      if (_error != null) Semantics(liveRegion: true, child: Text(_error!)),
      TextButton(onPressed: () => setState(() {
        _error = null;
        _cameraGeneration++;
      }), child: const Text('Retry camera / scan again')),
      OutlinedButton(onPressed: widget.onManual, child: const Text('Enter Cart ID instead')),
    ],
  );
}

class _CartCamera extends StatefulWidget {
  const _CartCamera({required this.onCode});
  final ValueChanged<String?> onCode;
  @override
  State<_CartCamera> createState() => _CartCameraState();
}

class _CartCameraState extends State<_CartCamera> with WidgetsBindingObserver {
  final _controller = MobileScannerController(autoStart: false,
    formats: const [BarcodeFormat.qrCode], detectionSpeed: DetectionSpeed.noDuplicates);
  Future<void> _operations = Future<void>.value();
  bool _accepted = false;
  bool _failed = false;

  bool get _foreground => WidgetsBinding.instance.lifecycleState == null ||
      WidgetsBinding.instance.lifecycleState == AppLifecycleState.resumed;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    WidgetsBinding.instance.addPostFrameCallback((_) => _camera(true));
  }

  void _camera(bool start) {
    _operations = _operations.then((_) async {
      if (start) {
        if (!mounted || _accepted || !_foreground) return;
        await _controller.start();
        // Permission dialogs can background the app during the first start.
        if (!mounted || !_foreground) await _controller.stop();
      } else {
        await _controller.stop();
      }
    }).catchError((Object _) {
      if (mounted) setState(() => _failed = true);
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (!_controller.value.hasCameraPermission) return;
    _camera(state == AppLifecycleState.resumed);
  }

  Future<void> _detect(BarcodeCapture capture) async {
    if (_accepted || !mounted || !_foreground) return;
    final codes = capture.barcodes.where((code) => code.rawValue != null).toList();
    if (codes.isEmpty) return;
    _accepted = true;
    _camera(false);
    await _operations;
    if (mounted && !_failed) widget.onCode(codes.first.rawValue);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    // Serialize disposal behind permission/start/stop operations.
    unawaited(_operations.then((_) => _controller.dispose()).catchError((Object _) {}));
    super.dispose();
  }

  Widget _cameraError() => const Center(child: Text(
    'Camera unavailable or permission denied. Allow camera access in Android '
    'Settings and retry, or enter the Cart ID manually.', textAlign: TextAlign.center));

  @override
  Widget build(BuildContext context) => _failed ? _cameraError() : MobileScanner(
    controller: _controller, useAppLifecycleState: false, onDetect: _detect,
    errorBuilder: (context, error) => _cameraError(),
    onDetectError: (error, stack) {
      if (mounted) setState(() => _failed = true);
    },
  );
}

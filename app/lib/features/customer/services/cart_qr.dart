/// QR labels carry a physical cart ID only, never ownership or credentials.
String parseCartQr(String? payload) {
  if (payload == null || !RegExp(r'^CART-[A-Z0-9]{1,32}$').hasMatch(payload) ||
      payload.contains('\n') || payload.contains('\r')) {
    throw const FormatException('Scan a SmartCart label such as CART-001.');
  }
  return payload;
}

/// Latches synchronously before any camera shutdown or asynchronous pairing.
class CartScanGate {
  bool _accepted = false;
  String? accept(String? payload) {
    if (_accepted) return null;
    final id = parseCartQr(payload);
    _accepted = true;
    return id;
  }
}

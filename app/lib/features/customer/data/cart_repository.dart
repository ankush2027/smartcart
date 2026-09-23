import '../models/customer_cart.dart';

abstract interface class CartRepository {
  /// Presentation disclosure; does not claim physical device connectivity.
  bool get isDemo;

  /// Null means no current session. Backend adapters provide authoritative totals.
  Future<CustomerCart?> loadCurrentCart();
}

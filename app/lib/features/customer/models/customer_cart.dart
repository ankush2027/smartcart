/// Client presentation data, independent of backend session implementation.
enum CartStatus { active, completed }

class CartProduct {
  const CartProduct({required this.name, this.id});
  final String? id;
  final String name;
}

class CartItem {
  const CartItem({
    required this.product,
    required this.quantity,
    required this.unitPricePaise,
    required this.lineTotalPaise,
  }) : assert(quantity > 0),
       assert(unitPricePaise >= 0),
       assert(lineTotalPaise >= 0);

  final CartProduct product;
  final int quantity;
  final int unitPricePaise;
  // Supplied by the repository, not recalculated by widgets.
  final int lineTotalPaise;
}

class CustomerCart {
  CustomerCart({
    required this.displayId,
    required this.status,
    required List<CartItem> items,
    required this.subtotalPaise,
    required this.totalPaise,
    this.cartId,
    this.customerUid,
    this.startTime,
    this.endTime,
  }) : items = List.unmodifiable(items),
       assert(subtotalPaise >= 0),
       assert(totalPaise >= 0);

  /// Session path key for real data (a display label for mock fixtures).
  final String displayId;
  final String? cartId;
  final String? customerUid;
  final DateTime? startTime;
  final DateTime? endTime;
  final CartStatus status;
  final List<CartItem> items;
  final int subtotalPaise;
  final int totalPaise;
  int get itemCount => items.fold(0, (count, item) => count + item.quantity);
}

String formatRupees(int paise) {
  assert(paise >= 0);
  return '₹${paise ~/ 100}.${(paise % 100).toString().padLeft(2, '0')}';
}

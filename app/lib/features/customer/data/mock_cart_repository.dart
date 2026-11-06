import '../models/customer_cart.dart';
import 'cart_repository.dart';

/// Development fixtures only, not a product catalogue or pricing contract.
enum MockCartScenario { active, empty, noSession, failure }

class MockCartRepository implements CartRepository {
  MockCartRepository({
    this.scenario = MockCartScenario.active,
    this.delay = const Duration(milliseconds: 400),
  });

  final MockCartScenario scenario;
  final Duration delay;

  @override
  bool get isDemo => true;

  @override
  Future<CustomerCart?> loadCurrentCart() async {
    await Future<void>.delayed(delay);
    if (scenario == MockCartScenario.failure) {
      throw StateError('Simulated cart repository failure');
    }
    if (scenario == MockCartScenario.noSession) return null;
    final items = scenario == MockCartScenario.empty
        ? <CartItem>[]
        : [
            _sample('Noodles Rs 10', 1, 1000),
            _sample('Noodles Rs 5', 2, 500),
            _sample('Bru', 1, 1500),
            _sample('Coca Cola', 1, 4000),
            _sample('Lays', 2, 1000),
            _sample('Lifebuoy', 1, 3500),
          ];
    // Only the mock computes sample totals. Future adapters read backend totals.
    final total = items.fold(0, (sum, item) => sum + item.lineTotalPaise);
    return CustomerCart(
      displayId: 'Demo cart 01',
      status: CartStatus.active,
      items: items,
      subtotalPaise: total,
      totalPaise: total,
    );
  }

  CartItem _sample(String name, int quantity, int price) => CartItem(
    product: CartProduct(name: name),
    quantity: quantity,
    unitPricePaise: price,
    lineTotalPaise: quantity * price,
  );
}

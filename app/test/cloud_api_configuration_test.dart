import 'package:flutter_test/flutter_test.dart';
import 'package:smartcart/core/config/api_configuration.dart';
import 'package:smartcart/features/checkout/data/http_checkout_repository.dart';
import 'package:smartcart/features/customer/services/cart_pairing_service.dart';

void main() {
  test('pairing and checkout use one HTTPS base including its prefix', () {
    const url = 'https://api.example.test/v1/';
    expect(const PairingConfiguration(baseUrl: url).endpoint.toString(), '$url' 'pair-cart');
    expect(const BackendConfiguration(baseUrl: url).endpoint('/sessions/1/checkout').toString(),
        '$url' 'sessions/1/checkout');
  });
  for (final url in ['', 'http://192.168.137.98:5001', 'https://user:secret@host.test',
    'https://host.test?token=x', 'https://host.test#fragment', 'ftp://host.test']) {
    test('cloud configuration rejects $url', () {
      expect(() => apiEndpoint(url, '/pair-cart', allowDevelopmentHttp: false), throwsFormatException);
    });
  }
  test('explicit debug development HTTP remains available', () {
    expect(apiEndpoint('http://localhost:5001', '/pair-cart', allowDevelopmentHttp: true).scheme, 'http');
  });
}

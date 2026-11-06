from decimal import Decimal, localcontext

import pytest

from edge.payment.money import to_paise, PaymentError, MAX_SAFE_PAISE


@pytest.mark.parametrize('value,expected', [(10, 1000), (10.5, 1050), (10.50, 1050),
                                           (0, 0), (0.01, 1), (100000, 10000000)])
def test_exact_paise(value, expected):
    with localcontext() as context:
        context.prec = 2  # Caller context must not silently round the conversion.
        assert to_paise(value) == expected


@pytest.mark.parametrize('value', [10.005, 20 + 1e-10, 0.1 + 0.2, -1, float('nan'),
                                   float('inf'), -float('inf'), True, False, '10', None,
                                   Decimal('10'), 100000.01, 10**100, 1e308, 5e-324])
def test_rejects_invalid_or_inexact_money(value):
    with pytest.raises(PaymentError):
        to_paise(value)


def test_configurable_cap():
    assert to_paise(10, max_paise=1000) == 1000
    with pytest.raises(PaymentError, match='limit'):
        to_paise(10.01, max_paise=1000)
    assert to_paise(100001, max_paise=10000100) == 10000100


def test_arbitrarily_large_integer_has_controlled_error():
    for value in (10**5000, -(10**5000)):
        with pytest.raises(PaymentError):
            to_paise(value)


@pytest.mark.parametrize('cap', [0, -1, True, 100.0, MAX_SAFE_PAISE + 1, 2**63])
def test_cap_rejects_unsafe_configuration(cap):
    with pytest.raises(ValueError):
        to_paise(1, max_paise=cap)

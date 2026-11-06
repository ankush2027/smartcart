"""The only REAL-INR to integer-paise boundary; never rounds a frozen quote."""
from decimal import Decimal, localcontext

from edge.billing.checkout import CheckoutError


class PaymentError(CheckoutError):
    """Controlled payment error, compatible with the existing API contract."""


# Development project cap: INR 100,000. Inject a lower/higher cap explicitly;
# an eventual adapter must additionally enforce its documented provider limit.
DEFAULT_MAX_PAISE = 10_000_000
MAX_SAFE_PAISE = 2**53 - 1


def validate_cap(max_paise):
    if type(max_paise) is not int or not 1 <= max_paise <= MAX_SAFE_PAISE:
        raise ValueError('Payment cap must be a positive safe integer in paise')


def to_paise(value, *, max_paise=DEFAULT_MAX_PAISE):
    validate_cap(max_paise)
    if type(value) not in (int, float) or value < 0:
        raise PaymentError('invalid_payment_amount', 'Checkout amount cannot be paid.')
    # Bound arbitrary Python integers before str() can hit its digit safety limit.
    if type(value) is int and value > max_paise:
        raise PaymentError('payment_amount_limit', 'Checkout exceeds the payment limit.')
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise PaymentError('invalid_payment_amount', 'Checkout amount cannot be paid.')
    with localcontext() as context:
        context.prec = 50  # Exceeds bounded integer/float input precision.
        if amount > Decimal(max_paise) / 100:
            raise PaymentError('payment_amount_limit', 'Checkout exceeds the payment limit.')
        paise = amount * Decimal('100')
        if paise != paise.to_integral_value():
            raise PaymentError('payment_amount_precision', 'Checkout amount is not exact to paise.')
        return int(paise)

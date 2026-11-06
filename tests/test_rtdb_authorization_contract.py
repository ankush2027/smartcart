"""Offline rule-contract tests, not an emulator or proof of deployed rules.

Evaluate the actual checked-in predicates using only their small boolean/strict
comparison subset. Unknown syntax fails the tests rather than being ignored.
Firebase query execution and rule cascading still require emulator validation.
"""
import ast
import json
from pathlib import Path

import pytest

RULES = json.loads((Path(__file__).resolve().parents[1] / 'configs/firebase_rules.json').read_text())['rules']


def predicate(expression, *, uid=None, claims=None, order=None, equal=None, owner=None):
    if isinstance(expression, bool):
        return expression
    substitutions = {
        "data.child('customer_uid').val()": 'owner',
        'auth.token.role': 'role',
        'auth.uid': 'uid',
        'query.orderByChild': 'order',
        'query.equalTo': 'equal',
        'auth != null': 'authenticated',
        '===': '==', '&&': ' and ', '||': ' or ',
    }
    for original, replacement in substitutions.items():
        expression = expression.replace(original, replacement)
    values = dict(authenticated=uid is not None, uid=uid,
                  role=(claims or {}).get('role'), order=order, equal=equal, owner=owner)

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BoolOp):
            assert isinstance(node.op, (ast.And, ast.Or))
            operands = (visit(value) for value in node.values)
            return all(operands) if isinstance(node.op, ast.And) else any(operands)
        if isinstance(node, ast.Compare):
            assert len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
            left, right = visit(node.left), visit(node.comparators[0])
            return type(left) is type(right) and left == right
        raise AssertionError(f'Unsupported rule syntax: {ast.dump(node)}')

    return visit(ast.parse(expression, mode='eval').body)


@pytest.mark.parametrize('uid,claims,order,equal,allowed', [
    (None, {}, None, None, False),
    (None, {}, 'customer_uid', 'alice', False),
    ('alice', {}, None, None, False),
    ('alice', {}, 'customer_uid', 'alice', True),
    ('alice', {'role': 'customer'}, 'customer_uid', 'alice', True),
    ('alice', {}, 'customer_uid', 'bob', False),
    ('alice', {}, 'customer_uid', None, False),
    ('alice', {}, 'customer_uid', 123, False),
    ('alice', {}, 'cart_id', 'alice', False),
    ('alice', {}, None, 'alice', False),
    ('admin', {'role': 'manager'}, None, None, True),
    ('admin', {'role': 'Manager'}, None, None, False),
    ('admin', {'role': True}, None, None, False),
    ('admin', {'manager': True}, None, None, False),
    ('admin', {'role': 'customer', 'manager': True}, None, None, False),
    ('admin', {'role': 'manager', 'manager': False}, None, None, True),
])
def test_collection_read_contract(uid, claims, order, equal, allowed):
    assert predicate(RULES['sessions']['.read'], uid=uid, claims=claims,
                     order=order, equal=equal) is allowed


@pytest.mark.parametrize('end_time', [None, '2026-09-25 12:00:00'])
@pytest.mark.parametrize('uid,claims,owner,allowed', [
    (None, {}, None, False),
    (None, {}, 'alice', False),
    ('alice', {}, 'alice', True),
    ('alice', {}, 'bob', False),
    ('alice', {}, None, False),
    ('alice', {}, '', False),
    ('alice', {}, True, False),
    ('alice', {}, {'uid': 'alice'}, False),
    ('alice', {}, ['alice'], False),
    ('alice', {}, 123, False),
    ('admin', {'role': 'manager'}, 'bob', True),
    ('admin', {'manager': True}, 'bob', False),
])
def test_direct_session_ownership_active_and_completed(end_time, uid, claims, owner, allowed):
    record = {'customer_uid': owner, 'end_time': end_time}
    # end_time never loosens ownership, including historical completed sessions.
    assert predicate(RULES['sessions']['$session_id']['.read'], uid=uid,
                     claims=claims, owner=record['customer_uid']) is allowed


def test_exact_rule_tree_prevents_unreviewed_cascading_grants():
    assert set(RULES) == {'.read', '.write', 'sessions', 'products'}
    assert RULES['.read'] is False
    assert set(RULES['sessions']) == {'.read', '.write', '.indexOn', '$session_id'}
    assert RULES['sessions']['.indexOn'] == ['customer_uid']
    assert set(RULES['sessions']['$session_id']) == {'.read', '.write'}
    assert RULES['sessions']['$session_id']['.read'] == (
        "auth != null && (auth.token.role === 'manager' || "
        "data.child('customer_uid').val() === auth.uid)"
    )
    assert set(RULES['products']) == {'.read', '.write'}
    assert RULES['products']['.read'] == "auth != null && auth.token.role === 'manager'"


def test_all_client_writes_remain_denied_at_every_rule_node():
    def walk(node):
        assert node['.write'] is False
        for key, child in node.items():
            if not key.startswith('.'):
                walk(child)
    walk(RULES)
    # No deeper grant can authorize totals, bill_items, events, ownership,
    # cart_id, completion, products, deletes or multipath client updates.


@pytest.mark.parametrize('uid,claims,allowed', [
    (None, {}, False),
    ('alice', {}, False),
    ('alice', {'role': 'customer'}, False),
    ('admin', {'manager': True}, False),
    ('admin', {'role': 'manager'}, True),
])
def test_products_keep_manager_only_read_scope(uid, claims, allowed):
    assert predicate(RULES['products']['.read'], uid=uid, claims=claims) is allowed


def test_only_one_manager_claim_convention_exists():
    content = json.dumps(RULES)
    assert 'auth.token.manager' not in content
    assert 'auth.token.role' in content

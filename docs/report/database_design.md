# SmartCart Database Design

## products
Purpose:
Stores product information such as product name, YOLO class, barcode, price and weight.

Fields:
- product_id
- yolo_class
- barcode
- product_name
- price
- weight_g

---

## bill_items
Purpose:
Stores items currently present in the customer's cart.

Fields:
- bill_item_id
- session_id
- product_id
- quantity

---

## sessions
Purpose:
Stores shopping session information.

Fields:
- session_id
- start_time
- end_time
- status

---

## session_log
Purpose:
Stores every ADD and REMOVE event.

Fields:
- log_id
- session_id
- product_id
- event_type
- timestamp
- confidence
- weight_delta

---

## threshold_log
Purpose:
Stores adaptive threshold updates.

Fields:
- threshold_id
- product_id
- old_threshold
- new_threshold
- timestamp




## Notes

Example Products

1. Maggi Small
   - Price: ₹10
   - Weight: 35g

2. Maggi Large
   - Price: ₹20
   - Weight: 70g

3. Chips
   - Price: ₹20
   - Weight: 45g
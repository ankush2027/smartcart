# SmartCart System Flow

## Overview

SmartCart is an automated shopping cart that uses computer vision and weight sensing to detect products, generate bills automatically, sync data to Firebase, and display the bill in a Flutter mobile application.

---

## Complete Flow

Pi Camera
↓
OpenCV MOG2
↓
Candidate Event
↓
HX711 Weight Check
↓
Dual Channel Confirmation
↓
YOLOv8 Product Identification
↓
Confidence Check
↓
Barcode Fallback
↓
SQLite Billing
↓
Session Logging
↓
Theft Detection
↓
Firebase Sync
↓
Flutter App
↓
UPI Payment

---

## Example Shopping Session

Customer adds a packet of chips.

1. Camera captures frame.
2. MOG2 detects motion.
3. HX711 detects weight increase.
4. Event confirmed.
5. YOLO identifies product.
6. SQLite updates bill.
7. Firebase syncs data.
8. Flutter app updates total.
9. Customer pays using UPI QR.
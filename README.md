# SmartCart

SmartCart is a smart shopping cart system designed to automate item detection, billing, and shopping activity monitoring.

## Project Overview

The system uses computer vision and weight sensing to detect when products are added to or removed from the cart. Product information is stored locally and used to maintain the shopping bill.

## Current Technology

* Python
* OpenCV
* SQLite
* Raspberry Pi
* YOLOv8
* Firebase
* Flutter

## Project Structure

```text
smart-cart/
├── app/
├── edge/
│   ├── billing/
│   ├── database/
│   ├── detection/
│   ├── firebase/
│   ├── model/
│   ├── theft/
│   └── weight/
├── data/
├── docs/
├── notebooks/
├── tests/
├── .gitignore
├── README.md
└── requirements.txt
```

## Current Progress

* Camera-based movement detection using OpenCV MOG2
* Movement event generation and confirmation
* Weight-change detection module
* SQLite database initialization
* Product repository
* Shopping session management
* ADD/REMOVE billing operations
* Session event logging
* Bill total calculation
* Basic database and billing tests

## Development Status

The project is currently being developed and tested on a laptop. Raspberry Pi and other hardware components will be integrated later.

## Future Work

* Integrate YOLOv8 product identification
* Connect product identification with the database
* Integrate confirmed events with billing
* Implement theft detection
* Integrate Firebase
* Develop the Flutter mobile application
* Deploy the complete system on Raspberry Pi
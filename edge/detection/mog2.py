import cv2


camera = cv2.VideoCapture(0)

if not camera.isOpened():
    print("Could not open camera")
    exit()


mog2 = cv2.createBackgroundSubtractorMOG2(
    history=500,
    varThreshold=50,
    detectShadows=True
)

kernel = cv2.getStructuringElement(
    cv2.MORPH_ELLIPSE,
    (5, 5)
)


while True:
    success, frame = camera.read()

    if not success:
        print("Could not read frame")
        break

    mask = mog2.apply(frame)

    # Remove small noise
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        kernel
    )

    # Fill small gaps
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        kernel
    )

    foreground_pixels = cv2.countNonZero(mask)

    if foreground_pixels > 1000:
        print("Movement detected")

    cv2.imshow("Original", frame)
    cv2.imshow("Clean MOG2 Mask", mask)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break


camera.release()
cv2.destroyAllWindows()
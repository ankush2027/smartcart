"""Reusable MOG2 gesture detection and the original standalone camera demo."""
from datetime import datetime

from edge.detection.event import DetectionEvent

REQUIRED_FRAMES = 5


class MOG2Processor:
    def __init__(self, *, cv=None):
        if cv is None:
            import cv2 as cv
        self.cv = cv
        self.background = cv.createBackgroundSubtractorMOG2(
            history=500, varThreshold=50, detectShadows=True)
        self.kernel = cv.getStructuringElement(cv.MORPH_ELLIPSE, (5, 5))
        self.movement_frames = 0
        self.movement_active = False
        self.mask = None
        self.bounding_box = None

    def process(self, frame, *, timestamp=None):
        """Return one movement event per gesture; never annotate the input."""
        cv = self.cv
        mask = self.background.apply(frame)
        mask = cv.morphologyEx(mask, cv.MORPH_OPEN, self.kernel)
        self.mask = cv.morphologyEx(mask, cv.MORPH_CLOSE, self.kernel)
        contours, _ = cv.findContours(self.mask, cv.RETR_EXTERNAL,
                                     cv.CHAIN_APPROX_SIMPLE)
        contour = max(contours, key=cv.contourArea, default=None)
        if contour is not None and cv.contourArea(contour) > 500:
            self.movement_frames += 1
            self.bounding_box = cv.boundingRect(contour)
        else:
            self.movement_frames = 0
            self.movement_active = False
            self.bounding_box = None
        if self.movement_frames >= REQUIRED_FRAMES and not self.movement_active:
            self.movement_active = True
            return DetectionEvent(timestamp or datetime.now(), 'MOVEMENT',
                                  self.bounding_box, cv.countNonZero(self.mask))
        return None


def run_motion_detector(camera_index=0):
    import cv2
    from edge.detection.camera import open_camera
    camera = open_camera(camera_index)
    processor = MOG2Processor()
    try:
        while True:
            success, frame = camera.read()
            if not success:
                break
            event = processor.process(frame)
            if event is not None:
                print(event)
            display = frame.copy()
            if processor.bounding_box is not None:
                x, y, w, h = processor.bounding_box
                cv2.rectangle(display, (x, y), (x + w, y + h), (0, 255, 0), 2)
            cv2.imshow('Original', display)
            cv2.imshow('Clean MOG2 Mask', processor.mask)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
    finally:
        camera.release()
        cv2.destroyAllWindows()


if __name__ == '__main__':
    run_motion_detector()

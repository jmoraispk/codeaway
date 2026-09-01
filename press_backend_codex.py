from dataclasses import dataclass

from press_backends import BackendEvaluation


@dataclass(frozen=True)
class CodexDesktopBackend:
    id: str = "codex_desktop"
    label: str = "Codex"

    def evaluate(self, rgb):
        import cv2
        import numpy as np

        height, width = rgb.shape[:2]
        x0, x1 = int(width * 0.165), int(width * 0.218)
        y0, y1 = int(height * 0.12), int(height * 0.95)
        crop = rgb[y0:y1, x0:x1]
        r = crop[:, :, 0].astype(np.int16)
        g = crop[:, :, 1].astype(np.int16)
        b = crop[:, :, 2].astype(np.int16)
        mask = ((b >= 150) & ((b-r) >= 60) & ((b-g) >= 20) & (g >= 70)).astype(np.uint8) * 255
        count, _labels, stats, centers = cv2.connectedComponentsWithStats(mask, 8)
        markers = []
        for idx in range(1, count):
            _left, _top, component_w, component_h, area = [int(v) for v in stats[idx]]
            ratio = component_w / component_h
            if 12 <= area <= 500 and 3 <= component_w <= 30 and 3 <= component_h <= 30 and 0.5 <= ratio <= 2.0:
                cx, cy = centers[idx]
                markers.append((x0 + int(round(cx)), y0 + int(round(cy))))
        markers.sort(key=lambda point: (point[1], point[0]))
        marker_tuple = tuple(markers)
        return BackendEvaluation(
            ready=bool(marker_tuple), score=1.0 if marker_tuple else 0.0,
            ready_count=len(marker_tuple), marker_centers=marker_tuple,
        )

    def transform_click(self, rgb, requested_xy):
        evaluation = self.evaluate(rgb)
        requested_x, requested_y = requested_xy
        _height, width = rgb.shape[:2]
        radius = max(18, int(round(width * 0.018)))
        nearby = [marker for marker in evaluation.marker_centers
                  if (marker[0]-requested_x)**2 + (marker[1]-requested_y)**2 <= radius**2]
        offset = max(24, int(round(width * 0.025)))
        if nearby:
            marker_x, marker_y = min(nearby, key=lambda marker:
                (marker[0]-requested_x)**2 + (marker[1]-requested_y)**2)
            return max(0, marker_x - offset), marker_y
        if int(width * 0.165) <= requested_x <= int(width * 0.218):
            return max(0, requested_x - offset), requested_y
        return requested_xy

    def scroll_target(self, region):
        x, y, width, height = [int(v) for v in region]
        return x + int(round(width * 0.60)), y + int(round(height * 0.45))

    def send_target(self, region):
        x, y, width, height = [int(v) for v in region]
        return x + int(round(width * 0.60)), y + int(round(height * 0.92))

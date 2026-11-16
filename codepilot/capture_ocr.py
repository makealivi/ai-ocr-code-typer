import threading


class CaptureOCR:
    def __init__(self):
        self.engine = None
        self.lock = threading.Lock()

    def capture(self):
        import mss
        import numpy as np
        with mss.mss() as screen:
            return np.asarray(screen.grab(screen.monitors[0]))[:, :, :3].copy()

    def recognize(self, image):
        from rapidocr_onnxruntime import RapidOCR
        with self.lock:
            if self.engine is None:
                self.engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
            results, _ = self.engine(image)
        if not results:
            raise ValueError("全屏截图中未识别到文字，请显示题目后重新截图")
        # RapidOCR already sorts detected lines; preserve its reading order and line breaks.
        return "\n".join(item[1] for item in results)

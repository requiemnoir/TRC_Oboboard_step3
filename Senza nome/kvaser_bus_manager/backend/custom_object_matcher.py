import json
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

try:
    import cv2  # type: ignore
except Exception:
    cv2 = None


def _safe_name(name: str) -> str:
    raw = (name or '').strip()
    safe = re.sub(r'[^a-zA-Z0-9_-]+', '_', raw).strip('_')
    return safe[:64]


@dataclass
class ObjectInfo:
    name: str
    sample_count: int
    trained: bool


class CustomObjectMatcher:
    """Simple local custom-object recognizer.

    It does NOT modify YOLO weights. It stores multiple captured samples per object name and
    trains an ORB descriptor database. Detection uses ORB matching on the current frame.

    This preserves all existing YOLO classes while enabling user-defined objects.
    """

    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)
        self.objects_dir = os.path.join(self.base_dir, 'objects')
        os.makedirs(self.objects_dir, exist_ok=True)

    def _obj_dir(self, name: str) -> str:
        safe = _safe_name(name)
        if not safe:
            raise ValueError('invalid name')
        return os.path.join(self.objects_dir, safe)

    def _samples_dir(self, name: str) -> str:
        return os.path.join(self._obj_dir(name), 'samples')

    def _model_path(self, name: str) -> str:
        return os.path.join(self._obj_dir(name), 'orb_model.json')

    def list_objects(self) -> List[Dict]:
        out: List[Dict] = []
        if not os.path.isdir(self.objects_dir):
            return out
        for entry in sorted(os.listdir(self.objects_dir)):
            p = os.path.join(self.objects_dir, entry)
            if not os.path.isdir(p):
                continue
            samples = os.path.join(p, 'samples')
            count = 0
            if os.path.isdir(samples):
                count = len([f for f in os.listdir(samples) if f.lower().endswith(('.jpg', '.jpeg', '.png'))])
            trained = os.path.isfile(os.path.join(p, 'orb_model.json'))
            out.append({'name': entry, 'sample_count': count, 'trained': trained})
        return out

    def capture_sample(self, name: str, frame_bgr) -> Dict:
        if cv2 is None:
            raise RuntimeError('opencv not available')
        safe = _safe_name(name)
        if not safe:
            raise ValueError('invalid name')

        d = self._samples_dir(safe)
        os.makedirs(d, exist_ok=True)
        ts = int(time.time() * 1000)
        path = os.path.join(d, f'{ts}.jpg')

        ok = cv2.imwrite(path, frame_bgr)
        if not ok:
            raise RuntimeError('failed to write sample')

        return {'name': safe, 'path': path, 'timestamp_ms': ts}

    def train(self, name: str, *, max_features: int = 800) -> Dict:
        if cv2 is None:
            raise RuntimeError('opencv not available')
        safe = _safe_name(name)
        if not safe:
            raise ValueError('invalid name')

        samples_dir = self._samples_dir(safe)
        if not os.path.isdir(samples_dir):
            raise RuntimeError('no samples')

        sample_paths = [
            os.path.join(samples_dir, f)
            for f in sorted(os.listdir(samples_dir))
            if f.lower().endswith(('.jpg', '.jpeg', '.png'))
        ]
        if len(sample_paths) < 3:
            raise RuntimeError('need at least 3 samples')

        orb = cv2.ORB_create(nfeatures=int(max_features))

        descriptors_list = []
        used = 0
        for p in sample_paths:
            img = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            kp, des = orb.detectAndCompute(img, None)
            if des is None or len(des) == 0:
                continue
            descriptors_list.append(des)
            used += 1

        if used == 0:
            raise RuntimeError('no features found in samples')

        # Store descriptors as a list of lists (json-friendly via .tolist()).
        payload = {
            'name': safe,
            'type': 'orb',
            'max_features': int(max_features),
            'trained_at_ms': int(time.time() * 1000),
            'samples_used': int(used),
            'descriptors': [d.tolist() for d in descriptors_list],
        }

        obj_dir = self._obj_dir(safe)
        os.makedirs(obj_dir, exist_ok=True)
        with open(self._model_path(safe), 'w', encoding='utf-8') as f:
            json.dump(payload, f)

        return {
            'name': safe,
            'trained': True,
            'samples_used': int(used),
        }

    def _load_model(self, name: str) -> Optional[Dict]:
        safe = _safe_name(name)
        if not safe:
            return None
        path = self._model_path(safe)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None

    def detect(self, frame_bgr, *, names_filter: Optional[List[str]] = None, threshold: int = 20) -> Optional[Dict]:
        if cv2 is None:
            return None
        if frame_bgr is None:
            return None

        try:
            gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        except Exception:
            return None

        orb = cv2.ORB_create(nfeatures=800)
        kp2, des2 = orb.detectAndCompute(gray, None)
        if des2 is None or len(des2) == 0:
            return {
                'matched': False,
                'reason': 'no_features_in_frame',
                'threshold': int(threshold),
                'scores': {},
            }

        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

        candidates = self.list_objects()
        if names_filter:
            wanted = set([_safe_name(n) for n in names_filter if _safe_name(n)])
            candidates = [c for c in candidates if c.get('name') in wanted]

        scores: Dict[str, int] = {}
        best_name = None
        best_score = 0

        for c in candidates:
            name = c.get('name')
            if not name:
                continue
            model = self._load_model(name)
            if not model or model.get('type') != 'orb':
                continue

            desc_arrays = model.get('descriptors') or []
            score_total = 0
            for arr in desc_arrays:
                try:
                    import numpy as np  # type: ignore

                    des1 = np.array(arr, dtype='uint8')
                    if des1 is None or len(des1) == 0:
                        continue
                    matches = bf.match(des1, des2)
                    # Use count of reasonably-close matches as score
                    good = [m for m in matches if m.distance < 50]
                    score_total += len(good)
                except Exception:
                    continue

            scores[name] = int(score_total)
            if score_total > best_score:
                best_score = int(score_total)
                best_name = name

        matched = bool(best_name) and best_score >= int(threshold)
        return {
            'matched': matched,
            'best': {'name': best_name, 'score': int(best_score)} if best_name else None,
            'threshold': int(threshold),
            'scores': scores,
        }

#!/usr/bin/env python3
"""CI에서 수식 모델을 실제로 돌려 본다(onnxruntime). 앱과 같은 math_ocr.py를 쓴다.
사용: math_check.py <모델 폴더> <출력 폴더> 그림...
모델 폴더: mfd.onnx, mfr_encoder.onnx, mfr_decoder.onnx (+ 설정 json). 같은 폴더에 q/ 가 있으면 int8 판도 비교한다."""
import json
import os
import sys
import time

import cv2
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app", "src", "main", "python"))
import math_ocr as M  # noqa: E402

mdir, out = sys.argv[1], sys.argv[2]
os.makedirs(out, exist_ok=True)
rep = {"models": {}, "images": []}
runner = M.LocalRunner(mdir)
for n in ("mfd", "mfr_encoder", "mfr_decoder"):
    rep["models"][n] = dict(runner.info(n), mb=round(os.path.getsize(os.path.join(mdir, n + ".onnx")) / 1e6, 1))
print(json.dumps(rep["models"], indent=1, default=str), flush=True)
finder, reader = M.MathFinder(runner), M.MathReader(runner, mdir)
qdir = os.path.join(mdir, "q")
qreader = M.MathReader(M.LocalRunner(qdir), mdir) if os.path.exists(os.path.join(qdir, "mfr_encoder.onnx")) else None
font = M.math_font([mdir, os.path.join(os.path.dirname(__file__), "..", "app", "src", "main", "assets")])
for path in sys.argv[3:]:
    img = cv2.imread(path)
    t = time.time()
    boxes = finder.find(img)
    td = time.time() - t
    vis = img.copy()
    items = []
    for k, (x1, y1, x2, y2, sc, c) in enumerate(sorted(boxes, key=lambda b: (round(b[1] / 40), b[0]))):
        p = 4
        crop = img[max(0, int(y1) - p):int(y2) + p, max(0, int(x1) - p):int(x2) + p]
        t1 = time.time()
        tex = reader.read(crop)
        tr = time.time() - t1
        it = dict(box=[round(float(v), 1) for v in (x1, y1, x2, y2)], score=round(sc, 3), kind=c, latex=tex, sec=round(tr, 2))
        if qreader:
            t2 = time.time()
            it["latex_int8"] = qreader.read(crop)
            it["sec_int8"] = round(time.time() - t2, 2)
        r = M.latex_svg(tex, 34, font) if font else None
        it["render_ok"] = bool(r)
        items.append(it)
        cv2.rectangle(vis, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255) if c == 0 else (255, 0, 0), 2)
        cv2.putText(vis, str(k), (int(x1), int(y1) - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        print(k, it, flush=True)
    name = os.path.splitext(os.path.basename(path))[0]
    cv2.imwrite(os.path.join(out, name + "_boxes.jpg"), vis, [cv2.IMWRITE_JPEG_QUALITY, 80])
    rep["images"].append(dict(image=name, detect_sec=round(td, 2), formulas=items))
json.dump(rep, open(os.path.join(out, "math_report.json"), "w"), ensure_ascii=False, indent=1, default=str)

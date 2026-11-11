"""안드로이드 앱에서 scan2svg를 부르는 진입점.

앱(Kotlin)이 넘겨주는 bridge 객체로 진행 메시지를 보내고, OCR은 기기 내장 엔진(ML Kit)에 맡긴다.
  bridge.log(문자열)            진행 메시지
  bridge.ocr(PNG 경로) -> JSON  {"words": [{text,x,y,w,h,conf}], "chars": [[글자, 왼쪽, 오른쪽], ...]}
"""
import json
import os
import traceback

import cv2

import scan2svg as S


class _DeviceOcr:
    def __init__(self, bridge, tmp_dir):
        self.bridge, self.tmp_dir, self.n = bridge, tmp_dir, 0

    def __call__(self, img, single_line, lang):
        self.n += 1
        path = os.path.join(self.tmp_dir, "ocr_in.png")
        cv2.imwrite(path, img)
        d = json.loads(str(self.bridge.ocr(path)))
        words = [dict(text=w["text"], x=float(w["x"]), y=float(w["y"]), w=float(w["w"]), h=float(w["h"]),
                      conf=float(w["conf"])) for w in d.get("words", []) if w.get("text", "").strip()]
        chars = [(c[0], float(c[1]), float(c[2])) for c in d.get("chars", [])]
        return words, chars


def run(image_path, out_dir, font_dirs, cache_dir, bridge):
    """사진 한 장을 변환한다. 결과 파일은 out_dir에: result.svg, result_preview.png, result_compare.jpg, result_report.json
    돌려주는 값은 JSON 문자열: {"ok": true, "stats": {...}} 또는 {"ok": false, "error": "...", "trace": "..."}"""
    try:
        image_path, out_dir, cache_dir = str(image_path), str(out_dir), str(cache_dir)
        os.makedirs(out_dir, exist_ok=True)
        os.makedirs(cache_dir, exist_ok=True)
        S.LOG_SINK = lambda s: bridge.log(str(s))
        S.CACHE_DIR = cache_dir
        ocr = _DeviceOcr(bridge, cache_dir)
        S.OCR_BACKEND = ocr
        stats = S.process(image_path, os.path.join(out_dir, "result.svg"),
                          font_dirs=[str(d) for d in font_dirs], max_side=2400)
        stats["ocr_calls"] = ocr.n
        return json.dumps(dict(ok=True, stats=stats), ensure_ascii=False, default=str)
    except BaseException as e:  # process()는 입력이 잘못되면 SystemExit를 던진다
        return json.dumps(dict(ok=False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), ensure_ascii=False)


def versions():
    """앱 정보 화면·테스트용: 들어 있는 라이브러리 버전과 조판 방식."""
    import numpy
    import PIL
    from PIL import features
    return json.dumps(dict(opencv=cv2.__version__, numpy=numpy.__version__, pillow=PIL.__version__,
                           raqm=bool(features.check("raqm")), freetype=features.version("freetype2")))

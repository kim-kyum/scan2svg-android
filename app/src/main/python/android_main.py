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
    char_y = True  # 글자 상자의 세로 중심도 준다 → 다시 읽기를 줄들을 쌓은 몇 장으로 끝낼 수 있다

    def __init__(self, bridge, tmp_dir):
        self.bridge, self.tmp_dir, self.n = bridge, tmp_dir, 0

    def __call__(self, img, single_line, lang):
        self.n += 1
        path = os.path.join(self.tmp_dir, "ocr_in.png")
        cv2.imwrite(path, img)
        d = json.loads(str(self.bridge.ocr(path)))
        words = [dict(text=w["text"], x=float(w["x"]), y=float(w["y"]), w=float(w["w"]), h=float(w["h"]),
                      conf=float(w["conf"])) for w in d.get("words", []) if w.get("text", "").strip()]
        chars = [(c[0], float(c[1]), float(c[2])) + ((float(c[3]),) if len(c) > 3 else ()) for c in d.get("chars", [])]
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


def _faces(font_dirs, cache_dir):
    S.CACHE_DIR = str(cache_dir)
    return S.load_fonts([str(d) for d in font_dirs] + S.default_font_dirs())  # 변환 때와 같은 글꼴 색인 캐시


def run_page(image_path, page_dir, font_dirs, cache_dir, bridge):
    """문서함의 한 쪽을 변환한다. page_dir에 result.svg, result_scan.jpg(펴고 보정한 원본), page.pdf(화면 표시·내보내기용),
    result_report.json을 남긴다. 돌려주는 값은 run()과 같은 JSON."""
    import time
    r = json.loads(run(image_path, page_dir, font_dirs, cache_dir, bridge))
    if not r.get("ok"):
        return json.dumps(r, ensure_ascii=False)
    try:
        import svg2pdf
        t = time.time()
        page_dir = str(page_dir)
        info = svg2pdf.svg_to_pdf(os.path.join(page_dir, "result.svg"), os.path.join(page_dir, "page.pdf"),
                                  _faces(font_dirs, cache_dir))
        r["stats"]["pdf_sec"] = round(time.time() - t, 2)
        r["stats"]["pdf"] = info
        bridge.log(f"[7] PDF 쪽 만들기 ({r['stats']['pdf_sec']}s)")
    except BaseException as e:
        r["stats"]["pdf_error"] = f"{type(e).__name__}: {e}"
        r["pdf_trace"] = traceback.format_exc()
    return json.dumps(r, ensure_ascii=False, default=str)


def export_pdf(spec_json, out_pdf, font_dirs, cache_dir):
    """여러 쪽을 PDF 하나로 묶는다(필기 포함).
    spec_json: {"title": "...", "pages": [{"svg": 경로, "ink": 경로|null, "scan": 경로|null}, ...]}"""
    try:
        import svg2pdf
        spec = json.loads(str(spec_json))
        info = svg2pdf.pages_to_pdf(spec["pages"], str(out_pdf), _faces(font_dirs, cache_dir), spec.get("title"))
        return json.dumps(dict(ok=True, info=info), ensure_ascii=False)
    except BaseException as e:
        return json.dumps(dict(ok=False, error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()), ensure_ascii=False)


def versions():
    """앱 정보 화면·테스트용: 들어 있는 라이브러리 버전과 조판 방식."""
    import numpy
    import PIL
    from PIL import features
    return json.dumps(dict(opencv=cv2.__version__, numpy=numpy.__version__, pillow=PIL.__version__,
                           raqm=bool(features.check("raqm")), freetype=features.version("freetype2")))


def font_probe(out_dir, font_dirs, text="요일 과학 동아리 Abc 2026"):
    """검사용: 글꼴이 이 기기에서 제대로 그려지는지 눈으로 볼 표본 그림(font_probe.png)을 만든다.
    font_dirs의 글꼴 전부와, 기기 글꼴 가운데 몇 가지를 한 줄씩 그리고 글자 폭도 적어 돌려준다."""
    from PIL import Image, ImageDraw
    out_dir = str(out_dir)
    rows, notes = [], []
    picked = []
    for d in [str(x) for x in font_dirs]:
        for root, _, files in os.walk(d):
            for fn in sorted(files):
                if fn.lower().endswith((".ttf", ".otf", ".ttc", ".otc")):
                    try:
                        picked += [S.Face(f) for f in S._scan_font_file(os.path.join(root, fn))]
                    except Exception as e:
                        notes.append(f"{fn}: 읽지 못함 {type(e).__name__}: {e}")
    system = S.load_fonts(S.default_font_dirs(), cache_path=os.path.join(out_dir, "probe_fonts.json"))
    picked += [f for f in system if f.family in ("Noto Sans CJK KR", "Noto Serif CJK KR", "Roboto") and f.weight in (400, 700)
               and not f.italic][:8]
    for f in picked:
        label = f"{f.family} / {f.style} / {f.weight} / {os.path.basename(f.path)}" + (f" var={f.var}" if f.var else "")
        im = Image.new("L", (1100, 96), 255)
        d = ImageDraw.Draw(im)
        d.text((8, 2), label.encode("ascii", "replace").decode(), fill=0)
        try:
            font = f.pil(48.0)
            S.t_draw(d, (12, 76), text, font, fill=0)
            note = f"{label}: 폭 {S.t_len(font, text):.1f}px, 요={f.adv('요'):.3f} A={f.adv('A'):.3f} 먹 {int((255 - __import__('numpy').asarray(im)[18:]).sum() // 255)}"
            try:
                note += f" axes={[(a['minimum'], a['default'], a['maximum']) for a in font.get_variation_axes()]}"
            except Exception:
                pass
        except Exception as e:
            note = f"{label}: 그리지 못함 {type(e).__name__}: {e}"
        notes.append(note)
        rows.append(im)
    if rows:
        sheet = Image.new("L", (1100, 96 * len(rows)), 255)
        for i, im in enumerate(rows):
            sheet.paste(im, (0, 96 * i))
        sheet.save(os.path.join(out_dir, "font_probe.png"))
    return "\n".join(notes)


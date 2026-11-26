"""수식 찾기·읽기·다시 그리기 (기기 안에서, 인터넷 없이).

- 찾기: Pix2Text MFD (YOLOv8, MIT) — 쪽 그림을 몇 조각으로 나눠 수식 상자를 찾는다(줄 안 수식 / 따로 선 수식)
- 읽기: Pix2Text MFR (TrOCR 구조, MIT) — 수식 그림 → LaTeX
- 다시 그리기: ziamath + STIX Two Math 글꼴로 LaTeX를 벡터로 그린다

모델 실행은 runner에 맡긴다. PC·CI에서는 onnxruntime(LocalRunner), 안드로이드에서는 앱의 ONNX Runtime을
파일로 주고받는 DeviceRunner가 같은 일을 한다. 그래서 전처리·후처리는 모두 여기 한 곳에 있다.
"""
import json
import os
import time

import cv2
import numpy as np


# ───────────────────────── 모델 실행기 ─────────────────────────
class LocalRunner:
    """onnxruntime이 있는 곳(PC, CI)."""

    def __init__(self, model_dir):
        import onnxruntime as ort
        self.ort, self.dir, self.sess, self.kept = ort, model_dir, {}, {}

    def _s(self, name):
        if name not in self.sess:
            so = self.ort.SessionOptions()
            so.intra_op_num_threads = min(4, os.cpu_count() or 2)
            self.sess[name] = self.ort.InferenceSession(os.path.join(self.dir, name + ".onnx"), so,
                                                        providers=["CPUExecutionProvider"])
        return self.sess[name]

    def info(self, name):
        s = self._s(name)
        return dict(inputs=[(i.name, i.shape, i.type) for i in s.get_inputs()],
                    outputs=[(o.name, o.shape, o.type) for o in s.get_outputs()])

    def run(self, model, feeds, outputs, keep=None, argmax_last=()):
        s = self._s(model)
        f = {k: (self.kept[v[1]] if isinstance(v, tuple) else v) for k, v in feeds.items()}
        names = [o for o in outputs]
        res = dict(zip(names, s.run(names, f)))
        out = {}
        for n, v in res.items():
            if keep and n in keep:
                self.kept[keep[n]] = v
            if n in argmax_last:
                out[n] = int(np.argmax(v[0, -1]))
            else:
                out[n] = v
        return out


class DeviceRunner:
    """안드로이드 앱(Kotlin)의 ONNX Runtime에 일을 맡긴다. 큰 배열은 파일로, 작은 것은 JSON으로 주고받는다.
    bridge.ort(요청 JSON) → 응답 JSON"""

    def __init__(self, bridge, tmp_dir):
        self.bridge, self.tmp = bridge, tmp_dir
        os.makedirs(tmp_dir, exist_ok=True)
        self.n = 0

    def run(self, model, feeds, outputs, keep=None, argmax_last=()):
        req = {"model": model, "inputs": {}, "outputs": []}
        for k, v in feeds.items():
            if isinstance(v, tuple):
                req["inputs"][k] = {"ref": v[1]}
                continue
            dt = "int64" if v.dtype == np.int64 else "float32"
            v = np.ascontiguousarray(v, dtype=np.int64 if dt == "int64" else np.float32)
            if v.size <= 512:
                req["inputs"][k] = {"data": v.ravel().tolist(), "shape": list(v.shape), "dtype": dt}
            else:
                self.n += 1
                p = os.path.join(self.tmp, f"in_{self.n % 8}_{k}.bin")
                v.tofile(p)
                req["inputs"][k] = {"file": p, "shape": list(v.shape), "dtype": dt}
        for n in outputs:
            o = {"name": n}
            if keep and n in keep:
                o["keep"] = keep[n]
            elif n in argmax_last:
                o["mode"] = "argmax_last"
            else:
                o["file"] = os.path.join(self.tmp, f"out_{n}.bin")
            req["outputs"].append(o)
        res = json.loads(str(self.bridge.ort(json.dumps(req))))
        if res.get("error"):
            raise RuntimeError(res["error"])
        out = {}
        for o in req["outputs"]:
            r = res["outputs"][o["name"]]
            if "argmax" in r:
                out[o["name"]] = int(r["argmax"])
            elif "file" in o:
                out[o["name"]] = np.fromfile(o["file"], dtype=np.float32).reshape(r["shape"])
        return out


# ───────────────────────── 찾기(MFD) ─────────────────────────
class MathFinder:
    def __init__(self, runner, size=768):
        self.r, self.S = runner, size

    def _detect(self, bgr):
        h, w = bgr.shape[:2]
        k = self.S / max(h, w)
        nh, nw = int(round(h * k)), int(round(w * k))
        img = np.full((self.S, self.S, 3), 114, np.uint8)
        img[:nh, :nw] = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_LINEAR)
        x = img[..., ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        out = self.r.run("mfd", {"images": x}, ["output0"])["output0"]
        p = out[0]
        if p.shape[0] < p.shape[1]:  # (4+nc, N) → (N, 4+nc)
            p = p.T
        boxes = []
        if p.shape[1] == 6 and np.all(p[:, 4] <= 1.0) and np.all(p[:, 5] == np.round(p[:, 5])) and p[:, 2].max() > p[:, 0].max():
            for x1, y1, x2, y2, sc, c in p:  # 이미 NMS를 거친 형식(x1,y1,x2,y2,점수,종류)
                if sc > 0.25:
                    boxes.append((x1 / k, y1 / k, x2 / k, y2 / k, float(sc), int(c)))
            return boxes
        sc = p[:, 4:]
        cls = sc.argmax(1)
        conf = sc.max(1)
        sel = conf > 0.25
        for (cx, cy, bw, bh), c, s in zip(p[sel, :4], cls[sel], conf[sel]):
            boxes.append(((cx - bw / 2) / k, (cy - bh / 2) / k, (cx + bw / 2) / k, (cy + bh / 2) / k, float(s), int(c)))
        return boxes

    def find(self, bgr, tiles=(2, 2), overlap=0.12):
        """쪽 전체를 조각내어 찾는다(작은 줄 안 수식도 놓치지 않게). 돌려주는 값: [(x1,y1,x2,y2,점수,종류)] 종류 0=줄 안, 1=따로"""
        H, W = bgr.shape[:2]
        nx, ny = tiles
        tw, th = W / (nx - (nx - 1) * overlap), H / (ny - (ny - 1) * overlap)
        allb = []
        for j in range(ny):
            for i in range(nx):
                x0 = int(i * tw * (1 - overlap))
                y0 = int(j * th * (1 - overlap))
                x1, y1 = min(W, int(x0 + tw)), min(H, int(y0 + th))
                for b in self._detect(bgr[y0:y1, x0:x1]):
                    allb.append((b[0] + x0, b[1] + y0, b[2] + x0, b[3] + y0, b[4], b[5]))
        return _nms_merge(allb)


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _nms_merge(boxes, thr=0.4):
    """조각이 겹치는 곳에서 같은 수식이 두 번 잡힌다: 많이 겹치면 점수 높은 것만, 한쪽이 다른 쪽에 거의 들어가면 합친다."""
    boxes = sorted(boxes, key=lambda b: -b[4])
    out = []
    for b in boxes:
        merged = False
        for k, o in enumerate(out):
            inter = max(0, min(b[2], o[2]) - max(b[0], o[0])) * max(0, min(b[3], o[3]) - max(b[1], o[1]))
            small = min((b[2] - b[0]) * (b[3] - b[1]), (o[2] - o[0]) * (o[3] - o[1]))
            if _iou(b, o) > thr or (small > 0 and inter / small > 0.7):
                out[k] = (min(b[0], o[0]), min(b[1], o[1]), max(b[2], o[2]), max(b[3], o[3]), max(b[4], o[4]), o[5])
                merged = True
                break
        if not merged:
            out.append(b)
    return out


# ───────────────────────── 읽기(MFR) ─────────────────────────
class MathReader:
    def __init__(self, runner, model_dir):
        self.r = runner
        rd = lambda n, d=None: json.load(open(os.path.join(model_dir, n), encoding="utf-8")) if os.path.exists(os.path.join(model_dir, n)) else (d or {})
        pre = rd("preprocessor_config.json")
        gen = rd("generation_config.json")
        cfg = rd("config.json")
        size = pre.get("size", 384)
        if isinstance(size, dict):
            self.size = (int(size.get("height", 384)), int(size.get("width", 384)))
        else:
            self.size = (int(size), int(size))
        self.mean = np.array(pre.get("image_mean", [0.5, 0.5, 0.5]), np.float32)
        self.std = np.array(pre.get("image_std", [0.5, 0.5, 0.5]), np.float32)
        dec = cfg.get("decoder", {})
        self.start = gen.get("decoder_start_token_id", cfg.get("decoder_start_token_id", dec.get("decoder_start_token_id", 2)))
        self.eos = gen.get("eos_token_id", cfg.get("eos_token_id", dec.get("eos_token_id", 2)))
        self.pad = gen.get("pad_token_id", cfg.get("pad_token_id", 1))
        self.max_len = int(gen.get("max_length", 256))
        tok = rd("tokenizer.json")
        vocab = tok.get("model", {}).get("vocab", {})
        if isinstance(vocab, list):  # Unigram
            self.id2tok = {i: v[0] for i, v in enumerate(vocab)}
        else:
            self.id2tok = {i: t for t, i in vocab.items()}
        for a in tok.get("added_tokens", []):
            self.id2tok[a["id"]] = a["content"]
        self.special = {a["content"] for a in tok.get("added_tokens", []) if a.get("special")}
        self.dec_type = json.dumps(tok.get("decoder", {}))
        self.enc_out = None

    def _prep(self, gray_or_bgr):
        im = gray_or_bgr
        if im.ndim == 2:
            im = cv2.cvtColor(im, cv2.COLOR_GRAY2BGR)
        h, w = self.size
        im = cv2.resize(im, (w, h), interpolation=cv2.INTER_CUBIC)[..., ::-1].astype(np.float32) / 255.0
        im = (im - self.mean) / self.std
        return im.transpose(2, 0, 1)[None].astype(np.float32)

    def decode(self, ids):
        toks = [self.id2tok.get(int(i), "") for i in ids]
        toks = [t for t in toks if t not in self.special]
        if "ByteLevel" in self.dec_type:
            bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
            cs = bs[:]
            n = 0
            for b in range(256):
                if b not in bs:
                    bs.append(b)
                    cs.append(256 + n)
                    n += 1
            inv = {chr(c): b for b, c in zip(bs, cs)}
            raw = bytes(inv.get(ch, 32) for ch in "".join(toks))
            return raw.decode("utf-8", "replace").strip()
        if "WordPiece" in self.dec_type:
            s = " ".join(toks).replace(" ##", "")
            return s.strip()
        if "Metaspace" in self.dec_type:
            return "".join(toks).replace("▁", " ").strip()
        return " ".join(toks).strip()

    def read(self, img):
        x = self._prep(img)
        self.r.run("mfr_encoder", {"pixel_values": x}, ["last_hidden_state"], keep={"last_hidden_state": "enc"})
        ids = [self.start]
        for _ in range(self.max_len):
            nxt = self.r.run("mfr_decoder", {"input_ids": np.array([ids], np.int64), "encoder_hidden_states": ("ref", "enc")},
                             ["logits"], argmax_last=("logits",))["logits"]
            if nxt == self.eos:
                break
            ids.append(nxt)
        return self.decode(ids[1:])


# ───────────────────────── 다시 그리기 ─────────────────────────
_MATH_FONT = None


def math_font(dirs):
    global _MATH_FONT
    if _MATH_FONT:
        return _MATH_FONT
    for d in dirs:
        for root, _, files in os.walk(d):
            for fn in files:
                if fn.lower().startswith("stixtwomath"):
                    _MATH_FONT = os.path.join(root, fn)
                    return _MATH_FONT
    return None


def latex_svg(tex, size, font):
    """LaTeX → (SVG 조각 문자열(경로들), 너비, 높이, 기준선 위 높이). 실패하면 None."""
    try:
        import ziamath as zm
    except Exception:
        return None
    try:
        zm.config.math.mathfont = font
        zm.config.svg2 = False  # <use> 없이 경로를 그대로(다른 앱·PDF 변환과 잘 맞게)
        m = zm.Latex(tex, size=size)
        svg = m.svg()
        w, h = m.getsize()
        return svg, float(w), float(h)
    except Exception:
        return None

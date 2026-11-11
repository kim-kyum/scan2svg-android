#!/usr/bin/env python3
"""scan2svg — 문서 사진을 '편집 가능한' SVG로 재구성하는 프로토타입.

글자는 <text>(글꼴·크기·색이 붙은 진짜 글자), 상자·선·원은 <rect>/<line>/<ellipse>, 손그림 같은 나머지는 베지어 <path>,
사진처럼 벡터로 바꿀 수 없는 부분만 잘라낸 <image>로 남긴다. Inkscape·Illustrator·Figma 등에서 열어 고칠 수 있다.

원리는 전부 "가설을 세우고 → 그려 보고 → 원본과 겹쳐 본다"다.
  1. 용지를 찾아 원근을 펴고, 그림자·색온도를 걷어낸다.
  2. 색 면(팔레트)으로 분할한다. 사진처럼 평탄하지 않은 영역은 이미지 조각으로 남긴다.
  3. 글자를 줄 단위로 쪼개 OCR로 읽고, 줄마다 후보 글꼴로 같은 글을 그려 원본과 겹쳐 본 뒤
     가장 잘 맞는 글꼴·굵기·크기·위치·띄어쓰기를 고른다.
       - 먼저 작게 그려 전체 글꼴을 훑고(1차), 상위 몇 개만 제 크기로 정밀 비교한다(2차)
       - 크기·종류가 비슷한 줄은 묶어서, 대표 줄에서 찾은 글꼴을 나머지 줄에 먼저 대입한다
       - OCR이 미심쩍은 줄은 설정을 바꿔 다시 읽고, 읽은 결과들 중 '그렸을 때 가장 잘 겹치는 것'을 채택한다
       - 겹침이 나쁜 줄은 글자로 바꾸지 않고 원래 모양(경로)으로 둔다. 애매한 곳은 '검토' 레이어에 표시한다
  4. 나머지 연결 요소는 사각형/둥근 사각형/타원/선/격자 가설을 그려 보고
     맞으면 기본 도형으로, 아니면 베지어 곡선 경로로 바꾼다.
  5. 결과를 다시 그려 원본과 비교한다(result_compare.jpg, result_report.json).

필요한 것: Python 3.9+, opencv-python, numpy, pillow, fonttools, pytesseract
          + Tesseract 본체와 언어 데이터(kor, eng). 문서에 쓰인 글꼴이 이 컴퓨터에 깔려 있어야 그 글꼴을 찾는다.
          (Pillow가 Raqm 조판을 지원하면 더 정확하다: python -c "from PIL import features; print(features.check('raqm'))")

사용법:  python scan2svg.py photo.jpg -o result.svg [--fonts 글꼴폴더] [--lang kor+eng] [--debug 폴더]
출력:    result.svg  result_preview.png  result_compare.jpg(원본|재구성|차이)  result_report.json(줄별 글꼴·점수)
SVG 레이어: 종이 / 원본 스캔(숨김, 대조용) / 이미지 조각 / 도형 / 일러스트 / 글자 / 검토 필요(숨김)
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import platform
import sys
import time
from collections import defaultdict
from xml.sax.saxutils import escape, quoteattr

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, features

os.environ.setdefault("OMP_THREAD_LIMIT", "1")  # Tesseract: 작은 그림은 단일 스레드가 더 빠르고, 여러 개를 동시에 돌리기 좋다
VERBOSE = True
REF = 64  # 글꼴 비교용 기준 렌더 크기(px)
REF_S = 32  # 1차(저해상도) 비교용 렌더 크기. 어차피 26px 높이로 줄여 견주므로 작게 그리는 편이 몇 배 빠르다
try:
    import PIL as _PIL
    _SUBPIXEL_TEXT = tuple(int(v) for v in _PIL.__version__.split(".")[:2]) >= (10, 0)
except Exception:
    _SUBPIXEL_TEXT = False
RAQM = features.check("raqm") and not os.environ.get("SCAN2SVG_BASIC_LAYOUT")
# ↑ Pillow가 HarfBuzz 조판(브라우저·Inkscape와 같은 방식)을 쓸 수 있는가. 없으면(윈도우용 Pillow 기본 설치 등) 글자를 하나씩
#   나란히 놓는 단순 조판만 되므로, SVG에도 '커닝·합자·한국어용 대체를 끄라'고 적어서 화면이 우리가 맞춰 본 모양과 같게 한다


# 다른 프로그램(안드로이드 앱 등)에 넣어 쓸 때 바꿔 끼우는 것들
LOG_SINK = None     # 진행 메시지를 받을 함수(문자열 하나). 없으면 stderr로 찍는다
CACHE_DIR = None    # 글꼴 색인 캐시를 둘 폴더. 없으면 ~/.cache/scan2svg
OCR_BACKEND = None  # OCR 엔진. 없으면 Tesseract(pytesseract)를 쓴다.
#   OCR_BACKEND(회색조 uint8 그림, 한_줄인가, lang) -> (단어들, 글자들)
#     단어들: [dict(text, x, y, w, h, conf)]  conf는 0~100
#     글자들: [(글자, 왼쪽 x, 오른쪽 x)]  — 없으면 빈 목록


def log(*a):
    if LOG_SINK is not None:
        LOG_SINK(" ".join(str(x) for x in a))
    elif VERBOSE:
        print(*a, file=sys.stderr, flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# 1. 용지 검출 · 원근 보정 · 조명 평탄화
# ─────────────────────────────────────────────────────────────────────────────
def _order_quad(pts):
    pts = np.asarray(pts, np.float32)
    s, d = pts.sum(1), pts[:, 1] - pts[:, 0]
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], np.float32)


def _intersect(l1, l2):
    (p, d), (q, e) = l1, l2
    A = np.array([[d[0], -e[0]], [d[1], -e[1]]], np.float64)
    t = np.linalg.solve(A, np.asarray(q, np.float64) - np.asarray(p, np.float64))
    return np.asarray(p, np.float64) + t[0] * np.asarray(d, np.float64)


def find_page_quad(img):
    """배경보다 밝은 가장 큰 사각형(용지)의 네 꼭짓점 [TL, TR, BR, BL]. 못 찾으면 None."""
    h, w = img.shape[:2]
    k = 900.0 / max(h, w)
    small = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    L = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2LAB)[..., 0], (0, 0), 2)
    _, th = cv2.threshold(L, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((21, 21), np.uint8))
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((11, 11), np.uint8))
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(c) < 0.2 * th.size:
        return None
    hull = cv2.convexHull(c)
    peri = cv2.arcLength(hull, True)
    quad = None
    for eps in (0.01, 0.02, 0.03, 0.05):
        ap = cv2.approxPolyDP(hull, eps * peri, True)
        if len(ap) == 4:
            quad = _order_quad(ap.reshape(4, 2))
            break
    if quad is None:
        return None
    # 각 변을 윤곽 점들로 다시 직선 적합해서 꼭짓점을 부화소 정밀도로 구한다
    pts = c.reshape(-1, 2).astype(np.float32)
    lines = []
    for i in range(4):
        a, b = quad[i], quad[(i + 1) % 4]
        d = b - a
        n = float(np.linalg.norm(d))
        d = d / n
        rel = pts - a
        t = rel @ d
        dist = np.abs(rel[:, 0] * d[1] - rel[:, 1] * d[0])
        sel = pts[(t > 0.08 * n) & (t < 0.92 * n) & (dist < 6)]
        if len(sel) >= 20:
            vx, vy, x0, y0 = cv2.fitLine(sel, cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
            lines.append((np.array([x0, y0]), np.array([vx, vy])))
        else:
            lines.append((a, d))
    corners = np.array([_intersect(lines[i - 1], lines[i]) for i in range(4)], np.float32) / k
    return _refine_quad(img, corners)


def _refine_quad(img, quad):
    """원본 해상도에서 각 변에 수직인 밝기 단면을 떠 '어두움→밝음' 경계를 찾아 직선을 다시 맞춘다."""
    gray = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (0, 0), 1.5).astype(np.float32)
    ctr = quad.mean(0)
    R = max(12, int(0.012 * max(img.shape[:2])))
    ts = np.arange(-R, R + 1, dtype=np.float32)
    lines = []
    for i in range(4):
        a, b = quad[i], quad[(i + 1) % 4]
        d = (b - a) / np.linalg.norm(b - a)
        nrm = np.array([-d[1], d[0]], np.float32)
        if (ctr - a) @ nrm < 0:
            nrm = -nrm  # 안쪽을 향하게
        base = a[None] + np.linspace(0.08, 0.92, 60, dtype=np.float32)[:, None] * (b - a)[None]
        gx = base[:, None, 0] + ts[None] * nrm[0]
        gy = base[:, None, 1] + ts[None] * nrm[1]
        prof = cv2.remap(gray, gx, gy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        g = prof[:, 2:] - prof[:, :-2]
        j = g.argmax(1)
        ok = g[np.arange(len(j)), j] > 12
        pts = (base + (ts[j + 1])[:, None] * nrm[None])[ok]
        if len(pts) >= 15:
            vx, vy, x0, y0 = cv2.fitLine(pts.astype(np.float32), cv2.DIST_HUBER, 0, 0.01, 0.01).ravel()
            lines.append((np.array([x0, y0]), np.array([vx, vy])))
        else:
            lines.append((a, d))
    return np.array([_intersect(lines[i - 1], lines[i]) for i in range(4)], np.float32)


def rectify(img, quad, max_side=2400):
    """네 꼭짓점을 직사각형으로 편다. A4 비율에 가까우면 A4로 맞춘다."""
    tl, tr, br, bl = quad
    wA = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    hA = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    ratio, page_mm = hA / wA, None
    if abs(ratio / math.sqrt(2) - 1) < 0.08:
        ratio, page_mm = math.sqrt(2), (210.0, 297.0)
    elif abs(ratio * math.sqrt(2) - 1) < 0.08:
        ratio, page_mm = 1 / math.sqrt(2), (297.0, 210.0)
    W = wA
    if max(W, W * ratio) > max_side:
        W = max_side / max(1.0, ratio)
    W, H = int(round(W)), int(round(W * ratio))
    M = cv2.getPerspectiveTransform(np.float32([tl, tr, br, bl]), np.float32([[0, 0], [W, 0], [W, H], [0, H]]))
    shrink = W < wA * 0.9
    return cv2.warpPerspective(img, M, (W, H), flags=cv2.INTER_AREA if shrink else cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE), page_mm


def flatten_illumination(img):
    """그림자·색온도 제거. 타일별 '종이 밝기'를 구해 부드러운 면으로 만들고 나눈다.
    색이 있는 타일(연노랑 상자 등)과 너무 어두운 타일은 종이가 아니라고 보고 건너뛰므로 색 면은 보존된다."""
    h, w = img.shape[:2]
    t = max(12, int(round(w / 80)))
    gh, gw = h // t, w // t
    small = cv2.resize(img[:gh * t, :gw * t], (gw * 6, gh * 6), interpolation=cv2.INTER_AREA)
    tiles = np.percentile(small.reshape(gh, 6, gw, 6, 3).astype(np.float32), 85, axis=(1, 3))
    lum = tiles.mean(2)
    yy, xx = np.mgrid[0:gh, 0:gw].astype(np.float32)
    x, y = xx.ravel() / gw * 2 - 1, yy.ravel() / gh * 2 - 1
    A = np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], 1)
    v, wgt = lum.ravel(), np.ones(gh * gw, np.float32)
    for _ in range(8):  # 위쪽 포락선(=종이)을 향해 가는 강건 적합
        coef, *_ = np.linalg.lstsq(A * wgt[:, None], v * wgt, rcond=None)
        r = v - A @ coef
        s = 1.4826 * np.median(np.abs(r[wgt > 0.5])) + 0.5
        wgt = (r > -2.5 * s).astype(np.float32)
    S0 = (A @ coef).reshape(gh, gw)
    chroma = tiles / np.maximum(lum, 1)[..., None]
    pch = np.median(chroma.reshape(-1, 3)[wgt > 0.5], axis=0)
    dev = np.abs(chroma - pch).max(2)
    paper = ((lum > 0.6 * S0) & (dev < 0.05)).astype(np.float32)
    num = cv2.GaussianBlur(tiles * paper[..., None], (0, 0), 2.5)
    den = cv2.GaussianBlur(paper, (0, 0), 2.5)
    a = np.clip(den / 0.2, 0, 1)[..., None]
    bg = num / np.maximum(den, 1e-3)[..., None] * a + (S0[..., None] * pch) * (1 - a)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_CUBIC)
    out = img.astype(np.float32) / np.maximum(bg, 1) * 255 * 1.03
    return np.clip(out, 0, 255).astype(np.uint8)


# ─────────────────────────────────────────────────────────────────────────────
# 2. 색 면 분할
# ─────────────────────────────────────────────────────────────────────────────
def to_lab(bgr):
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB).astype(np.float32)
    lab[..., 0] *= 100.0 / 255.0
    lab[..., 1:] -= 128.0
    return lab


def lab_to_bgr(c):
    px = np.array([[[c[0] * 255 / 100, c[1] + 128, c[2] + 128]]], np.float32)
    return cv2.cvtColor(np.clip(px, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)[0, 0]


def to_hex(bgr):
    return "#%02x%02x%02x" % (int(bgr[2]), int(bgr[1]), int(bgr[0]))


WHITE = np.array([100.0, 0.0, 0.0], np.float32)


def _find_palette(px, min_count, r_in=6.0, r_out=13.0, sharp=0.5, kmax=24):
    """평탄 픽셀들의 Lab 분포에서 '뾰족한' 봉우리만 팔레트로 뽑는다.
    단색 면은 좁게 뭉치고, 사진·그라디언트는 넓게 퍼지므로 후자는 걸러진다."""
    if len(px) == 0:
        return []
    BIN = 4.0
    dims = (27, 66, 66)
    q = np.floor((px + np.float32([0, 128, 128])) / BIN).astype(np.int64)
    q = np.clip(q, 0, np.array(dims) - 1)
    idx = np.ravel_multi_index(q.T, dims)
    rem = np.ones(len(px), bool)
    pal = []
    for _ in range(80):
        hist = np.bincount(idx[rem], minlength=int(np.prod(dims))).astype(np.float32).reshape(dims)
        for ax in range(3):  # [1,2,1] 평활
            hist = (2 * hist + np.roll(hist, 1, ax) + np.roll(hist, -1, ax)) / 4
        if hist.max() * 3 < min_count:
            break
        c = (np.array(np.unravel_index(hist.argmax(), dims), np.float32) + 0.5) * BIN - np.float32([0, 128, 128])
        for _ in range(3):
            d = np.linalg.norm(px - c, axis=1)
            m = rem & (d < r_in)
            if m.sum() == 0:
                break
            c = px[m].mean(0)
        d = np.linalg.norm(px - c, axis=1)
        n_in, n_out = int((rem & (d < r_in)).sum()), int((rem & (d < r_out)).sum())
        if n_in >= min_count and n_in / max(1, n_out) >= sharp and len(pal) < kmax:
            pal.append(c)
        rem &= d >= r_out
        if rem.sum() < min_count:
            break
    return pal


def segment(flat, u):
    """페이지를 라벨 맵으로 분할. 0=종이, k≥1=팔레트 색. 사진 같은 영역은 별도 마스크로 떼어 낸다.

    두꺼운 면과 가는 획을 따로 다룬다. 면은 안쪽의 균일한 픽셀(씨앗)에서 색을 얻고,
    획은 흐림 때문에 제 색이 안 나오므로 요소마다 '가장 진한 쪽'을 잉크색으로 본다.
    경계의 혼합색 픽셀은 마지막에 양쪽 색 중 가까운 쪽(=50% 경계)으로 보낸다.
    """
    den = cv2.medianBlur(flat, 3)
    lab = to_lab(den)
    dW = np.linalg.norm(lab - WHITE, axis=2)
    fg = dW > 9
    b = max(4, int(round(10 * u)))
    fg[:b] = fg[-b:] = False
    fg[:, :b] = fg[:, -b:] = False
    H, W = fg.shape
    k5 = np.ones((5, 5), np.uint8)
    ke = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    min_count = max(60, int(120 * u * u))

    # (a) 두꺼운 면의 안쪽(씨앗): 5x5 이웃이 균일한 픽셀
    rng = (cv2.dilate(den, k5).astype(np.int16) - cv2.erode(den, k5)).max(2)
    seeds = fg & (rng < 30)

    def palette_of(mask):
        spx = lab[mask]
        sub = spx[:: max(1, len(spx) // 400000)]
        pal = _find_palette(sub, max(20, int(min_count * len(sub) / max(1, len(spx)))))
        lbl = np.zeros((H, W), np.int32)
        unexp = np.zeros((H, W), bool)
        ys, xs = np.nonzero(mask)
        if len(ys) and pal:
            D = np.stack([np.linalg.norm(spx - c, axis=1) for c in pal], 1)
            ok = D.min(1) < 16
            lbl[ys[ok], xs[ok]] = D.argmin(1)[ok] + 1
            unexp[ys[~ok], xs[~ok]] = True
        elif len(ys):
            unexp[ys, xs] = True
        return pal, lbl, unexp

    # (b) 사진 영역: 평탄한데 어느 '뾰족한' 색으로도 설명되지 않는 픽셀이 넓게 뭉친 곳
    _, _, unexplained = palette_of(seeds)
    photo = np.zeros((H, W), np.uint8)
    n, cc, st, _ = cv2.connectedComponentsWithStats(cv2.morphologyEx(unexplained.astype(np.uint8), cv2.MORPH_CLOSE, ke), 8)
    for i in range(1, n):
        if st[i][4] >= (40 * u) ** 2:
            photo[cc == i] = 1
    photos = []
    if photo.any():  # 사진이 꽤(15%↑) 섞인 덩어리는 통째로 사진으로 본다. 단색 그림은 거의 100% 팔레트로 설명되므로
        # 이만큼 설명이 안 되면 사진·그라디언트다(사진 안의 밋밋한 부분이 색 면으로 잘못 떨어져 나가는 것을 막는다)
        n, cc, st, _ = cv2.connectedComponentsWithStats((fg | (photo > 0)).astype(np.uint8), 8)
        for i in np.unique(cc[photo > 0]):
            x, y, w, h, area = st[i]
            m = cc[y:y + h, x:x + w] == i
            if i == 0 or (photo[y:y + h, x:x + w][m] > 0).mean() < 0.15:
                m = m & (photo[y:y + h, x:x + w] > 0)
            photo[y:y + h, x:x + w][m] = 1
        cn, _ = cv2.findContours(photo, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(photo, cn, -1, 1, cv2.FILLED)
        photo = cv2.dilate(photo, np.ones((3, 3), np.uint8))
        photos = [cv2.boundingRect(c) for c in cn if cv2.contourArea(c) >= (40 * u) ** 2]
        photos = [(max(0, x - 1), max(0, y - 1), min(W, x + w + 1) - max(0, x - 1), min(H, y + h + 1) - max(0, y - 1))
                  for x, y, w, h in photos]
    pm = photo.astype(bool)
    fg &= ~pm

    # (a') 사진을 뺀 나머지로 팔레트를 다시 뽑는다(사진 속 색이 팔레트를 오염시키지 않게)
    pal, labels, unexplained = palette_of(seeds & ~pm)
    n_thick = len(pal)  # 여기까지는 두꺼운 면에서 얻은 색, 이후 추가분은 가는 획에서 추정한 색
    local = np.zeros((H, W, 3), np.float32)  # 라벨이 붙은 픽셀의 '그 자리 색'
    if pal:
        sm = labels > 0
        local[sm] = np.array(pal, np.float32)[labels[sm] - 1]

    def neighbors(ry, rx, r=7):
        """(ry, rx) 픽셀마다 r x r 안에 있는 라벨 k와 그 라벨의 그 자리 평균색을 차례로 내준다."""
        for k in range(1, len(pal) + 1):
            mk = (labels == k).astype(np.float32)
            if not mk.any():
                continue
            cnt = cv2.boxFilter(mk, -1, (r, r), normalize=False)[ry, rx]
            idx = np.nonzero(cnt > 0.5)[0]
            if len(idx):
                col = cv2.boxFilter(local * mk[..., None], -1, (r, r), normalize=False)[ry[idx], rx[idx]]
                yield k, idx, col / cnt[idx, None]

    # (c) 면 가장자리의 번짐: '이웃 면의 색과 종이색을 섞은 색'으로 설명되는 픽셀은 섞인 비율 50%를 기준으로 면/종이에 붙인다.
    #     그렇게 설명되지 않는 픽셀(노란 면 옆의 검은 외곽선 등)만 다음 단계의 '획' 후보로 남는다.
    fringe = np.zeros((H, W), bool)
    it = 0
    while pal and it < 4:  # 7x7로 세 번까지, 마지막 한 번은 넓게(굵은 흰 글자 속 좁은 틈처럼 면에서 조금 떨어진 번짐까지)
        r = 7 if it < 3 else 17
        ry, rx = np.nonzero(fg & (labels == 0) & ~unexplained & ~fringe)
        if not len(ry):
            break
        px = lab[ry, rx] - WHITE
        best_r = np.full(len(ry), 12.0, np.float32)
        best_k, best_t = np.zeros(len(ry), np.int32), np.zeros(len(ry), np.float32)
        for k, idx, col in neighbors(ry, rx, r):
            v = col - WHITE
            t = (px[idx] * v).sum(1) / np.maximum((v * v).sum(1), 1e-6)
            resid = np.linalg.norm(px[idx] - np.clip(t, 0, 1)[:, None] * v, axis=1)
            upd = resid < best_r[idx]
            best_r[idx[upd]], best_k[idx[upd]], best_t[idx[upd]] = resid[upd], k, t[upd]
        on, off = (best_k > 0) & (best_t >= 0.5), (best_k > 0) & (best_t < 0.5)
        changed = bool(on.any() or off.any())
        if changed:
            labels[ry[on], rx[on]] = best_k[on]
            local[ry[on], rx[on]] = np.array(pal, np.float32)[best_k[on] - 1]
            fringe[ry[off], rx[off]] = True
        it = it + 1 if changed else (4 if r == 17 else 3)  # 더 바뀔 게 없으면 바로 넓은 반경으로
    fg &= ~fringe

    # (d) 가는 구조(글자 획·선): 요소마다 '바탕에서 가장 먼 색'을 잉크색으로 본다.
    #     바탕은 요소를 둘러싼 것 — 보통은 종이지만, 색 면 위에 놓인 글자(남색 띠 위의 노란 글자)는 그 면이 바탕이다
    thin = fg & (labels == 0) & ~unexplained
    n, cc, st, _ = cv2.connectedComponentsWithStats(thin.astype(np.uint8), 8)
    comp_ink, comp_w = np.zeros((n, 3), np.float32), np.zeros(n, np.float32)
    comp_bg, comp_kb = np.tile(WHITE, (n, 1)), np.zeros(n, np.int32)
    pal_arr = np.array(pal, np.float32).reshape(-1, 3)
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if area < 4:
            continue
        X0, Y0, X1, Y1 = max(0, x - 2), max(0, y - 2), min(W, x + w + 2), min(H, y + h + 2)
        m = (cc[Y0:Y1, X0:X1] == i).astype(np.uint8)
        ring = labels[Y0:Y1, X0:X1][cv2.dilate(m, k5) > m]
        if len(ring) and len(pal_arr):
            kb = int(np.bincount(ring, minlength=len(pal_arr) + 1).argmax())
            if kb > 0 and (ring == kb).mean() > 0.6:
                comp_bg[i], comp_kb[i] = pal_arr[kb - 1], kb
        px = lab[Y0:Y1, X0:X1][m > 0]
        d = np.linalg.norm(px - comp_bg[i], axis=1)
        comp_ink[i] = np.median(px[d >= np.percentile(d, 70)], axis=0)
        comp_w[i] = area

    def cdist(a, c, bg, wa=0.6):
        """색 차이. 단, '잉크 c가 바탕 bg 쪽으로 섞이는 방향'의 차이는 wa배로 덜 센다(가는 획은 흐려져 그 방향으로 끌려간다).
        흰 종이 위의 검은 글자라면 그 방향이 곧 밝기 축이다."""
        v = c - bg
        uhat = v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-6)
        d = a - c
        along = (d * uhat).sum(-1, keepdims=True)
        return np.sqrt((wa * along[..., 0]) ** 2 + ((d - along * uhat) ** 2).sum(-1))

    comp_label = np.zeros(n, np.int32)
    todo = comp_w > 0
    far = np.linalg.norm(comp_ink - comp_bg, axis=1)  # 바탕에서 얼마나 먼가(=얼마나 진한가)

    noise = todo & (comp_kb > 0) & (far < 25)  # 색 면 안의, 면과 거의 같은 색 조각 = 압축 잡음·얼룩 → 면의 일부
    comp_label[noise] = comp_kb[noise]
    todo &= ~noise
    if pal:
        # 같은 잉크라면 팔레트 색이 관측색보다 바탕에서 더(적어도 비슷하게) 멀어야 한다. 이 조건이 없으면
        # 회색 글자가 근처의 더 옅은 분홍 면과 같은 색으로 묶인다. 후보가 여럿이면 '섞이는 방향에서 벗어난 정도'가
        # 작은 쪽을 고른다(흐려진 검은 글자는 무채색 그대로다 → 남색 면이 아니라 검정)
        P = np.array(pal, np.float32)
        ok = np.stack([(np.linalg.norm(c - comp_bg, axis=1) >= far - 6) & (cdist(comp_ink, c, comp_bg) < 24) for c in P], 1)
        rank = np.where(ok, np.stack([cdist(comp_ink, c, comp_bg, 0.25) for c in P], 1), 1e9)
        hit = todo & ok.any(1)
        comp_label[hit] = rank.argmin(1)[hit] + 1
        todo &= ~hit
    while todo.any():
        i = int(np.argmax(np.where(todo, comp_w, 0)))
        if comp_w[todo].sum() < 0.5 * min_count and pal:
            D = np.stack([cdist(comp_ink, c, comp_bg) for c in np.array(pal, np.float32)], 1)
            comp_label[todo] = D.argmin(1)[todo] + 1
            break
        near = todo & (cdist(comp_ink, comp_ink[i], comp_bg) < 24)
        # 흐려진 가는 획보다 실제 잉크가 더 진하다: 군집에서 진한(바탕에서 먼) 쪽 대표값
        dark = near & (far >= (far[near] * comp_w[near]).sum() / comp_w[near].sum())
        c = (comp_ink[dark] * comp_w[dark, None]).sum(0) / comp_w[dark].sum()
        pal.append(c)
        comp_label[near] = len(pal)
        todo &= ~near
    ty, tx = np.nonzero(thin)
    ci = cc[ty, tx]
    ink, px = comp_ink[ci], lab[ty, tx]
    core = (comp_label[ci] > 0) & (np.linalg.norm(px - ink, axis=1) < 0.6 * np.linalg.norm(px - comp_bg[ci], axis=1))
    labels[ty[core], tx[core]] = comp_label[ci][core]  # 확실한 잉크(약 60% 이상)만 먼저, 나머지는 (e)에서
    local[ty[core], tx[core]] = ink[core]

    # (e) 남은 혼합색 픽셀: 7x7 안에 있는 라벨들의 '그 자리 색'과 종이 중 가장 가까운 쪽으로(=50% 경계).
    #     몇 번 반복해 좁은 틈(별 꼭지 안쪽 등)까지 이웃 색이 번져 들어가게 한다
    for _ in range(3 if pal else 0):
        ry, rx = np.nonzero(fg & (labels == 0))
        if not len(ry):
            break
        px = lab[ry, rx]
        best_d = np.linalg.norm(px - WHITE, axis=1)
        best = np.zeros(len(ry), np.int32)
        for k, idx, col in neighbors(ry, rx):
            d = np.linalg.norm(px[idx] - col, axis=1)
            upd = d < best_d[idx]
            best[idx[upd]], best_d[idx[upd]] = k, d[upd]
        got = best > 0
        if not got.any():
            break
        labels[ry[got], rx[got]] = best[got]
        local[ry[got], rx[got]] = np.array(pal, np.float32)[best[got] - 1]
    # 최종 색이 같은 항목(예: 두꺼운 검정과, 가는 획에서 추정한 '거의 검정')은 한 라벨로 합친다
    palette = np.array(pal, np.float32).reshape(-1, 3)
    bgr = [snap_color(c, i >= n_thick) for i, c in enumerate(palette)]
    remap, keep = np.zeros(len(pal) + 1, np.int32), []
    for i, c in enumerate(bgr):
        j = next((jj for jj, kk in enumerate(keep) if np.abs(bgr[kk].astype(int) - c.astype(int)).max() <= 6), None)
        if j is None:
            keep.append(i)
            j = len(keep) - 1
        remap[i + 1] = j + 1
    labels = remap[labels]
    thick = np.array([k < n_thick for k in keep], bool)
    return labels, palette[keep], [bgr[k] for k in keep], thick, photos, photo, lab


def estimate_blur(lab, labels, palette):
    """스캔의 흐림 정도(가우시안 σ, px)를 두꺼운 색 면의 가장자리에서 잰다.
    흐려진 계단 경계의 최대 기울기는 (색 차이)/(σ·√2π) 이므로 기울기를 재면 σ가 나온다. 잴 곳이 없으면 None."""
    dW = np.linalg.norm(lab - WHITE, axis=2)
    gx, gy = cv2.Sobel(dW, cv2.CV_32F, 1, 0, ksize=1) / 2, cv2.Sobel(dW, cv2.CV_32F, 0, 1, ksize=1) / 2
    g = cv2.dilate(np.hypot(gx, gy), np.ones((3, 3), np.uint8))
    k7, k3, est = np.ones((7, 7), np.uint8), np.ones((3, 3), np.uint8), []
    for k, c in enumerate(palette, 1):
        delta = float(np.linalg.norm(c - WHITE))
        if delta < 40:
            continue
        R = (labels == k).astype(np.uint8)
        T = cv2.dilate(cv2.erode(R, k7), k7) & R  # 폭 7px 이상인 부분만
        edge = (T > 0) & (cv2.dilate((labels == 0).astype(np.uint8), k3) > 0)  # 종이와 맞닿은 경계
        edge &= cv2.dilate(((labels != 0) & (labels != k)).astype(np.uint8), k7) == 0  # 다른 색이 근처에 없는 곳
        v = g[edge]
        if len(v) >= 200:
            est.append((len(v), delta / (np.median(v) * 2.5066)))
    if not est:
        return None
    w = np.array([e[0] for e in est], np.float64)
    return float(np.clip((np.array([e[1] for e in est]) * w).sum() / w.sum(), 0.3, 3.0))


def mark_reversed(labels, n_pal, u):
    """색 면 안에 갇힌 종이색 조각을 별도 라벨(n_pal+1, '흰 잉크')로 바꾼다 → 어두운 바탕의 흰 글자·무늬를 요소로 다룰 수 있다.
    조각을 둘러싼 색 덩어리가 조각보다 훨씬 넓고 두꺼운 '면'일 때만 해당한다(넓은 면 위의 흰 글자).
    테두리만 있는 상자의 안쪽이나 글자 속 빈 곳('ㅇ'의 속)은 둘러싼 것이 가는 획이므로 그대로 종이다."""
    H, W = labels.shape
    n, cc, st, _ = cv2.connectedComponentsWithStats((labels == 0).astype(np.uint8), 4)
    k3, hit, planes = np.ones((3, 3), np.uint8), False, {}
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if area < 6 * u * u or h > 0.09 * H or w > 0.3 * W or x < 2 or y < 2 or x + w > W - 2 or y + h > H - 2:
            continue
        X0, Y0, X1, Y1 = x - 2, y - 2, x + w + 2, y + h + 2
        reg = labels[Y0:Y1, X0:X1]
        isl = (cc[Y0:Y1, X0:X1] == i).astype(np.uint8)
        rm = cv2.dilate(isl, k3) > isl
        ring = reg[rm]
        k = int(np.bincount(ring, minlength=n_pal + 2)[:n_pal + 1].argmax())
        if k == 0 or (ring == k).mean() < 0.8:
            continue
        if k not in planes:
            planes[k] = (*cv2.connectedComponentsWithStats((labels == k).astype(np.uint8), 8)[1:3], {})
        pcc, pst, thick = planes[k]
        j = int(np.bincount(pcc[Y0:Y1, X0:X1][rm & (reg == k)]).argmax())
        if pst[j][4] < max(6 * area, (40 * u) ** 2):
            continue
        if j not in thick:  # 둘러싼 덩어리가 '면'인가(획이 아니라): 안쪽으로 12u 이상 들어갈 수 있어야 한다
            jx, jy, jw, jh = pst[j][:4]
            pm_ = cv2.copyMakeBorder((pcc[jy:jy + jh, jx:jx + jw] == j).astype(np.uint8), 1, 1, 1, 1, cv2.BORDER_CONSTANT)
            thick[j] = float(cv2.distanceTransform(pm_, cv2.DIST_L2, 3).max()) >= 12 * u
        if not thick[j]:
            continue
        reg[isl > 0] = n_pal + 1
        hit = True
    return hit


def snap_color(c, thin=False):
    """거의 검정이면 순수 검정으로. 가는 획은 흐려져 바탕색이 섞이므로 기준을 넓게 잡는다."""
    if c[0] < (34 if thin else 30) and math.hypot(c[1], c[2]) < (24 if thin else 12):
        return np.array([0, 0, 0], np.uint8)
    return lab_to_bgr(c)


def label_components(labels, n_labels, min_area):
    """라벨별 연결 요소. 전역 id 맵과 (label, x, y, w, h, area) 표를 돌려준다."""
    cmap = np.zeros(labels.shape, np.int32)
    rows = [(0, 0, 0, 0, 0, 0)]
    for k in range(1, n_labels + 1):
        n, cc, st, _ = cv2.connectedComponentsWithStats((labels == k).astype(np.uint8), 8)
        base = len(rows) - 1
        keep = st[:, 4] >= min_area
        keep[0] = False
        remap = np.zeros(n, np.int32)
        ids = np.nonzero(keep)[0]
        remap[ids] = base + 1 + np.arange(len(ids))
        m = cc > 0
        cmap[m] = remap[cc[m]]
        for i in ids:
            rows.append((k, *st[i][:5]))
    return cmap, np.array(rows, np.int64)


# ─────────────────────────────────────────────────────────────────────────────
# 3. 글꼴 목록
# ─────────────────────────────────────────────────────────────────────────────
class Face:
    def __init__(self, d):
        self.__dict__.update(d)
        self._cache, self._cmap, self._adv, self._gs = {}, None, {}, None

    def adv(self, ch):
        """글자 하나의 폭(em 단위, 반올림 없는 값). 아주 크게 놓고 재서 픽셀 반올림 오차를 없앤다."""
        a = self._adv.get(ch)
        if a is None:
            a = self._var_adv(ch) if self.var else None
            if a is None:
                a = self.pil(1000.0).getlength(ch) / 1000.0
            self._adv[ch] = a
        return a

    def _var_adv(self, ch):
        """가변 글꼴의 글자 폭은 글꼴 파일의 표에서 직접 계산한다.
        옛 FreeType(안드로이드용 Pillow의 2.9.1)은 일부 가변 글꼴의 폭을 0에 가깝게 돌려줘서 글자가 한자리에 겹친다."""
        if self._gs is None:
            try:
                from fontTools.ttLib import TTFont
                tt = TTFont(self.path, fontNumber=self.index, lazy=True)
                axes = [a.axisTag for a in tt["fvar"].axes]
                self._gs = (tt.getGlyphSet(location=dict(zip(axes, self.var))), tt.getBestCmap(), float(tt["head"].unitsPerEm), tt)
            except Exception:
                self._gs = False
        if not self._gs:
            return None
        try:
            name = self._gs[1].get(ord(ch))
            return float(self._gs[0][name].width) / self._gs[2] if name else None
        except Exception:
            return None

    def supports(self, ch):
        """이 글꼴에 그 글자가 들어 있는가."""
        if self._cmap is None:
            from fontTools.ttLib import TTFont
            try:
                tt = TTFont(self.path, fontNumber=self.index, lazy=True)
                try:
                    self._cmap = set(tt.getBestCmap())
                finally:
                    tt.close()
            except Exception:
                self._cmap = set()
        return ord(ch) in self._cmap

    def pil(self, size=REF):
        key = round(float(size), 2)
        f = self._cache.get(key)
        if f is None:
            lay = getattr(ImageFont, "Layout", None)  # Pillow 9.1 이전에는 상수 이름이 달랐다
            eng = (lay.RAQM if RAQM else lay.BASIC) if lay else (ImageFont.LAYOUT_RAQM if RAQM else ImageFont.LAYOUT_BASIC)
            try:
                f = ImageFont.truetype(self.path, key, index=self.index, layout_engine=eng)
            except TypeError:  # Pillow 10.1 이전에는 글자 크기가 정수여야 했다 → 가장 가까운 정수 크기로 그린다
                f = ImageFont.truetype(self.path, max(1, int(round(key))), index=self.index, layout_engine=eng)
            if self.var:
                try:
                    f.set_variation_by_axes(self.var)
                except Exception:  # 가변 글꼴을 못 다루는 FreeType: 기본 굵기로 그려진다
                    pass
            f.face, f.px = self, key  # px: 조판에 쓰는 크기(소수 포함)
            if len(self._cache) > 9:
                self._cache.pop(next(k for k in self._cache if k not in (float(REF), float(REF_S), 1000.0)))
            self._cache[key] = f
        return f

    @property
    def label(self):
        return f"{self.family} {self.style}".strip()


def default_font_dirs():
    home = os.path.expanduser("~")
    s = platform.system()
    if s == "Windows":
        return [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts")]
    if s == "Darwin":
        return ["/System/Library/Fonts", "/Library/Fonts", home + "/Library/Fonts"]
    return ["/usr/share/fonts", "/usr/local/share/fonts", home + "/.fonts", home + "/.local/share/fonts",
            "/usr/share/texmf/fonts/opentype", "/system/fonts", "/product/fonts", "/system_ext/fonts"]  # 뒤 셋은 안드로이드


def _name(tt, ids):
    nm = tt["name"]
    for i in ids:
        for plat, enc, lang in ((3, 1, 0x409), (1, 0, 0), (3, 10, 0x409)):
            r = nm.getName(i, plat, enc, lang)
            if r:
                return str(r)
    for i in ids:
        r = nm.getDebugName(i)
        if r:
            return r
    return ""


def _scan_font_file(path):
    from fontTools.ttLib import TTCollection, TTFont
    out = []
    n = 1
    if path.lower().endswith((".ttc", ".otc")):
        col = TTCollection(path, lazy=True)
        n = len(col.fonts)
        col.close()
    for i in range(n):
        tt = TTFont(path, fontNumber=i, lazy=True)
        try:
            out += _scan_face(tt, path, i)
        finally:
            tt.close()
    return out


def _scan_face(tt, path, i):
    out = []
    cmap = tt.getBestCmap() or {}
    scripts = [s for s, probe in (("latin", "AaZz09"), ("hangul", "가한힣"), ("kana", "あア"), ("han", "漢字"))
               if all(ord(ch) in cmap for ch in probe)]
    if not scripts:
        return out
    os2 = tt["OS/2"] if "OS/2" in tt else None
    base = dict(path=path, index=i, family=_name(tt, (16, 1)), style=_name(tt, (17, 2)),
                weight=int(os2.usWeightClass) if os2 else 400,
                italic=bool(os2 and os2.fsSelection & 1) or bool(tt["post"].italicAngle),
                psname=_name(tt, (6,)), scripts=scripts, var=None)
    if "fvar" in tt:  # 가변 글꼴: 이름 붙은 인스턴스마다 후보로
        axes = [a.axisTag for a in tt["fvar"].axes]
        for inst in tt["fvar"].instances:
            d = dict(base)
            d["style"] = tt["name"].getDebugName(inst.subfamilyNameID) or base["style"]
            d["var"] = [float(inst.coordinates[a]) for a in axes]
            if "wght" in inst.coordinates:
                d["weight"] = int(round(inst.coordinates["wght"]))
            out.append(d)
    else:
        out.append(base)
    return out


def load_fonts(dirs, cache_path=None):
    """글꼴 폴더를 훑어 후보 목록을 만든다(글꼴 파일별 결과는 캐시)."""
    cache_path = cache_path or os.path.join(CACHE_DIR or os.path.join(os.path.expanduser("~"), ".cache", "scan2svg"), "fonts.json")
    try:
        with open(cache_path, encoding="utf-8") as fh:
            cache = json.load(fh)
    except Exception:
        cache = {}
    faces, dirty = [], False
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for fn in sorted(files):
                if not fn.lower().endswith((".ttf", ".otf", ".ttc", ".otc")):
                    continue
                p = os.path.join(root, fn)
                key = f"{p}|{os.path.getmtime(p):.0f}|{os.path.getsize(p)}"
                if key not in cache:
                    try:
                        cache[key] = _scan_font_file(p)
                    except Exception as e:  # 깨진 글꼴은 건너뜀
                        cache[key] = []
                    dirty = True
                faces += cache[key]
    if dirty:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, ensure_ascii=False)
        except Exception:
            pass
    seen, out = set(), []
    for f in faces:
        k = (f["family"], f["style"], f["weight"], f["italic"])
        if k not in seen and f["family"]:
            seen.add(k)
            out.append(Face(f))
    return out


def lang_of(text):
    """줄의 언어 태그. 글꼴에 따라 언어별로 조판이 달라진다(예: Noto Sans CJK KR은 한국어일 때 띄어쓰기 폭이 넓어진다).
    SVG에 같은 태그(lang)를 적고 여기서도 같은 태그로 그려야, 우리가 맞춰 본 모양과 실제 화면이 일치한다."""
    for ch in text:
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
            return "ko"
        if 0x3040 <= o <= 0x30FF:
            return "ja"
    return None


# 한국어 문서의 조판 방식은 두 가지가 섞여 있다. 본고딕·본명조(Noto CJK KR) 같은 글꼴은 '한국어용 글리프 대체' 기능
# (OpenType locl·calt)을 갖고 있어서, 이를 쓰는 프로그램(웹 브라우저 등)에서는 쌍점·괄호·쉼표의 모양과 자리,
# 띄어쓰기 폭이 조금 달라진다. 쓰지 않는 프로그램에서 만든 문서도 많다. 어느 쪽인지는 문서 전체를 보고 정하고
# (decide_locl), SVG에도 같은 설정(font-feature-settings)을 적어서 우리가 맞춰 본 모양과 실제 화면이 같게 한다.
KO_LOCL = True


def _kw(lang):
    if not lang or not RAQM:
        return {}
    return {"language": lang} if KO_LOCL or lang != "ko" else {"language": lang, "features": ["-locl", "-calt"]}


def _is_hangul(ch):
    return 0xAC00 <= ord(ch) <= 0xD7A3


# 글자를 놓는 방법은 두 가지다.
#  - Raqm이 있으면 Pillow(HarfBuzz)에 맡긴다: 커닝·합자·언어별 대체까지 브라우저와 같다.
#  - 없으면 '단순 조판'을 직접 한다: 글자를 하나씩, 글꼴에 적힌 폭만큼(반올림 없이) 옮겨 가며 놓는다.
#    Pillow의 기본 조판은 글자 폭을 정수 픽셀로 반올림해서 줄이 길수록 어긋나므로 쓰지 않는다.
#    SVG에도 커닝·합자를 끄라고 적어 두므로(svg_text) 브라우저가 그린 결과가 이 단순 조판과 일치한다.
def t_len(f, text, lang=None):
    """글자열의 폭(px)."""
    if RAQM:
        return f.getlength(text, **_kw(lang))
    return f.px * sum(f.face.adv(ch) for ch in text)


def t_draw(d, xy, text, f, fill=255, lang=None):
    """글자열을 그린다. xy = 첫 글자의 원점(왼쪽·기준선)."""
    if RAQM:
        d.text(xy, text, font=f, fill=fill, anchor="ls", **_kw(lang))
        return
    x = xy[0]
    for ch in text:
        if not ch.isspace():  # 예전 Pillow는 소수 좌표를 버림하므로 미리 반올림해 준다
            d.text((x, xy[1]) if _SUBPIXEL_TEXT else (int(round(x)), int(round(xy[1]))), ch, font=f, fill=fill, anchor="ls")
        x += f.px * f.face.adv(ch)


def t_bbox(f, text, lang=None):
    """글자열이 그려질 범위 (왼쪽, 위, 오른쪽, 아래) — 원점 기준. 그릴 도화지 크기를 정하는 데 쓴다."""
    if RAQM:
        return f.getbbox(text, anchor="ls", **_kw(lang))
    asc, desc = f.getmetrics()
    return -0.25 * f.px, -asc - 0.15 * f.px, t_len(f, text) + 0.35 * f.px, desc + 0.15 * f.px


def space_adv(f, a, b, lang=None):
    """글자 a와 b 사이에 놓인 공백 하나의 폭. 앞뒤 글자·언어에 따라 달라지는 글꼴이 있어 통째로 재서 뺀다."""
    return t_len(f, a + " " + b, lang) - t_len(f, a + b, lang)


def word_starts(f, words, lang=None):
    """각 단어가 시작하는 x(글자 원점 기준). 공백 폭이 앞뒤 글자·언어에 따라 달라지는 글꼴이 있어 통째로 잰다."""
    out, s = [], ""
    for w in words:
        s = w if not s else s + " " + w
        out.append(t_len(f, s, lang) - t_len(f, w, lang))
    return out


def scripts_of(text):
    need = set()
    for ch in text:
        o = ord(ch)
        if 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3130 <= o <= 0x318F:
            need.add("hangul")
        elif 0x3040 <= o <= 0x30FF:
            need.add("kana")
        elif 0x4E00 <= o <= 0x9FFF:
            need.add("han")
        elif ch.isalnum() and o < 0x250:
            need.add("latin")
    return need


# ─────────────────────────────────────────────────────────────────────────────
# 4. OCR + 줄 단위 글꼴 매칭
# ─────────────────────────────────────────────────────────────────────────────
def find_text_lines(cmap, comps, cand, H, W, u):
    """글자 후보 요소들을 줄로 묶는다. 각 요소를 좌우로 넉넉히, 위아래로 조금 부풀려 서로 닿으면 같은 줄."""
    ids = np.nonzero(cand)[0]
    for _ in range(3):
        grow = np.zeros((H, W), np.uint8)
        for i in ids:
            _, x, y, w, h, _ = comps[i]
            s = max(w, h, 8 * u)
            cv2.rectangle(grow, (int(x - 0.55 * s), int(y - 0.10 * s)), (int(x + w + 0.55 * s), int(y + h + 0.10 * s)), 1, -1)
        _, cc = cv2.connectedComponents(grow, 8)
        groups = defaultdict(list)
        for i in ids:
            _, x, y, w, h, _ = comps[i]
            groups[int(cc[min(H - 1, y + h // 2), min(W - 1, x + w // 2)])].append(i)
        # 글자들 사이에 낀 훨씬 큰 요소(옆의 그림 조각, 물결선)는 글자가 아니다. 이런 요소는 넓게 부풀어 서로 무관한 것들을
        # 한 줄로 이어 버리므로, 빼고 다시 묶는다
        drop = []
        for g in groups.values():
            if len(g) >= 4:
                g = np.array(g)
                hm = float(np.median(comps[g, 4]))
                drop += list(g[(np.maximum(comps[g, 3], comps[g, 4]) > 3.0 * hm) & (comps[g, 4] > 1.6 * hm)])
        if not drop:
            break
        ids = np.setdiff1d(ids, drop)
    lines = []
    for g in groups.values():
        g = np.array(g)
        y0, y1 = comps[g, 2].min(), (comps[g, 2] + comps[g, 4]).max()
        x0, x1 = comps[g, 1].min(), (comps[g, 1] + comps[g, 3]).max()
        parts = [g]
        if len(g) >= 6 and (x1 - x0) > 3 * (y1 - y0) / 2:  # 두 줄이 붙었으면 빈 가로띠에서 나눈다
            rows = np.isin(cmap[y0:y1, x0:x1], g).any(1)
            edges = np.flatnonzero(np.diff(np.concatenate([[0], rows.astype(np.int8), [0]])))
            bands = [(a, b) for a, b in zip(edges[::2], edges[1::2])]
            tall = max(b - a for a, b in bands)
            major = [bd for bd in bands if bd[1] - bd[0] >= 0.55 * tall]
            if len(major) >= 2:
                mid = np.array([(a + b) / 2 for a, b in major])
                cyc = comps[g, 2] + comps[g, 4] / 2 - y0
                near = np.abs(cyc[:, None] - mid[None]).argmin(1)
                parts = [g[near == j] for j in range(len(major)) if (near == j).any()]
        for part in parts:
            py0, py1 = comps[part, 2].min(), (comps[part, 2] + comps[part, 4]).max()
            px0, px1 = comps[part, 1].min(), (comps[part, 1] + comps[part, 3]).max()
            if py1 - py0 >= 10 * u and px1 - px0 >= 6 * u:
                lines.append(dict(ids=part, box=(int(px0), int(py0), int(px1), int(py1))))
    # 같은 높이대에 나란히 놓인 묶음은 한 줄로 합친다(고정폭 글꼴처럼 띄어쓰기가 넓은 줄). 더 합칠 게 없을 때까지 반복
    changed = True
    while changed and len(lines) > 1:
        changed = False
        lines.sort(key=lambda l: l["box"][0])
        for a in range(len(lines)):
            if lines[a] is None:
                continue
            for b in range(a + 1, len(lines)):
                if lines[b] is None:
                    continue
                ax0, ay0, ax1, ay1 = lines[a]["box"]
                bx0, by0, bx1, by1 = lines[b]["box"]
                ha, hb = ay1 - ay0, by1 - by0
                if bx0 - ax1 > 0.2 * W:
                    break
                ov = min(ay1, by1) - max(ay0, by0)
                if bx0 - ax1 <= 1.2 * max(ha, hb) and ov >= 0.6 * min(ha, hb) and 0.45 < ha / hb < 2.2:
                    lines[a] = dict(ids=np.concatenate([lines[a]["ids"], lines[b]["ids"]]),
                                    box=(min(ax0, bx0), min(ay0, by0), max(ax1, bx1), max(ay1, by1)))
                    lines[b] = None
                    changed = True
        lines = [l for l in lines if l is not None]
    # 줄에 끼지 못한 작은 조각(i의 점, 악센트, 쉼표, 따옴표)을 줄 상자 바로 위·아래에서 찾아 붙인다
    used = np.zeros(len(comps), bool)
    for l in lines:
        used[l["ids"]] = True
    for i in ids:
        if used[i]:
            continue
        _, x, y, w, h, _ = comps[i]
        cx, cy, best = x + w / 2, y + h / 2, None
        for li, l in enumerate(lines):
            x0, y0, x1, y1 = l["box"]
            lh = y1 - y0
            if max(w, h) > 0.5 * lh or not (x0 - 0.3 * lh < cx < x1 + 0.5 * lh):
                continue
            above, below = y0 - cy, cy - y1  # 줄 위쪽은 넉넉히(점·악센트), 아래쪽은 조금만(쉼표)
            if above <= 0.4 * lh and below <= 0.25 * lh:
                d = max(above, below, 0) + (0.1 * lh if below > 0 else 0)
                if best is None or d < best[0]:
                    best = (d, li)
        if best:
            l = lines[best[1]]
            x0, y0, x1, y1 = l["box"]
            l["ids"] = np.append(l["ids"], i)
            l["box"] = (int(min(x0, x)), int(min(y0, y)), int(max(x1, x + w)), int(max(y1, y + h)))
    lines.sort(key=lambda l: ((l["box"][1] + l["box"][3]) / 2, l["box"][0]))
    return lines


def _is_cjk(ch):
    o = ord(ch)
    return 0xAC00 <= o <= 0xD7A3 or 0x3040 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF or 0x3130 <= o <= 0x318F


def _ocr(img, single_line, lang):
    """OCR 한 번. 돌려주는 값: (단어들 [dict(text, x, y, w, h, conf 0~100)], 글자들 [(글자, 왼쪽 x, 오른쪽 x)])"""
    if OCR_BACKEND is not None:
        return OCR_BACKEND(img, single_line, lang)
    if single_line:
        return _tess(img, lang, 7)
    import pytesseract
    d = pytesseract.image_to_data(Image.fromarray(img), lang=lang, config="--psm 6", output_type=pytesseract.Output.DICT)
    return [dict(text=t.strip(), x=float(d["left"][i]), y=float(d["top"][i]), w=float(d["width"][i]), h=float(d["height"][i]),
                 conf=float(d["conf"][i])) for i, t in enumerate(d["text"]) if t.strip() and float(d["conf"][i]) >= 0], []


def ocr_rows(softs, lang, target=48):
    """줄 그림(잉크 0..1)들을 같은 글자 높이로 맞춰 세로로 쌓고 OCR을 한 번만 부른다.
    줄 찾기를 OCR 엔진에 맡기지 않으므로 줄 앞머리가 잘리는 일이 줄고, 글자 크기가 달라도 인식이 고르다."""
    rows, y, gap, mx = [], 40, 44, 40
    for sft in softs:
        k = target / sft.shape[0]
        im = _resize(sft, k) if abs(k - 1) > 0.15 else sft
        k = im.shape[0] / sft.shape[0]
        rows.append((y, im, k))
        y += im.shape[0] + gap
    sheet = np.full((y, max(r[1].shape[1] for r in rows) + 2 * mx), 255, np.uint8)
    for ry, im, _ in rows:
        sheet[ry:ry + im.shape[0], mx:mx + im.shape[1]] = (255 * (1 - im)).astype(np.uint8)
    tops = np.array([r[0] for r in rows])
    out = [[] for _ in rows]
    for w in _ocr(sheet, False, lang)[0]:
        cyw = w["y"] + w["h"] / 2
        j = int(np.abs(tops + target / 2 - cyw).argmin())
        if abs(tops[j] + rows[j][1].shape[0] / 2 - cyw) > rows[j][1].shape[0]:
            continue
        k = rows[j][2]
        out[j].append(dict(text=w["text"], x=(w["x"] - mx) / k, w=w["w"] / k, conf=w["conf"]))
    return [merge_words(ws, softs[j]) for j, ws in enumerate(out)], sheet


def merge_words(ws, soft):
    """Tesseract는 한글 음절 사이를 자주 띄운다. 두 단어 경계 근처의 실제 빈칸 폭이 좁으면 붙인다."""
    ws = sorted(ws, key=lambda w: w["x"])
    h = soft.shape[0]
    empty = soft.max(0) < 0.25  # 잉크가 없는 세로줄
    merged = []
    for w in ws:
        if merged:
            m = merged[-1]
            latin = not _is_cjk(m["text"][-1]) and not _is_cjk(w["text"][0])
            a, b = int(max(0, m["x"] + 0.5 * m["w"])), int(min(len(empty), w["x"] + 0.5 * w["w"]))
            run = best = 0
            for e in empty[a:b]:
                run = run + 1 if e else 0
                best = max(best, run)
            if not latin and best < 0.2 * h:
                m["text"] += w["text"]
                m["w"] = w["x"] + w["w"] - m["x"]
                m["conf"] = min(m["conf"], w["conf"])
                continue
        merged.append(dict(w))
    return merged


def _tess(img, lang, psm):
    """Tesseract를 한 번 실행해 단어(신뢰도 포함)와 글자별 상자를 함께 얻는다."""
    import subprocess
    import tempfile
    import pytesseract
    with tempfile.TemporaryDirectory() as td:
        ip, op = os.path.join(td, "l.png"), os.path.join(td, "o")
        Image.fromarray(img).save(ip)
        subprocess.run([pytesseract.pytesseract.tesseract_cmd, ip, op, "-l", lang, "--psm", str(psm), "tsv", "makebox"],
                       capture_output=True, timeout=120, check=True)
        words, chars = [], []
        for ln in open(op + ".tsv", encoding="utf-8").read().splitlines()[1:]:
            c = ln.split("\t")
            if len(c) >= 12 and c[11].strip() and float(c[10]) >= 0:
                words.append(dict(text=c[11].strip(), x=float(c[6]), y=float(c[7]), w=float(c[8]), h=float(c[9]), conf=float(c[10])))
        if os.path.exists(op + ".box"):
            for ln in open(op + ".box", encoding="utf-8").read().splitlines():
                c = ln.rsplit(" ", 5)
                if len(c) == 6 and c[0].strip():
                    chars.append((c[0], float(c[1]), float(c[3])))
    return words, chars


def ocr_variants(softs, lang):
    """줄마다 따로, 설정(언어 조합·확대 배율)을 바꿔 가며 다시 읽는다.
    어느 설정이 맞을지는 줄마다 다르다(한글 줄은 kor만 쓸 때, 영문 줄은 eng만 쓸 때 더 잘 읽히곤 한다).
    그래서 여기서는 후보만 모으고, 어느 것이 맞는지는 나중에 '그려 보고' 정한다.
    줄별로 [dict(words=단어 목록, conf=평균 신뢰도, chars=[(글자, 왼쪽 x, 오른쪽 x)]), ...]"""
    from concurrent.futures import ThreadPoolExecutor
    parts = lang.split("+")
    if OCR_BACKEND is not None:  # 언어 설정이 따로 없는 엔진: 확대 배율만 바꿔 본다
        confs = [(lang, 40), (lang, 56), (lang, 72)]
    else:
        confs = [(lang, 40), (lang, 56)] + ([(p_, 56 if p_ == "eng" else 48) for p_ in parts] if len(parts) > 1 else [(lang, 72)])
    pad = 20

    def one(job):
        sft, lg, target = job
        k = target / sft.shape[0]
        im = _resize(sft, k)
        k = im.shape[0] / sft.shape[0]
        sheet = np.full((im.shape[0] + 2 * pad, im.shape[1] + 2 * pad), 255, np.uint8)
        sheet[pad:pad + im.shape[0], pad:pad + im.shape[1]] = (255 * (1 - im)).astype(np.uint8)
        try:
            ws, chars = _ocr(sheet, True, lg)
        except Exception:
            return dict(words=[], conf=0.0, chars=[])
        ws = [dict(w, x=(w["x"] - pad) / k, w=w["w"] / k) for w in ws]
        return dict(words=[w["text"] for w in merge_words(ws, sft)], conf=float(np.mean([w["conf"] for w in ws])) if ws else 0.0,
                    chars=[(ch, (xa - pad) / k, (xb - pad) / k) for ch, xa, xb in chars])

    jobs = [(sft, lg, tg) for sft in softs for lg, tg in confs]
    if OCR_BACKEND is not None:
        res = [one(j) for j in jobs]
    else:
        with ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 2))) as ex:
            res = list(ex.map(one, jobs))
    n = len(confs)
    return [res[i * n:(i + 1) * n] for i in range(len(softs))]


def render_ref(face, text, lang=None, size=REF):
    """기준 크기로 한 줄을 그린다. (잉크 배열 0..1, 배열 원점의 '글자 원점(왼쪽·기준선)' 기준 좌표)"""
    f = face.pil(size)
    l, t, r, b = t_bbox(f, text, lang)
    if r - l < 2 or b - t < 2:
        return None
    m = 3
    im = Image.new("L", (int(r - l) + 2 * m, int(b - t) + 2 * m), 0)
    t_draw(ImageDraw.Draw(im), (m - l, m - t), text, f, 255, lang)
    arr = np.asarray(im, np.float32) / 255.0
    ys, xs = np.nonzero(arr > 0.2)
    if len(ys) == 0:
        return None
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    return arr[y0:y1, x0:x1], (l - m + x0, t - m + y0)


def space_cuts(face, words, lang=None, size=REF):
    """단어 사이 공백의 가운데 x좌표들(기준 크기, 글자 원점 기준)."""
    f = face.pil(size)
    st = word_starts(f, words, lang)
    return [(st[i] + t_len(f, words[i], lang) + st[i + 1]) / 2 for i in range(len(words) - 1)]


def spread(arr, off, cuts, delta, scale=1.0):
    """기준 크기 줄 그림을 scale배로 맞춘 뒤, 공백마다 delta(기준 크기 px)만큼 벌리거나 좁힌 배열.
    크기를 먼저 맞추고 벌리므로 단어 위치의 반올림 오차가 스캔 기준 0.5px을 넘지 않는다."""
    if abs(scale - 1.0) > 1e-6:
        arr = _resize(arr, scale)
    if not cuts or abs(delta) < 0.25:
        return arr
    h, w = arr.shape
    dd = delta * scale
    edges = [0] + [int(round(min(max((c - off[0]) * scale, 0), w))) for c in cuts] + [w]
    out = np.zeros((h, max(8, int(math.ceil(w + dd * len(cuts))) + 2)), np.float32)
    for i in range(len(edges) - 1):
        a, b = edges[i], edges[i + 1]
        if b <= a:
            continue
        x = int(round(a + i * dd))
        if x < 0:
            a, x = a - x, 0
        b2 = min(b, a + out.shape[1] - x)
        if b2 > a:
            np.maximum(out[:, x:x + b2 - a], arr[:, a:b2], out=out[:, x:x + b2 - a])
    return out


def _resize(a, fx):
    w, h = max(2, int(round(a.shape[1] * fx))), max(2, int(round(a.shape[0] * fx)))
    return cv2.resize(a, (w, h), interpolation=cv2.INTER_AREA if fx < 1 else cv2.INTER_LINEAR)


def _match(scan_pad, s2, templ, cos=False):
    """templ을 scan_pad 안에서 밀어 보며 가장 잘 겹치는 위치와 일치도(soft Dice = 2ΣST / (ΣS² + ΣT²)).
    정규화 상관과 달리 '얼마나 진한가'까지 맞아야 점수가 높다. 가는 글꼴+흐림과 굵은 글꼴을 구별하기 위함.
    단, 잉크의 진하기를 모르는 줄(회색 글자처럼 가는 획에서만 관측된 색)은 진하기를 맞출 기준이 없으므로
    cos=True로 모양만 견준다(ΣST / √(ΣS²·ΣT²)): 굵은 글꼴은 흐려져도 획의 폭이 넓다."""
    H, W = scan_pad.shape
    h, w = templ.shape
    t2 = float((templ * templ).sum())  # 잘려 나간 부분도 불일치로 센다
    if h > H:
        templ, h = templ[(h - H) // 2:(h - H) // 2 + H], H
    if w > W:
        templ, w = templ[:, :W], W
    if t2 < 1e-6:
        return -1.0, (0, 0)
    res = cv2.matchTemplate(scan_pad, np.ascontiguousarray(templ), cv2.TM_CCORR)
    _, mx, _, loc = cv2.minMaxLoc(res)
    return float(mx / math.sqrt(s2 * t2) if cos else 2 * mx / (s2 + t2)), loc


def _blur(a, sigma):
    return cv2.GaussianBlur(a, (0, 0), sigma) if sigma > 0.25 else a


class LineFit:
    """한 줄(스캔 잉크 마스크 + OCR 글자)에 글꼴을 대입해 점수를 매긴다."""
    sigma = 1.0  # 스캔의 흐림 정도(px). 렌더한 글자에도 같은 흐림을 입혀야 굵기를 공정하게 비교할 수 있다
    amp_free = False  # True: 잉크의 진하기를 모르는 줄 → 모양만 견준다(_match 참조)

    def __init__(self, text, words, soft, x0, y0):
        self.text, self.words, self.lang = text, words, lang_of(text)
        self.soft = soft.astype(np.float32)  # 잉크 0..1, 딱 맞게 자른 줄
        self.x0, self.y0 = x0, y0
        self.H, self.W = soft.shape
        self.nsp = max(0, len(words) - 1)
        self.pad = max(3, int(round(0.18 * self.H)))
        self.scan_pad = cv2.copyMakeBorder(self.soft, self.pad, self.pad, self.pad, self.pad, cv2.BORDER_CONSTANT)
        self.c = min(1.0, 26.0 / self.H)  # 1차 비교용 축소 비율
        self.scan_small = _resize(self.scan_pad, self.c) if self.c < 1 else self.scan_pad
        self.s2, self.s2_small = float((self.scan_pad ** 2).sum()), float((self.scan_small ** 2).sum())
        self._ref = {}

    def ref(self, face, size=REF, cuts=True):
        """이 줄의 글을 그 글꼴로 그린 기준 그림 (잉크 배열, 원점 좌표, 공백 위치). 공백 위치는 필요할 때만 잰다."""
        r = self._ref.get((id(face), size))
        if r is None:
            rr = render_ref(face, self.text, self.lang, size)
            r = [rr[0], rr[1], None] if rr else False
            self._ref[(id(face), size)] = r
        if r and cuts and r[2] is None:
            r[2] = space_cuts(face, self.words, self.lang, size)
        return r

    def _delta(self, arr, s, unit=REF):
        """폭 차이를 공백에 고르게 나눈 값(기준 크기 px). 공백이 없으면 0."""
        if not self.nsp:
            return 0.0
        d = (self.W / s - arr.shape[1]) / self.nsp
        return float(np.clip(d, -0.12 * unit, 0.8 * unit))

    def set_words(self, words):
        self.words, self.text, self.nsp, self._ref = list(words), " ".join(words), max(0, len(words) - 1), {}
        self.lang = lang_of(self.text)

    @staticmethod
    def _penalty(d, unit=REF):
        """띄어쓰기를 손봐야만 맞는 가설은 조금 불리하게(글꼴 본래 간격으로 맞는 글꼴이 우선)."""
        return 0.06 * min(1.0, abs(d) / (0.3 * unit))

    def _eval(self, arr, off, cuts, s, d, small=False, sigma=None, penalize=True, unit=REF):
        sigma = self.sigma if sigma is None else sigma
        c = self.c if small else 1.0
        t = _blur(spread(arr, off, cuts, d, s * c), sigma * c)
        sc, loc = _match(self.scan_small if small else self.scan_pad, self.s2_small if small else self.s2, t, self.amp_free)
        return sc - (self._penalty(d, unit) if penalize else 0.0), loc

    def coarse(self, face, small=True):
        """1차 비교(한 글꼴당 한두 번만 그려 본다). 저해상도 비교는 글자도 작게(REF_S) 그려서 쓴다."""
        unit = REF_S if small else REF
        r = self.ref(face, unit, cuts=False)
        if not r:
            return -1.0
        arr, off = r[0], r[1]
        sh, sw = self.H / arr.shape[0], self.W / arr.shape[1]
        best = -1.0
        if abs(math.log(sw / sh)) < 0.25:  # 가설 1: 글꼴 본래 간격 그대로 → 줄 폭으로 크기를 정한다(높이보다 훨씬 정확)
            best = self._eval(arr, off, None, sw, 0.0, small, unit=unit)[0]
        if self.nsp and sw > 1.03 * sh:  # 가설 2: 공백이 늘어난 줄(양쪽 정렬 등) → 높이로 크기, 남는 폭은 공백에
            cuts = self.ref(face, unit)[2]
            best = max(best, self._eval(arr, off, cuts, sh, self._delta(arr, sh, unit), small, unit=unit)[0])
        return best

    def fine(self, face, sigma=None):
        """정밀 비교: 크기를 조금씩 바꿔 보며 최고점을 찾는다."""
        sigma = self.sigma if sigma is None else sigma
        r = self.ref(face)
        if not r:
            return None
        arr, off, cuts = r
        sh, sw = self.H / arr.shape[0], self.W / arr.shape[1]
        s0 = sw if (not self.nsp or abs(math.log(sw / sh)) < 0.08) else sh
        best = None
        for step, rel in ((0.03, (-2, -1, 0, 1, 2)), (0.01, (-2, -1, 1, 2)), (0.004, (-2, -1, 1, 2))):
            base = best[1] if best else s0
            for kk in rel:
                s = base * (1 + kk * step)
                d = self._delta(arr, s)
                if abs(d) < 0.035 * REF:
                    d = 0.0
                sc, loc = self._eval(arr, off, cuts, s, d, False, sigma, penalize=False)  # 크기는 겹침만 보고 고른다
                if best is None or sc > best[0]:
                    best = (sc, s, d, loc)
        sc, s, d, loc = best
        sc -= self._penalty(d)
        return dict(score=sc, face=face, size=REF * s, wsp=d * s,
                    x=self.x0 - self.pad + loc[0] - s * off[0], y=self.y0 - self.pad + loc[1] - s * off[1], s=s, d=d)

    def implied_delta(self, face, s):
        """크기를 s로 놓았을 때 줄 폭이 맞으려면 공백 하나가 얼마나 달라져야 하는가(기준 크기 px)."""
        r = self.ref(face)
        return self._delta(r[0], s) if r else 0.0

    def refit(self, face, s, d):
        """크기 s와 공백 보정 d를 정해 놓고 위치만 다시 맞춘다."""
        r = self.ref(face)
        if not r:
            return None
        arr, off, cuts = r
        sc, loc = self._eval(arr, off, cuts, s, d, False)
        return dict(score=sc, face=face, size=REF * s, wsp=d * s,
                    x=self.x0 - self.pad + loc[0] - s * off[0], y=self.y0 - self.pad + loc[1] - s * off[1], s=s, d=d)

    def cells(self, fit):
        """맞춘 결과를 글자 칸 하나하나로 나눠 다시 비교한다. 줄 전체 점수는 높아도 한 글자가 틀렸을 수 있다.
        돌려주는 값: [(글에서의 위치 j, 칸 왼쪽 x, 칸 오른쪽 x, 겹침 점수, 잉크양)] — x는 줄 그림 좌표."""
        arr, off, cuts = self.ref(fit["face"])
        s, d = fit["s"], fit["d"]
        t = _blur(spread(arr, off, cuts, d, s), self.sigma)
        lx = int(round(fit["x"] + s * off[0] - self.x0))
        ly = int(round(fit["y"] + s * off[1] - self.y0))
        canvas = np.zeros_like(self.soft)
        ya, yb = max(0, ly), min(self.H, ly + t.shape[0])
        xa, xb = max(0, lx), min(self.W, lx + t.shape[1])
        if yb > ya and xb > xa:
            canvas[ya:yb, xa:xb] = t[ya - ly:yb - ly, xa - lx:xb - lx]
        f, kw, ox = fit["face"].pil(REF), _kw(self.lang), fit["x"] - self.x0
        out, j = [], 0
        for k, (w, st) in enumerate(zip(self.words, word_starts(f, self.words, self.lang))):
            for c in range(len(w)):
                a = ox + s * (st + k * d + t_len(f, w[:c], self.lang))
                b = ox + s * (st + k * d + t_len(f, w[:c + 1], self.lang))
                ia, ib = int(max(0, round(a) - 1)), int(min(self.W, round(b) + 1))
                sc, mass = 1.0, 0.0
                if ib - ia >= 3:
                    p, q = self.soft[:, ia:ib].ravel(), canvas[:, ia:ib].ravel()
                    mass = float(p @ p + q @ q)
                    sc = float(2 * (p @ q) / (mass + 1e-6))
                out.append((j, a, b, sc, mass))
                j += 1
            j += 1
        return out

    def weak_cells(self, fit):
        """다른 칸들보다 눈에 띄게 안 맞는 글자 칸 [(j, 왼쪽, 오른쪽, 점수)].
        잉크가 적은 칸(마침표·쌍점 같은 문장부호)은 1px만 어긋나도 점수가 출렁이므로 아주 안 맞을 때만 꼽는다."""
        cs = self.cells(fit)
        if not cs:
            return []
        med = float(np.median([c[3] for c in cs]))
        big = float(np.median([c[4] for c in cs]))
        return [c[:4] for c in cs if c[4] > 2.0 and c[3] < (min(0.80, med - 0.12) if c[4] > 0.35 * big else 0.5)]

    def word_scores(self, fit):
        """맞춘 결과를 단어 구간별로 다시 비교(검토 표시용)."""
        arr, off, cuts = self.ref(fit["face"])
        t = _blur(spread(arr, off, cuts, fit["d"], fit["s"]), self.sigma)
        lx = int(round(fit["x"] + fit["s"] * off[0] - self.x0))
        ly = int(round(fit["y"] + fit["s"] * off[1] - self.y0))
        canvas = np.zeros_like(self.soft)
        ya, yb = max(0, ly), min(self.H, ly + t.shape[0])
        xa, xb = max(0, lx), min(self.W, lx + t.shape[1])
        if yb > ya and xb > xa:
            canvas[ya:yb, xa:xb] = t[ya - ly:yb - ly, xa - lx:xb - lx]
        edges = [0] + [int(round(lx + (c - off[0] + i * fit["d"]) * fit["s"])) for i, c in enumerate(cuts, 1)] + [self.W]
        out = []
        for i in range(len(edges) - 1):
            a, b = max(0, edges[i]), min(self.W, edges[i + 1])
            if b - a < 3:
                out.append(0.0)
                continue
            p, q = self.soft[:, a:b].ravel(), canvas[:, a:b].ravel()
            out.append(float(2 * (p @ q) / (p @ p + q @ q + 1e-6)))
        return out, edges


REGION = {"hangul": " KR", "kana": " JP"}
DOC_REGION = None  # 문서에 한글(또는 가나)이 있으면 그 지역판. 로마자만 있는 줄의 동점 처리에 쓴다


def prune_regional(cands, need):
    """한글(또는 가나)만 쓰는 줄에서는 같은 글꼴 모음(.ttc) 안의 다른 지역판(JP/SC/TC/HK)이 똑같이 그려진다.
    해당 언어판만 남겨 비교 횟수를 줄인다. 로마자·숫자만 있는 줄도 지역판끼리 똑같이 그려지므로 문서의 언어판을 따른다
    (그러지 않으면 한국어 문서의 영문 줄에 '… CJK TC' 같은 엉뚱한 지역판 이름이 붙는다)."""
    tag = next((REGION[s] for s in ("hangul", "kana") if s in need), None) or DOC_REGION
    if not tag or "han" in need:
        return cands
    keep_keys = {(fc.path, fc.weight, "Mono" in fc.family) for fc in cands if tag in fc.family + " "}
    return [fc for fc in cands if tag in fc.family + " " or (fc.path, fc.weight, "Mono" in fc.family) not in keep_keys]


def _adj(score, face, need):
    """동점 처리용 미세 보정(사전 확률). 한글 줄에서는 고정폭판보다 일반판,
    이웃한 굵기끼리 점수가 거의 같으면(예: DemiLight 350 vs Regular 400) 흔한 굵기인 Regular·Bold 쪽.
    사진의 흐림 속에서 굵기 한 단계 차이는 점수 0.01 안팎이라, 그 정도 차이는 증거로 보지 않는다."""
    return (score - (0.002 if "hangul" in need and "Mono" in face.family else 0.0)
            - (0.0 if face.weight in (400, 700) else 0.012))


def respace(fit, face):
    """띄어쓰기를 글꼴 치수로 다시 정한다. OCR은 한글 음절 사이를 자주 잘못 띄우거나 붙인다.
    글꼴을 알면 두 글자 사이 빈틈이 '붙여 썼을 때'와 '띄어 썼을 때' 각각 몇 px이어야 하는지 계산할 수 있으므로,
    스캔에서 잰 빈틈이 어느 쪽에 가까운지로 판정한다. 로마자 단어 속(글자끼리 닿아 빈틈이 없다)은 건드리지 않는다."""
    r = fit.ref(face)
    chars = "".join(fit.words)
    if not r or len(chars) < 2:
        return False
    s = fit.H / r[0].shape[0]  # 줄 높이로 잰 크기(띄어쓰기 오류의 영향을 받지 않는다)
    f, kw = face.pil(REF), _kw(fit.lang)
    memo = {}

    def ink(t):
        """글자열을 스캔과 같은 조건(크기·흐림)으로 그렸을 때 잉크가 '보이는' 가로 범위(스캔 px, 글자 원점 기준).
        빈틈을 재는 것과 같은 문턱(0.25)을 쓴다 — 명조체의 가는 삐침은 흐려지면 문턱 아래로 사라지기 때문이다."""
        if t not in memo:
            rr = render_ref(face, t, fit.lang)
            memo[t] = (0.0, t_len(f, t, fit.lang) * s)
            if rr:
                m = int(3 * LineFit.sigma) + 2
                img = cv2.copyMakeBorder(_resize(rr[0], s), 0, 0, m, m, cv2.BORDER_CONSTANT)
                cols = np.nonzero(_blur(img, LineFit.sigma).max(0) >= 0.25)[0]
                if len(cols):
                    memo[t] = (rr[1][0] * s - m + cols[0], rr[1][0] * s - m + cols[-1] + 1)
        return memo[t]
    empty = np.concatenate([fit.soft.max(0) < 0.25, [False]])
    starts, lens, st = [], [], None
    for i, e in enumerate(empty):
        if e and st is None:
            st = i
        elif not e and st is not None:
            starts.append(st)
            lens.append(i - st)
            st = None
    if not starts:
        return False
    starts, lens = np.array(starts, np.float32), np.array(lens, np.float32)
    had, n = set(), 0
    for w in fit.words[:-1]:
        n += len(w)
        had.add(n - 1)
    spaces, cursor, seg0 = set(), 0.0, 0
    for j in range(len(chars) - 1):
        a, b = chars[j], chars[j + 1]
        both, either = _is_cjk(a) and _is_cjk(b), _is_cjk(a) or _is_cjk(b)
        if j not in had and not both:
            continue
        seg = chars[seg0:j + 1]  # 마지막으로 위치를 확인한 빈틈 이후의 글자들
        l0, r1 = ink(seg)
        edge = cursor + r1 - l0  # 이 글자의 잉크가 끝날 것으로 예상되는 자리
        i = int(np.abs(starts - edge).argmin())
        if abs(starts[i] - edge) > max(2.5, 0.10 * REF * s):  # 예상한 자리에 빈틈이 없다: 판정할 근거가 없으니 그대로 둔다
            if j in had:
                spaces.add(j)
            continue
        g_no = t_len(f, seg, fit.lang) * s - r1 + ink(b)[0]
        g_sp = g_no + space_adv(f, a, b, fit.lang) * s
        if (abs(lens[i] - g_sp) < abs(lens[i] - g_no)) if either else (j in had):
            spaces.add(j)
        cursor, seg0 = float(starts[i] + lens[i]), j + 1
    if spaces == had:
        return False
    words, cur = [], ""
    for j, ch in enumerate(chars):
        cur += ch
        if j in spaces:
            words.append(cur)
            cur = ""
    words.append(cur)
    fit.set_words([w for w in words if w])
    return True


def fix_spaces(fit, face):
    """OCR이 잘못 넣거나 빠뜨린 띄어쓰기를 바로잡는다. 먼저 글꼴 치수로 판정하고(respace),
    남은 것은 '그려 보고' 지운다: 공백 하나를 뺀 문장을 같은 글꼴로 그렸을 때 원본과 더 잘 맞으면 그 공백은 가짜다."""
    def questionable(a, b):
        return _is_cjk(a[-1]) or _is_cjk(b[0])
    changed = respace(fit, face)
    words = list(fit.words)
    base = fit.coarse(face, small=False)
    while len(words) > 1:
        best = (base + 0.004, None)
        for k in range(len(words) - 1):
            if not questionable(words[k], words[k + 1]):
                continue
            fit.set_words(words[:k] + [words[k] + words[k + 1]] + words[k + 2:])
            sc = fit.coarse(face, small=False)
            if sc > best[0]:
                best = (sc, k)
        if best[1] is None:
            break
        k = best[1]
        words = words[:k] + [words[k] + words[k + 1]] + words[k + 2:]
        base, changed = best[0], True
    fit.set_words(words)
    return changed


def match_fonts(fits, faces, stats, blur=None):
    """줄 단위 글꼴 매칭.
    1) 크기가 비슷한 줄끼리 묶고, 묶음마다 글자 수가 많은 대표 줄 2개만 전체 글꼴과 비교(저해상도)
    2) 거기서 추린 소수 후보만 나머지 줄에 정밀 대입. 점수가 낮은 줄만 전체 비교로 되돌아감
    3) 같은 묶음 안에서는 가능한 한 같은 글꼴로 합의
    """
    global DOC_REGION
    t0 = time.time()
    need = [scripts_of(f.text) for f in fits]
    seen_scripts = set().union(*need) if need else set()
    DOC_REGION = next((REGION[s] for s in ("hangul", "kana") if s in seen_scripts), None)
    script = [tuple(sorted(n - {"latin"})) or ("latin",) for n in need]  # 숫자·영문이 섞인 한글 줄도 한글 줄로 본다
    ink = [getattr(f, "ink_label", 0) for f in fits]
    order = sorted(range(len(fits)), key=lambda i: (script[i], ink[i], fits[i].H))
    clusters = []
    for i in order:  # 같은 문자 체계 + 같은 글자색 + 비슷한 글자 높이 (색이 다른 줄은 제목처럼 글꼴도 다르기 쉽다)
        if (clusters and script[i] == script[clusters[-1][0]] and ink[i] == ink[clusters[-1][0]]
                and fits[i].H <= 1.22 * fits[clusters[-1][0]].H):
            clusters[-1].append(i)
        else:
            clusters.append([i])
    cand = [prune_regional([fc for fc in faces if need[i] <= set(fc.scripts)], need[i]) for i in range(len(fits))]
    cand_set = [set(c) for c in cand]
    n_coarse = n_fine = 0
    table = [dict() for _ in fits]  # 줄별 {face: fit}
    full_scanned = set()

    def full_scan(i, top=6):
        nonlocal n_coarse
        sc = [(fits[i].coarse(fc), fc) for fc in cand[i]]
        n_coarse += len(sc)
        sc.sort(key=lambda t: -_adj(t[0], t[1], need[i]))
        full_scanned.add(i)
        return [fc for s, fc in sc[:top] if s > 0]

    def fine(i, fcs):
        nonlocal n_fine
        for fc in fcs:
            if fc not in table[i] and fc in cand_set[i]:
                r = fits[i].fine(fc)
                n_fine += 1
                if r:
                    table[i][fc] = r

    # 0) 스캔 흐림: 면 가장자리에서 잰 값이 있으면 그것을 쓰고, 없으면(글자뿐인 문서) 가장 긴 줄 몇 개에
    #    상위 후보 글꼴을 흐림을 바꿔 가며 입혀 보고 점수가 가장 높은 값을 쓴다
    if blur is not None:
        LineFit.sigma = blur
    else:
        probe = sorted(range(len(fits)), key=lambda i: -len(fits[i].text))[:4]
        tops = {i: full_scan(i, 3) for i in probe}
        sig_score = {}
        for sg in (0.0, 0.5, 0.8, 1.1, 1.4, 1.8, 2.3):
            best = [max((r["score"] for r in (fits[i].fine(fc, sg) for fc in tops[i]) if r), default=0) for i in probe]
            sig_score[sg] = float(np.mean(best)) if best else 0
        LineFit.sigma = max(sig_score, key=sig_score.get)
        full_scanned.clear()
    stats["blur_sigma"] = round(LineFit.sigma, 2)
    stats["blur_from"] = "edges" if blur is not None else "text"

    for cl in clusters:
        reps = sorted(cl, key=lambda i: -len(fits[i].text))[:2]
        short = []
        for i in reps:
            for fc in full_scan(i):
                if fc not in short:
                    short.append(fc)
        rep_best = []
        for i in reps:
            fine(i, short)
            rep_best.append(max((r["score"] for r in table[i].values()), default=0))
        thr = min(0.92, max(0.80, min(rep_best) - 0.03))
        for i in cl:
            fine(i, short)
            best = max((r["score"] for r in table[i].values()), default=-1)
            if best < 0.95 and table[i] and (fits[i].nsp or fits[i].lang):  # 점수가 아쉬운 줄: 띄어쓰기 오인식부터 의심
                top = max(table[i].items(), key=lambda kv: kv[1]["score"])[0]
                if fix_spaces(fits[i], top):
                    table[i].clear()
                    need[i] = scripts_of(fits[i].text)
                    fine(i, short)
                    best = max((r["score"] for r in table[i].values()), default=-1)
            if best < thr and i not in full_scanned:  # 묶음 후보로 설명이 안 되는 줄만 전체 비교
                fine(i, full_scan(i))
            if table[i]:  # 가장 잘 맞은 글꼴 가족의 다른 굵기들도 확인(굵기는 저해상도 비교로 가리기 어렵다)
                top = max(table[i].items(), key=lambda kv: kv[1]["score"])[0]
                fine(i, [fc for fc in cand[i] if fc.family == top.family and fc.italic == top.italic])
        # 합의: 묶음에서 총점이 가장 높은 글꼴을 고르고, 그 글꼴로도 충분히 설명되는 줄(자기 최고점과 근소한 차이)에 배정.
        # 설명 안 되는 줄들만 남겨 같은 일을 반복한다 → 본문은 Regular, 머리글은 Bold처럼 소수의 글꼴로 정리된다
        left = [i for i in cl if table[i]]
        own = {i: max(_adj(r["score"], fc, need[i]) for fc, r in table[i].items()) for i in left}
        while left:
            tot = defaultdict(float)
            for i in left:
                for fc, r in table[i].items():
                    tot[fc] += max(0.0, _adj(r["score"], fc, need[i])) * len(fits[i].text)
            pick = max(tot.items(), key=lambda kv: (kv[1], id(kv[0])))[0]
            got = [i for i in left if pick in table[i] and _adj(table[i][pick]["score"], pick, need[i])
                   >= own[i] - (0.012 + 0.03 / math.sqrt(max(1, len(fits[i].text))))]  # 짧은 줄은 증거가 약하다
            if not got:  # 합의 불가 → 각자 최고점
                for i in left:
                    fits[i].result = max(table[i].items(), key=lambda kv: _adj(kv[1]["score"], kv[0], need[i]))[1]
                break
            for i in got:
                fits[i].result = table[i][pick]
            left = [i for i in left if i not in got]
        for i in cl:
            if table[i]:
                fits[i].runner_up = sorted(((r["score"], fc.label) for fc, r in table[i].items()), reverse=True)[:4]
    stats.update(font_candidates=len(faces), coarse_evals=n_coarse, fine_evals=n_fine,
                 full_scans=len(full_scanned), lines=len(fits), font_match_sec=round(time.time() - t0, 2))


def _hangul_2350():
    """KS X 1001 완성형 한글 2,350자(일상 문서에 나오는 음절은 거의 다 여기 있다)."""
    out = []
    for c in range(0xAC00, 0xD7A4):
        try:
            chr(c).encode("euc-kr")
            out.append(chr(c))
        except UnicodeEncodeError:
            pass
    return out


_ALPHA = {}
SYMBOLS = [chr(c) for c in range(33, 127)] + list("·•※–—‘’“”…°×→")


def glyph_fix(f, limit=4):
    """잘 맞은 줄에서 유독 안 맞는 글자 칸에 다른 글자를 '대입'해 본다 — 글꼴·크기·위치를 이미 알고 있으므로
    후보 글자를 같은 자리에 그려 겹침을 재기만 하면 된다. 한글 칸에는 완성형 2,350자를, 그 밖에는 기호·영문·숫자를 넣어 본다.
    폭이 같은 글자는 그 칸만 보고 바꾸고, 폭이 다른 글자(뒤 글자들이 밀린다)는 줄 전체를 다시 맞춰 봐서 더 나을 때만 바꾼다.
    바꾼 글자 수를 돌려준다."""
    r = f.result
    if not RAQM:
        # 단순 조판은 커닝을 흉내 내지 못해 칸이 1~2px씩 어긋날 수 있고, 그 정도 어긋남이면 닮은 글자가 더 잘 맞아 보이기도 한다
        # (T→7, i→I). 칸 단위 판정을 믿을 수 없으므로 하지 않는다
        return 0
    weak = f.weak_cells(r)
    n_ch = len(f.text.replace(" ", ""))
    if len(weak) > max(limit, 0.2 * n_ch):  # 너무 많이 틀렸으면 글자 대입으로 고칠 일이 아니다
        return 0
    if r["score"] < 0.97:
        # 문장부호는 잉크가 적어 칸 점수로는 틀린 줄 모른다(가운뎃점을 붙임표로 읽어도 줄 점수는 거의 그대로다).
        # 대신 폭이 달라 뒤 글자들이 조금씩 밀리므로, 줄이 완벽히 맞지 않을 때는 문장부호 칸도 후보로 올려 줄 전체로 검증한다
        seen = {c[0] for c in weak}
        weak = weak + [c[:4] for c in f.cells(r) if c[0] not in seen and not f.text[c[0]].isalnum() and c[3] < 0.9][:6]
    if not weak:
        return 0
    if "hangul" not in _ALPHA:
        _ALPHA["hangul"] = _hangul_2350()
    face = r["face"]
    fixed = 0
    for j, a, b, sc in sorted(weak, key=lambda c: -c[0]):  # 뒤에서부터: 앞 글자의 위치가 바뀌지 않게
        r = f.result
        font, kw, base_y = face.pil(r["size"]), _kw(f.lang), r["y"] - f.y0
        ch, adv = f.text[j], b - a
        cands = [c for c in (_ALPHA["hangul"] if _is_hangul(ch) else SYMBOLS) if c != ch and face.supports(c)]
        if not cands:
            continue
        advs = np.array([t_len(font, c, f.lang) for c in cands])
        ia = int(max(0, round(a) - 2))
        ib = int(min(f.W, round(a + max(adv, float(advs.max()))) + 2))
        tiles = []
        for c in cands:
            im = Image.new("L", (ib - ia, f.H), 0)
            t_draw(ImageDraw.Draw(im), (a - ia, base_y), c, font, 255, f.lang)
            tiles.append(np.asarray(im, np.float32) / 255.0)
        T_ = np.stack(tiles)
        if f.sigma > 0.3:
            k = max(3, int(2 * round(3 * f.sigma) + 1))
            T_ = np.stack([cv2.GaussianBlur(t_, (k, k), f.sigma) for t_ in T_])
        win = f.soft[:, ia:ib]
        # 각 후보를 '자기 칸'(펜 위치부터 자기 폭까지)에서만 견준다
        cols = np.arange(ib - ia)[None, :]
        own = ((cols >= round(a) - 2 - ia) & (cols < np.round(a + np.minimum(advs, adv))[:, None] + 2 - ia)).astype(np.float32)[:, None, :]
        num = 2 * (T_ * win[None] * own).sum((1, 2))
        dice = num / ((win[None] * win[None] * own).sum((1, 2)) + (T_ * T_ * own).sum((1, 2)) + 1e-6)
        dice[(T_.reshape(len(cands), -1).max(1) < 0.2)] = -1  # 글꼴에 그림이 없는 글자
        same = np.abs(advs - adv) <= max(1.0, 0.04 * adv)
        need = sc + (0.10 if sc >= 0.5 else 0.25)
        i = int(np.where(same, dice, -1).argmax())
        if same[i] and dice[i] > need and dice[i] >= 0.82:  # 폭이 같은 글자: 이 칸만 보고 결정
            f.set_words((f.text[:j] + cands[i] + f.text[j + 1:]).split(" "))
            f.result = f.fine(face) or r
            fixed += 1
            continue
        best = (r["score"] + 0.012, None, None)  # 폭이 다른 글자: 잘 맞는 몇 개만 줄 전체로 검증(확실히 나아질 때만)
        keep = list(f.words)
        for i in np.argsort(-np.where(same, -1, dice))[:3]:
            if dice[i] < 0.5:
                break
            f.set_words((f.text[:j] + cands[i] + f.text[j + 1:]).split(" "))
            r2 = f.fine(face)
            if r2 and r2["score"] > best[0]:
                best = (r2["score"], list(f.words), r2)
            f.set_words(keep)
        if best[1]:
            f.set_words(best[1])
            f.result = best[2]
            fixed += 1
    return fixed


def splice(f, variants):
    """안 맞는 구간을 다른 OCR 결과의 같은 자리 글자들로 바꿔 끼워 본다(예: 한글은 kor 결과가, 'PDF'는 eng 결과가 맞는 줄).
    바꿔 그렸을 때 원본과 더 잘 겹치면 채택한다."""
    changed = 0
    for _ in range(4):
        r = f.result
        weak = f.weak_cells(r)
        if not weak:
            break
        spans = []  # 이웃한 칸은 한 구간으로
        for j, a, b, sc in weak:
            if spans and j - spans[-1][1] <= 2:
                spans[-1][1], spans[-1][3] = j, b
            else:
                spans.append([j, j, a, b])
        best = (r["score"] + 0.01, None, None)
        for j0, j1, xa, xb in spans:
            cur = f.text[j0:j1 + 1].replace(" ", "")
            for v in variants:
                rep = "".join(ch for ch, ca, cb in v["chars"] if min(cb, xb) - max(ca, xa) > 0.6 * (cb - ca))
                if not rep or rep == cur:
                    continue
                words = (f.text[:j0] + rep + f.text[j1 + 1:]).split()
                keep = list(f.words)
                f.set_words(words)
                r2 = f.fine(r["face"])
                if r2 and r2["score"] > best[0]:
                    best = (r2["score"], words, r2)
                f.set_words(keep)
        if best[1] is None:
            break
        f.set_words(best[1])
        f.result = best[2]
        changed += 1
    return changed


def rescue(fits, faces, lang, stats, good=0.93):
    """첫 OCR이 미덥지 않은 줄을 바로잡는다. OCR은 후보를 내는 역할만 하고, 채택은 '그려 본 것과 원본의 겹침'이 결정한다.
      1) 줄을 다른 설정으로 다시 읽어, 읽은 결과들을 하나씩 그려 보고 가장 잘 겹치는 것을 고른다(글꼴은 문서에서 확인된 것부터)
      2) 그래도 안 맞는 구간은 다른 OCR 결과의 같은 자리 글자들로 바꿔 끼워 본다
      3) 마지막으로 안 맞는 글자 칸에 폭이 비슷한 다른 글자를 대입해 본다"""
    t0 = time.time()

    def sc(f):
        return f.result["score"] if getattr(f, "result", None) else -1.0

    hs = [f.H for f in fits if sc(f) >= 0.85]
    hmax = 2.5 * max(hs) if hs else 1e9  # 글자로 확인된 줄보다 훨씬 큰 덩어리는 그림일 가능성이 높다 → 다시 읽지 않는다
    weak = {id(f): bool(f.weak_cells(f.result)) for f in fits if sc(f) >= 0.78}
    todo = [f for f in fits if (sc(f) < good or f.conf < 60 or weak.get(id(f))) and (f.text or (len(f.ids) >= 2 and f.H <= hmax))][:60]
    stats.update(reread_lines=len(todo), reread_changed=0, chars_fixed=0)
    if not todo:
        return
    use = defaultdict(float)
    for f in fits:
        if sc(f) >= good:
            use[f.result["face"]] += len(f.text)
    doc = [fc for fc, _ in sorted(use.items(), key=lambda kv: -kv[1])[:8]]
    try:
        variants = ocr_variants([f.soft for f in todo], lang)
    except Exception as e:
        log(f"    ! 다시 읽기 실패({type(e).__name__})")
        return
    for f, vs in zip(todo, variants):
        cur, text0 = getattr(f, "result", None), f.text
        best = (sc(f), list(f.words), cur, f.conf)
        seen, cands = {f.text}, []
        for v in sorted(vs, key=lambda v: -v["conf"]):
            text = " ".join(v["words"])
            if text and text not in seen and any(ch.isalnum() for ch in text):
                seen.add(text)
                cands.append(v)
        scans = 0
        for v in cands:
            f.set_words(v["words"])
            need = scripts_of(f.text)
            ok = [fc for fc in faces if need <= set(fc.scripts)]
            short = [fc for fc in dict.fromkeys(([cur["face"]] if cur else []) + doc) if need <= set(fc.scripts)]
            rs = [r for r in (f.fine(fc) for fc in short) if r]
            if max((r["score"] for r in rs), default=-1) < 0.85 and scans < 2:  # 이미 쓰인 글꼴로는 설명이 안 됨 → 전체 글꼴과 비교
                scans += 1
                pool = prune_regional(ok, need)
                rank = sorted(((f.coarse(fc), i) for i, fc in enumerate(pool)), reverse=True)[:6]
                rs += [r for r in (f.fine(pool[i]) for s_, i in rank if s_ > 0) if r]
            if not rs:
                continue
            top = max(rs, key=lambda r: _adj(r["score"], r["face"], need))
            if (f.nsp or f.lang) and top["score"] < 0.95 and fix_spaces(f, top["face"]):
                top = f.fine(top["face"]) or top
            sib = [fc for fc in ok if fc.family == top["face"].family and fc.italic == top["face"].italic and fc is not top["face"]]
            rs = [top] + [r for r in (f.fine(fc) for fc in sib) if r]
            top = max(rs, key=lambda r: _adj(r["score"], r["face"], need))
            if top["score"] > best[0] + 0.01:
                best = (top["score"], list(f.words), top, v["conf"])
                f.runner_up = sorted(((r["score"], r["face"].label) for r in rs), reverse=True)[:4]
        f.set_words(best[1])
        f.result, f.conf = best[2], best[3]
        if f.result and f.result["score"] >= 0.78:
            splice(f, vs)
            stats["chars_fixed"] += glyph_fix(f)
        if f.text != text0:
            stats["reread_changed"] += 1
    stats["reread_sec"] = round(time.time() - t0, 2)


def decide_locl(fits):
    """문서가 '한국어용 글리프 대체(locl·calt)'를 쓰는 방식으로 조판됐는지, 아닌지를 정한다.
    잘 맞은 한글 줄들을 두 방식으로 각각 그려 보고, 전체적으로 더 잘 겹치는 쪽을 문서 전체에 적용한다."""
    global KO_LOCL
    if not RAQM:  # 단순 조판에는 글리프 대체가 없다
        KO_LOCL = False
        return
    ko = [f for f in fits if f.lang == "ko" and getattr(f, "result", None) and f.result["score"] >= 0.85]
    if not ko:
        return
    alt = []
    KO_LOCL = False
    for f in ko:
        f._ref = {}
        alt.append(f.fine(f.result["face"]))
    gain = sum(len(f.text) * ((a["score"] if a else 0) - f.result["score"]) for f, a in zip(ko, alt)) / sum(len(f.text) for f in ko)
    if gain > 0.002:
        for f in fits:
            if f.lang == "ko":
                f._ref = {}
                if getattr(f, "result", None):
                    f.result = f.fine(f.result["face"]) or f.result
    else:
        KO_LOCL = True
        for f in ko:
            f._ref = {}
    return gain


def harmonize(fits):
    # 크기·띄어쓰기 합의: 같은 글꼴이고 크기가 4% 안에 모인 줄들은 한 문단의 줄들로 보고 값을 통일한다.
    # 줄 하나만으로는 '글자가 조금 작다'와 '띄어쓰기가 조금 좁다'를 가르기 어렵지만, 크기를 함께 정하고 나면
    # 줄 폭(가장 정확히 잴 수 있는 값)에서 띄어쓰기를 역산할 수 있다.
    def wmedian(v, w):
        o = np.argsort(v)
        c = np.cumsum(np.asarray(w, np.float64)[o])
        return float(np.asarray(v)[o][np.searchsorted(c, c[-1] / 2)])

    by_face = defaultdict(list)
    for i, f in enumerate(fits):
        if getattr(f, "result", None):
            by_face[f.result["face"]].append(i)
    for fc, ids in by_face.items():
        ids.sort(key=lambda i: fits[i].result["s"])
        groups = [[ids[0]]]
        for i in ids[1:]:
            if fits[i].result["s"] <= 1.04 * fits[groups[-1][0]].result["s"]:
                groups[-1].append(i)
            else:
                groups.append([i])
        for g in groups:
            if len(g) < 2:
                continue
            s_c = wmedian([fits[i].result["s"] for i in g], [len(fits[i].text) for i in g])
            d_own = {i: fits[i].implied_delta(fc, s_c) if fits[i].nsp else 0.0 for i in g}
            sp = [i for i in g if fits[i].nsp]
            d_c = wmedian([d_own[i] for i in sp], [fits[i].nsp for i in sp]) if sp else 0.0
            if abs(d_c) < 0.02 * REF:
                d_c = 0.0
            for i in g:
                d = d_own[i]
                if fits[i].nsp and abs(d - d_c) < 1.5 / (s_c * fits[i].nsp) + 0.01 * REF:
                    d = d_c  # 문단 공통값과 측정 오차 안에서 같다(양쪽 정렬처럼 줄마다 다른 경우는 제 값을 유지)
                elif abs(d) < 0.035 * REF:
                    d = 0.0
                new = fits[i].refit(fc, s_c, d)
                if new and new["score"] >= fits[i].result["score"] - (0.012 + 0.05 / math.sqrt(max(1, len(fits[i].text)))):
                    fits[i].result = new


# ─────────────────────────────────────────────────────────────────────────────
# 5. 도형: 가설을 그려 보고 맞으면 기본 도형으로
# ─────────────────────────────────────────────────────────────────────────────
SS = 4  # 도형 마스크 슈퍼샘플 배율


def _poly(big, pts, val=255):
    """연속 좌표의 다각형을 SS배 캔버스에 채운다(여러 개면 even-odd)."""
    polys = pts if isinstance(pts, list) else [pts]
    cv2.fillPoly(big, [np.round((np.asarray(q, np.float64) * SS - 0.5) * 8).astype(np.int32) for q in polys], val,
                 lineType=cv2.LINE_8, shift=3)


def rrect_pts(x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    if r < 0.3:
        return np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], np.float64)
    out = []
    for cx, cy, a0 in ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180)):
        a = np.radians(np.linspace(a0, a0 + 90, 10))
        out.append(np.stack([cx + r * np.cos(a), cy + r * np.sin(a)], 1))
    return np.vstack(out)


def ellipse_pts(cx, cy, rx, ry, ang=0.0, n=96):
    a = np.linspace(0, 2 * np.pi, n, endpoint=False)
    p = np.stack([rx * np.cos(a), ry * np.sin(a)], 1)
    t = math.radians(ang)
    R = np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])
    return p @ R.T + [cx, cy]


def shape_mask(el, shape, ox=0.0, oy=0.0):
    """도형 하나를 0..255 알파 마스크로 그린다(검증·미리보기 공용)."""
    m = np.zeros((shape[0] * SS, shape[1] * SS), np.uint8)
    t, sw = el["type"], el.get("sw", 0.0)
    o = np.array([ox, oy])

    def filled_or_stroked(outer, inner):
        if el.get("stroke") is None:
            _poly(m, outer(0.0) - o)
        else:
            _poly(m, outer(sw / 2) - o)
            inn = inner(sw / 2)
            if inn is not None:
                _poly(m, inn - o, 0)
    if t == "rect":
        x, y, w, h, r = el["x"], el["y"], el["w"], el["h"], el.get("rx", 0.0)
        filled_or_stroked(lambda g: rrect_pts(x - g, y - g, w + 2 * g, h + 2 * g, r + g if r > 0.3 else 0),
                          lambda g: rrect_pts(x + g, y + g, w - 2 * g, h - 2 * g, max(0, r - g))
                          if w > 2 * g and h > 2 * g else None)
    elif t == "ellipse":
        cx, cy, rx, ry, a = el["cx"], el["cy"], el["rx"], el["ry"], el.get("angle", 0.0)
        filled_or_stroked(lambda g: ellipse_pts(cx, cy, rx + g, ry + g, a),
                          lambda g: ellipse_pts(cx, cy, rx - g, ry - g, a) if rx > g and ry > g else None)
    elif t == "line":
        p1, p2 = np.array([el["x1"], el["y1"]]), np.array([el["x2"], el["y2"]])
        d = p2 - p1
        L = np.linalg.norm(d) + 1e-9
        nrm = np.array([-d[1], d[0]]) / L * sw / 2
        if el.get("dash"):  # 점선: 토막마다 그린다
            on, off = el["dash"]
            a = 0.0
            while a < L:
                q1, q2 = p1 + d * (a / L), p1 + d * (min(L, a + on) / L)
                _poly(m, np.array([q1 + nrm, q2 + nrm, q2 - nrm, q1 - nrm]) - o)
                a += on + off
        else:
            _poly(m, np.array([p1 + nrm, p2 + nrm, p2 - nrm, p1 - nrm]) - o)
    elif t == "path":
        polys = [flatten_subpath(sp) - o for sp in el["subpaths"]]
        polys = [q for q in polys if len(q) >= 3]
        if polys:
            _poly(m, polys)
    return cv2.resize(m, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)


def _agree(M, R):
    """1px 오차를 허용한 일치도(가는 선도 공정하게)와 IoU."""
    k = np.ones((3, 3), np.uint8)
    a = (M & cv2.dilate(R, k)).sum() / max(1, M.sum())
    b = (R & cv2.dilate(M, k)).sum() / max(1, R.sum())
    return min(a, b), (M & R).sum() / max(1, (M | R).sum())


def fit_primitive(M, u):
    """이진 마스크 M(여백 포함)에 기본 도형을 맞춰 본다. 맞으면 요소 목록, 아니면 None."""
    cnts, _ = cv2.findContours(M, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not cnts:
        return None
    cnt = max(cnts, key=len)
    x, y, bw, bh = cv2.boundingRect(cnt)
    A = int(M.sum())
    filled = np.zeros_like(M)
    cv2.drawContours(filled, [cnt], -1, 1, cv2.FILLED)
    Ao = int(filled.sum())
    hn, hcc, hst, _ = cv2.connectedComponentsWithStats(filled & (1 - M), 4)
    holes = [hst[i] for i in range(1, hn) if hst[i][4] >= max(6, 0.004 * Ao)]
    cands = []

    def r_of():
        r = math.sqrt(max(0.0, bw * bh - Ao) / (4 - math.pi))
        return 0.0 if r < max(2.0, 2.5 * u) else min(r, bw / 2, bh / 2)

    if not holes:
        (cx, cy), (rw, rh), ang = cv2.minAreaRect(cnt)
        lng, sht = max(rw, rh) + 1, min(rw, rh) + 1
        if lng >= 5 * sht and lng >= 10 * u and A >= 0.75 * lng * sht:
            th = math.radians(ang if rw >= rh else ang + 90)
            dx, dy = math.cos(th) * lng / 2, math.sin(th) * lng / 2
            sw = A / lng
            if abs(dy) < max(0.6, 0.004 * lng):
                cands.append(dict(type="line", x1=x, y1=y + bh / 2, x2=x + bw, y2=y + bh / 2, sw=A / bw, stroke=1))
            elif abs(dx) < max(0.6, 0.004 * lng):
                cands.append(dict(type="line", x1=x + bw / 2, y1=y, x2=x + bw / 2, y2=y + bh, sw=A / bh, stroke=1))
            else:
                cands.append(dict(type="line", x1=cx + .5 - dx, y1=cy + .5 - dy, x2=cx + .5 + dx, y2=cy + .5 + dy,
                                  sw=sw, stroke=1))
        r = r_of()
        round_ = r > 0.42 * min(bw, bh) and abs(bw - bh) < 0.1 * max(bw, bh)  # 사실상 원 → 타원 후보에 맡긴다
        if not round_:
            cands.append(dict(type="rect", x=x, y=y, w=bw, h=bh, rx=r))
        cands.append(dict(type="ellipse", cx=x + bw / 2, cy=y + bh / 2, rx=bw / 2, ry=bh / 2))
        if len(cnt) >= 12 and min(bw, bh) >= 8:
            (ex, ey), (ea, eb), eang = cv2.fitEllipse(cnt)
            if abs((eang + 45) % 90 - 45) > 4 and max(ea, eb) < 2 * max(bw, bh):
                cands.append(dict(type="ellipse", cx=ex + .5, cy=ey + .5, rx=ea / 2 + .5, ry=eb / 2 + .5, angle=eang))
    elif len(holes) == 1 and holes[0][4] >= 0.25 * Ao:
        hx, hy, hw, hh, _ = holes[0]
        gaps = np.array([hx - x, hy - y, x + bw - hx - hw, y + bh - hy - hh], np.float64)
        t = float(np.median(gaps))
        if t >= 1 and np.abs(gaps - t).max() <= max(1.5, 0.4 * t):
            r = r_of()
            if not (r > 0.42 * min(bw, bh) and abs(bw - bh) < 0.1 * max(bw, bh)):
                cands.append(dict(type="rect", x=x + t / 2, y=y + t / 2, w=bw - t, h=bh - t,
                                  rx=max(0.0, r - t / 2) if r else 0.0, sw=t, stroke=1))
            cands.append(dict(type="ellipse", cx=x + bw / 2, cy=y + bh / 2, rx=(bw - t) / 2, ry=(bh - t) / 2,
                              sw=t, stroke=1))
    best = None
    for el in cands:
        ag, iou = _agree(M, (shape_mask(el, M.shape) >= 128).astype(np.uint8))
        thr = 0.93 if el.get("stroke") else 0.965
        if ag >= thr and (best is None or iou > best[0]):
            best = (iou, el)
    if best:
        iou, el = best
        if el["type"] == "ellipse" and el.get("angle") and abs(el["rx"] - el["ry"]) < 0.03 * max(el["rx"], el["ry"]):
            best = (iou, {k_: v_ for k_, v_ in el.items() if k_ != "angle"})  # 원에 가까우면 회전은 의미 없다
        if el["type"] == "rect" and el.get("rx", 0) > 0:  # 모서리 반지름은 넓이로 어림한 값 주변을 그려 보고 고른다
            for f in (0.45, 0.55, 0.65, 0.75, 0.85, 0.93, 1.07, 1.15):
                e2 = dict(el, rx=el["rx"] * f)
                i2 = _agree(M, (shape_mask(e2, M.shape) >= 128).astype(np.uint8))[1]
                if i2 > iou:
                    iou, best = i2, (i2, e2)
        return [best[1]]
    # 가로·세로 직선 묶음(표, 칸막이가 있는 상자)
    Lmin = int(max(14 * u, 14))
    hor = cv2.morphologyEx(M, cv2.MORPH_OPEN, np.ones((1, Lmin), np.uint8))
    ver = cv2.morphologyEx(M, cv2.MORPH_OPEN, np.ones((Lmin, 1), np.uint8))
    els = []
    for img, horiz in ((hor, True), (ver, False)):
        n, cc, st, _ = cv2.connectedComponentsWithStats(img, 8)
        for i in range(1, n):
            lx, ly, lw, lh, la = st[i]
            if horiz and lh <= max(3, 0.25 * lw):
                els.append(dict(type="line", x1=lx, y1=ly + lh / 2, x2=lx + lw, y2=ly + lh / 2, sw=la / lw, stroke=1))
            elif not horiz and lw <= max(3, 0.25 * lh):
                els.append(dict(type="line", x1=lx + lw / 2, y1=ly, x2=lx + lw / 2, y2=ly + lh, sw=la / lh, stroke=1))
    if len(els) >= 2:
        R = np.zeros_like(M)
        for el in els:
            R |= (shape_mask(el, M.shape) >= 128).astype(np.uint8)
        if _agree(M, R)[0] >= 0.95:
            sws = np.array([e["sw"] for e in els])
            if sws.max() <= 1.6 * sws.min():  # 같은 굵기로 그린 선들로 보고 중앙값으로 통일
                for e in els:
                    e["sw"] = float(np.median(sws))
            return _lines_to_rect(els, x, y, bw, bh)
    return None


def _lines_to_rect(els, x, y, bw, bh):
    """직선 묶음에서 바깥 테두리 4개를 찾으면 <rect> 하나로 합친다."""
    tol = lambda e: 2.0 + e["sw"]
    hs = [e for e in els if abs(e["y1"] - e["y2"]) < 1e-6 and abs(e["x1"] - x) < tol(e) and abs(e["x2"] - x - bw) < tol(e)]
    vs = [e for e in els if abs(e["x1"] - e["x2"]) < 1e-6 and abs(e["y1"] - y) < tol(e) and abs(e["y2"] - y - bh) < tol(e)]
    top = [e for e in hs if abs(e["y1"] - e["sw"] / 2 - y) < tol(e)]
    bot = [e for e in hs if abs(e["y1"] + e["sw"] / 2 - y - bh) < tol(e)]
    lef = [e for e in vs if abs(e["x1"] - e["sw"] / 2 - x) < tol(e)]
    rig = [e for e in vs if abs(e["x1"] + e["sw"] / 2 - x - bw) < tol(e)]
    if top and bot and lef and rig:
        four = [top[0], bot[0], lef[0], rig[0]]
        sw = float(np.mean([e["sw"] for e in four]))
        rest = [e for e in els if not any(e is f for f in four)]
        rect = dict(type="rect", x=lef[0]["x1"], y=top[0]["y1"], w=rig[0]["x1"] - lef[0]["x1"],
                    h=bot[0]["y1"] - top[0]["y1"], rx=0.0, sw=sw, stroke=1)
        return [rect] + rest
    return els


# ─────────────────────────────────────────────────────────────────────────────
# 6. 베지어 트레이싱 (Schneider의 곡선 적합)
# ─────────────────────────────────────────────────────────────────────────────
def _bez(c, t):
    t = t[:, None]
    mt = 1 - t
    return mt ** 3 * c[0] + 3 * mt ** 2 * t * c[1] + 3 * mt * t ** 2 * c[2] + t ** 3 * c[3]


def _unit(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-9 else np.array([1.0, 0.0])


def _gen_bezier(pts, uu, t1, t2):
    p0, p3 = pts[0], pts[-1]
    b0, b1, b2, b3 = (1 - uu) ** 3, 3 * uu * (1 - uu) ** 2, 3 * uu ** 2 * (1 - uu), uu ** 3
    A1, A2 = t1[None] * b1[:, None], t2[None] * b2[:, None]
    c00, c01, c11 = (A1 * A1).sum(), (A1 * A2).sum(), (A2 * A2).sum()
    tmp = pts - (p0[None] * (b0 + b1)[:, None] + p3[None] * (b2 + b3)[:, None])
    x0, x1 = (A1 * tmp).sum(), (A2 * tmp).sum()
    det = c00 * c11 - c01 * c01
    seg = float(np.linalg.norm(p3 - p0))
    arc = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
    if abs(det) > 1e-9:
        al, ar = (x0 * c11 - x1 * c01) / det, (c00 * x1 - c01 * x0) / det
    else:
        al = ar = seg / 3
    if al < 1e-3 * seg or ar < 1e-3 * seg or al > arc or ar > arc:
        al = ar = seg / 3
    return np.array([p0, p0 + t1 * al, p3 + t2 * ar, p3])


def _reparam(c, pts, uu):
    d1 = 3 * (c[1:] - c[:-1])
    d2 = 2 * (d1[1:] - d1[:-1])
    t = uu[:, None]
    q = _bez(c, uu)
    q1 = (1 - t) ** 2 * d1[0] + 2 * (1 - t) * t * d1[1] + t ** 2 * d1[2]
    q2 = (1 - t) * d2[0] + t * d2[1]
    num = ((q - pts) * q1).sum(1)
    den = (q1 * q1).sum(1) + ((q - pts) * q2).sum(1)
    return np.clip(uu - np.where(np.abs(den) > 1e-12, num / np.where(den == 0, 1, den), 0), 0, 1)


def fit_curve(pts, t1, t2, tol, depth=0):
    """점열을 오차 tol 이내의 3차 베지어 조각들로."""
    n = len(pts)
    if n < 2:
        return []
    if n == 2:
        d = float(np.linalg.norm(pts[1] - pts[0])) / 3
        return [np.array([pts[0], pts[0] + t1 * d, pts[1] + t2 * d, pts[1]])]
    cl = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))])
    if cl[-1] < 1e-6:
        return []
    uu = cl / cl[-1]
    c = _gen_bezier(pts, uu, t1, t2)
    for it in range(5):
        err = np.linalg.norm(_bez(c, uu) - pts, axis=1)
        i = int(err[1:-1].argmax()) + 1
        if err[i] <= tol:
            return [c]
        if err[i] > 4 * tol or it == 4:
            break
        uu = _reparam(c, pts, uu)
        c = _gen_bezier(pts, uu, t1, t2)
    if depth > 24:
        return [c]
    tc = _unit(pts[max(0, i - 3)] - pts[min(n - 1, i + 3)])  # 넓게 잡아야 접선이 덜 흔들린다
    return fit_curve(pts[:i + 1], t1, tc, tol, depth + 1) + fit_curve(pts[i:], -tc, t2, tol, depth + 1)


def _smooth(P, sigma, closed):
    """점열을 따라 가우시안 평활(닫힌 곡선은 이어 붙여서, 열린 조각은 양 끝을 고정)."""
    if sigma <= 0 or len(P) < 5:
        return P
    m = int(3 * sigma) + 1
    Q = np.concatenate([P[-m:], P, P[:m]]) if closed else np.concatenate([np.repeat(P[:1], m, 0), P, np.repeat(P[-1:], m, 0)])
    Q = cv2.GaussianBlur(Q.reshape(-1, 1, 2).astype(np.float64), (1, 0), sigmaX=1e-6, sigmaY=sigma,
                         borderType=cv2.BORDER_REPLICATE).reshape(-1, 2)[m:-m]
    if not closed:
        Q[0], Q[-1] = P[0], P[-1]
    return Q


def fit_closed(P, tol, step, k):
    """닫힌 윤곽 점열(조밀) → 부분경로: [시작점, ('L', p) | ('C', c1, c2, p), ...].
    모서리를 먼저 찾아 거기서 끊고, 모서리 사이 구간은 살짝 다듬은 뒤 베지어로 맞춘다(픽셀 계단이 곡선에 남지 않게)."""
    n = len(P)
    if n < 4 * k:
        ap = cv2.approxPolyDP(P.reshape(-1, 1, 2).astype(np.float32), tol, True).reshape(-1, 2)
        return [ap[0]] + [("L", p) for p in ap[1:]] if len(ap) >= 3 else None
    Ps = _smooth(P, k / 3.0, True)
    idx = np.arange(n)
    v1, v2 = Ps - Ps[(idx - k) % n], Ps[(idx + k) % n] - Ps
    cosang = (v1 * v2).sum(1) / (np.linalg.norm(v1, axis=1) * np.linalg.norm(v2, axis=1) + 1e-9)
    ang = np.degrees(np.arccos(np.clip(cosang, -1, 1)))
    corners, taken = [], np.zeros(n, bool)
    for i in np.argsort(-ang):
        if ang[i] < 52:
            break
        if not taken[i]:
            corners.append(int(i))
            taken[(i + np.arange(-k, k + 1)) % n] = True
    is_corner = set(corners)
    breaks = sorted(corners)
    if len(breaks) == 0:
        breaks = [0, n // 2]
    elif len(breaks) == 1:
        breaks = sorted([breaks[0], (breaks[0] + n // 2) % n])
    Pc = _smooth(P, 0.9 * step, True)  # 매끈한 이음점에서 쓸 접선·위치
    sub = None
    for a, b in zip(breaks, breaks[1:] + [breaks[0] + n]):
        ids = np.arange(a, b + 1) % n
        seg = P[ids].astype(np.float64).copy()
        if a % n not in is_corner:
            seg[0] = Pc[a % n]
        if b % n not in is_corner:
            seg[-1] = Pc[b % n]
        seg = _smooth(seg, 1.2 * step, False)
        sel = np.unique(np.concatenate([np.arange(0, len(seg), step), [len(seg) - 1]]))
        pts = seg[sel]
        m = min(len(pts) - 1, 3)
        t1 = _unit(pts[m] - pts[0]) if a % n in is_corner else _unit(Pc[(a + k) % n] - Pc[(a - k) % n])
        t2 = _unit(pts[-1 - m] - pts[-1]) if b % n in is_corner else -_unit(Pc[(b + k) % n] - Pc[(b - k) % n])
        if sub is None:
            sub = [pts[0]]
        for c in fit_curve(pts, t1, t2, tol):
            chord = c[3] - c[0]
            L = float(np.linalg.norm(chord))
            dev = np.abs((c[1:3] - c[0]) @ np.array([-chord[1], chord[0]])) / (L + 1e-9)
            sub.append(("L", c[3]) if dev.max() < 0.35 else ("C", c[1], c[2], c[3]))
    return sub


def trace_mask(M, tol=0.5, up=3):
    """이진 마스크 → 베지어 부분경로 목록(구멍 포함, even-odd)."""
    big = cv2.resize(M.astype(np.float32), None, fx=up, fy=up, interpolation=cv2.INTER_LINEAR)
    big = cv2.GaussianBlur(big, (0, 0), up * 0.6)
    cnts, _ = cv2.findContours((big > 0.5).astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    subs = []
    for c in cnts:
        if len(c) < 3 * up:
            continue
        sp = fit_closed((c.reshape(-1, 2).astype(np.float64) + 0.5) / up, tol, up, int(round(3.5 * up)))
        if sp and len(sp) >= 3:
            subs.append(sp)
    return subs


def flatten_subpath(sp, n=10):
    pts, cur = [np.asarray(sp[0], np.float64)], np.asarray(sp[0], np.float64)
    t = np.linspace(0, 1, n + 1)[1:]
    for seg in sp[1:]:
        if seg[0] == "L":
            pts.append(np.asarray(seg[1], np.float64))
        else:
            pts.extend(_bez(np.array([cur, seg[1], seg[2], seg[3]], np.float64), t))
        cur = np.asarray(seg[-1], np.float64)
    return np.array(pts)


def shift_el(el, dx, dy):
    """ROI 기준 좌표의 요소를 페이지 좌표로."""
    e = dict(el)
    for kx, ky in (("x", "y"), ("cx", "cy"), ("x1", "y1"), ("x2", "y2")):
        if kx in e:
            e[kx], e[ky] = e[kx] + dx, e[ky] + dy
    if "subpaths" in e:
        o = np.array([dx, dy], np.float64)
        e["subpaths"] = [[np.asarray(sp[0]) + o] + [(s[0], *[np.asarray(p) + o for p in s[1:]]) for s in sp[1:]]
                         for sp in e["subpaths"]]
    return e


# ─────────────────────────────────────────────────────────────────────────────
# 7. 출력: 래스터 미리보기(검증용) + SVG
# ─────────────────────────────────────────────────────────────────────────────
def el_bbox(el, W, H, pad=3):
    t = el["type"]
    sw = el.get("sw", 0) if el.get("stroke") is not None else 0
    if t == "rect":
        b = (el["x"], el["y"], el["x"] + el["w"], el["y"] + el["h"])
    elif t == "ellipse":
        r = max(el["rx"], el["ry"]) if el.get("angle") else None
        b = (el["cx"] - (r or el["rx"]), el["cy"] - (r or el["ry"]), el["cx"] + (r or el["rx"]), el["cy"] + (r or el["ry"]))
    elif t == "line":
        b = (min(el["x1"], el["x2"]), min(el["y1"], el["y2"]), max(el["x1"], el["x2"]), max(el["y1"], el["y2"]))
    elif t == "path":
        P = np.vstack([flatten_subpath(sp, 4) for sp in el["subpaths"]])
        b = (*P.min(0), *P.max(0))
    else:
        return 0, 0, W, H
    g = sw / 2 + pad
    return (max(0, int(b[0] - g)), max(0, int(b[1] - g)), min(W, int(math.ceil(b[2] + g))), min(H, int(math.ceil(b[3] + g))))


def render(elements, W, H):
    """요소 목록을 그려 본다(BGR). SVG 렌더러 없이도 결과를 검증·미리보기할 수 있게."""
    canvas = np.full((H, W, 3), 255, np.float32)
    texts = []

    def draw(items):
        for el in items:
            t = el["type"]
            if t == "text":
                texts.append(el)
            elif t == "group":
                draw(el["items"])
            elif t == "image":
                x, y = int(el["x"]), int(el["y"])
                im, al = el["bgr"].astype(np.float32), (el["alpha"].astype(np.float32) / 255)[..., None]
                h, w = im.shape[:2]
                canvas[y:y + h, x:x + w] = canvas[y:y + h, x:x + w] * (1 - al) + im * al
            else:
                _draw_shape(canvas, el, W, H)
    draw(elements)
    out = np.clip(canvas, 0, 255).astype(np.uint8)
    if texts:
        im = Image.fromarray(out[:, :, ::-1].copy())
        d = ImageDraw.Draw(im)
        for el in texts:
            draw_text(d, el, fill=(int(el["bgr"][2]), int(el["bgr"][1]), int(el["bgr"][0])))
        out = np.array(im)[:, :, ::-1].copy()
    return out


def draw_text(d, el, ox=0.0, oy=0.0, fill=255):
    """글자 요소 하나를 SVG 렌더러와 같은 규칙(언어별 조판 + word-spacing)으로 그린다."""
    f = el["face"].pil(el["size"])
    lang, words = el.get("lang"), el["text"].split(" ")
    for i, (w, x) in enumerate(zip(words, word_starts(f, words, lang))):
        t_draw(d, (el["x"] - ox + x + i * el.get("wsp", 0.0), el["y"] - oy), w, f, fill, lang)


def text_alpha(el, x0, y0, x1, y1):
    """글자 요소가 덮는 정도(0..1)를 페이지의 한 구역 [y0:y1, x0:x1]에 그린 것."""
    im = Image.new("L", (x1 - x0, y1 - y0), 0)
    draw_text(ImageDraw.Draw(im), el, x0, y0)
    return np.asarray(im, np.float32) / 255.0


def _draw_shape(canvas, el, W, H):
    x0, y0, x1, y1 = el_bbox(el, W, H)
    if x1 <= x0 or y1 <= y0:
        return
    a = (shape_mask(el, (y1 - y0, x1 - x0), x0, y0).astype(np.float32) / 255)[..., None]
    canvas[y0:y1, x0:x1] = canvas[y0:y1, x0:x1] * (1 - a) + np.float32(el["bgr"]) * a


def _n(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def _path_d(subpaths):
    out = []
    for sp in subpaths:
        out.append(f"M{_n(sp[0][0])} {_n(sp[0][1])}")
        for s in sp[1:]:
            out.append(s[0] + " ".join(f"{_n(p[0])} {_n(p[1])}" for p in s[1:]))
        out.append("Z")
    return "".join(out)


def _png_b64(bgr, alpha=None):
    img = bgr if alpha is None else np.dstack([bgr, alpha])
    return base64.b64encode(cv2.imencode(".png", img)[1].tobytes()).decode()


def svg_shape(el, extra=""):
    t = el["type"]
    if t == "image":
        h, w = el["bgr"].shape[:2]
        return (f'<image x="{_n(el["x"])}" y="{_n(el["y"])}" width="{w}" height="{h}" '
                f'href="data:image/png;base64,{_png_b64(el["bgr"], el["alpha"])}"{extra}/>')
    col = to_hex(el["bgr"])
    paint = (f'fill="none" stroke="{col}" stroke-width="{_n(el["sw"])}"' if el.get("stroke") is not None
             else f'fill="{col}"')
    if t == "rect":
        r = f' rx="{_n(el["rx"])}"' if el.get("rx", 0) > 0.3 else ""
        return f'<rect x="{_n(el["x"])}" y="{_n(el["y"])}" width="{_n(el["w"])}" height="{_n(el["h"])}"{r} {paint}{extra}/>'
    if t == "ellipse":
        rot = f' transform="rotate({_n(el["angle"])} {_n(el["cx"])} {_n(el["cy"])})"' if el.get("angle") else ""
        if abs(el["rx"] - el["ry"]) < 0.02 * max(el["rx"], el["ry"]) + 0.5 and not rot:
            return f'<circle cx="{_n(el["cx"])}" cy="{_n(el["cy"])}" r="{_n((el["rx"] + el["ry"]) / 2)}" {paint}{extra}/>'
        return f'<ellipse cx="{_n(el["cx"])}" cy="{_n(el["cy"])}" rx="{_n(el["rx"])}" ry="{_n(el["ry"])}"{rot} {paint}{extra}/>'
    if t == "line":
        dash = f' stroke-dasharray="{_n(el["dash"][0])} {_n(el["dash"][1])}"' if el.get("dash") else ""
        return (f'<line x1="{_n(el["x1"])}" y1="{_n(el["y1"])}" x2="{_n(el["x2"])}" y2="{_n(el["y2"])}" '
                f'stroke="{col}" stroke-width="{_n(el["sw"])}"{dash}{extra}/>')
    if t == "path":
        return f'<path d="{_path_d(el["subpaths"])}" fill="{col}" fill-rule="evenodd"{extra}/>'
    raise ValueError(t)


def svg_text(el):
    fc = el["face"]
    a = [f'x="{_n(el["x"])}"', f'y="{_n(el["y"])}"', f"font-family={quoteattr(fc.family)}",
         f'font-size="{_n(el["size"])}"', f'font-weight="{fc.weight}"']
    if fc.italic:
        a.append('font-style="italic"')
    if abs(el.get("wsp", 0)) > 0.05:
        a.append(f'word-spacing="{_n(el["wsp"])}"')
    style = []
    if el.get("lang"):
        a.append(f'xml:lang="{el["lang"]}" lang="{el["lang"]}"')
        if el["lang"] == "ko" and not el.get("locl", True):
            style.append("font-feature-settings:'locl' 0, 'calt' 0")
    if not RAQM:
        style.append("font-kerning:none;font-variant-ligatures:none")
    if style:
        a.append('style="' + ";".join(style) + '"')
    a += [f'fill="{to_hex(el["bgr"])}"', 'xml:space="preserve"', f'data-match="{el["score"]:.2f}"']
    return f"<text {' '.join(a)}>{escape(el['text'])}</text>"


def write_svg(path, W, H, page_mm, layers, reference=None, review=None):
    size = (f'width="{page_mm[0]:g}mm" height="{page_mm[1]:g}mm"' if page_mm else f'width="{W}" height="{H}"')
    o = ['<?xml version="1.0" encoding="UTF-8"?>',
         f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
         f'xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" {size} viewBox="0 0 {W} {H}">']

    def layer(id_, label, body, hidden=False):
        o.append(f'<g id="{id_}" inkscape:groupmode="layer" inkscape:label={quoteattr(label)}'
                 + (' style="display:none"' if hidden else "") + ">")
        o.extend(body)
        o.append("</g>")

    layer("paper", "종이(배경)", [f'<rect x="0" y="0" width="{W}" height="{H}" fill="#ffffff"/>'])
    if reference is not None:
        b64 = base64.b64encode(cv2.imencode(".jpg", reference, [cv2.IMWRITE_JPEG_QUALITY, 72])[1].tobytes()).decode()
        layer("reference", "원본 스캔(참고용)", [f'<image x="0" y="0" width="{W}" height="{H}" '
                                           f'preserveAspectRatio="none" href="data:image/jpeg;base64,{b64}"/>'], True)
    for id_, label, els in layers:
        body = []
        for el in els:
            if el["type"] == "group":
                body.append(f'<g id="{el["id"]}">')
                body += ["  " + svg_shape(e) for e in el["items"]]
                body.append("</g>")
            elif el["type"] == "text":
                body.append(svg_text(el))
            else:
                body.append(svg_shape(el))
        layer(id_, label, body)
    if review:
        layer("review", "검토 필요(낮은 일치도)", review, True)
    o.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(o))


# ─────────────────────────────────────────────────────────────────────────────
# 8. 전체 흐름
# ─────────────────────────────────────────────────────────────────────────────
def process(src, out_svg, lang="kor+eng", font_dirs=None, max_side=2400, do_rectify=True,
            embed_reference=True, debug_dir=None, min_text_score=0.78):
    T = {}
    t0 = time.time()
    img = cv2.imread(src)
    if img is None:
        raise SystemExit(f"이미지를 열 수 없습니다: {src}")
    stats = dict(source=os.path.basename(src))

    # 1) 펴기 + 평탄화
    quad = find_page_quad(img) if do_rectify else None
    if quad is not None:
        page, page_mm = rectify(img, quad, max_side)
    else:
        k = min(1.0, max_side / max(img.shape[:2]))
        page, page_mm = (cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA) if k < 1 else img), None
    flat = flatten_illumination(page)
    H, W = flat.shape[:2]
    u = W / 1654.0  # 길이 임계값의 기준 단위(A4 200dpi에서 1)
    stats.update(page_found=quad is not None, size=[W, H], page_mm=page_mm)
    T["rectify"] = time.time() - t0
    log(f"[1] 용지 {'검출' if quad is not None else '미검출(전체 사용)'} → {W}x{H}px")
    if not RAQM:
        log("    (Pillow에 Raqm 조판 기능이 없어 단순 조판으로 진행합니다: 커닝·합자 없이 글자를 놓습니다)")
    stats["raqm"] = bool(RAQM)

    # 2) 색 면 분할
    t = time.time()
    labels, palette, pal_bgr, pal_thick, photos, photo_mask, lab = segment(flat, u)
    paper_ink = 0
    if mark_reversed(labels, len(palette), u):  # 어두운 바탕 위의 흰 글자·무늬
        palette = np.vstack([palette, WHITE[None]])
        pal_bgr, pal_thick = pal_bgr + [np.array([255, 255, 255], np.uint8)], np.append(pal_thick, True)
        paper_ink = len(palette)
    cmap, comps = label_components(labels, len(palette), max(4, int(6 * u * u)))
    T["segment"] = time.time() - t
    log(f"[2] 팔레트 {len(palette)}색: {' '.join(to_hex(c) for c in pal_bgr)} / 사진 영역 {len(photos)}곳")

    # 3) 글자 줄 찾기 → 줄별 잉크 그림 → OCR
    t = time.time()
    big = (comps[:, 4] > 0.09 * H) | (comps[:, 3] > 0.30 * W) | (comps[:, 5] > (0.08 * W) ** 2)
    cand = ~big & (comps[:, 5] >= 6 * u * u)
    cand[0] = False
    pre = []
    for ln in find_text_lines(cmap, comps, cand, H, W, u):
        x0, y0, x1, y1 = ln["box"]
        ids = ln["ids"]
        if len(ids) <= 2:  # 요소 한두 개가 전부 원·사각형이면 글자("O", "ㅁ")보다 도형일 가능성이 높다
            prim = []
            for i in ids:
                _, cx_, cy_, cw_, ch_, _ = comps[i]
                Mi = cv2.copyMakeBorder((cmap[cy_:cy_ + ch_, cx_:cx_ + cw_] == i).astype(np.uint8), 4, 4, 4, 4, cv2.BORDER_CONSTANT)
                e = fit_primitive(Mi, u)
                prim.append(bool(e) and len(e) == 1 and e[0]["type"] != "line")
            if all(prim):
                continue
        if len(ids) >= 3:  # 글자 높이에 비해 아주 길고 납작한 요소(밑줄·괘선)는 글자가 아니다
            hs = np.percentile(comps[ids, 4], 75)
            keep = ~((comps[ids, 3] > 2.2 * hs) & (comps[ids, 4] < 0.25 * hs))
            if keep.sum() >= 2 and not keep.all():
                ids = ids[keep]
                x0, y0 = int(comps[ids, 1].min()), int(comps[ids, 2].min())
                x1, y1 = int((comps[ids, 1] + comps[ids, 3]).max()), int((comps[ids, 2] + comps[ids, 4]).max())
                ln["box"] = (x0, y0, x1, y1)
        lh = y1 - y0
        if len(ids) >= 3:  # 줄 맨 앞에 뚝 떨어져 있는 원·사각형은 글머리표/체크박스로 보고 글자에서 뺀다
            order = ids[np.argsort(comps[ids, 1])]
            f0 = order[0]
            _, fx, fy, fw, fh, _ = comps[f0]
            if comps[order[1:], 1].min() - (fx + fw) >= 0.35 * lh and fh >= 0.25 * lh:
                Mi = cv2.copyMakeBorder((cmap[fy:fy + fh, fx:fx + fw] == f0).astype(np.uint8), 4, 4, 4, 4, cv2.BORDER_CONSTANT)
                e = fit_primitive(Mi, u)
                if e and len(e) == 1 and e[0]["type"] != "line":
                    ids = order[1:]
                    x0, y0 = int(comps[ids, 1].min()), int(comps[ids, 2].min())
                    x1, y1 = int((comps[ids, 1] + comps[ids, 3]).max()), int((comps[ids, 2] + comps[ids, 4]).max())
                    ln["box"], lh = (x0, y0, x1, y1), y1 - y0
        pad = int(0.3 * lh)
        cnt = np.bincount(labels[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad].ravel(), minlength=len(palette) + 1)
        bg = int(cnt.argmax())  # 줄 둘레에서 가장 흔한 라벨 = 바탕(종이 또는 색 면). 글자는 소수다
        ids = ids[comps[ids, 0] != bg]  # 'ㅇ' 안쪽처럼 글자에 갇힌 바탕 조각은 글자가 아니다
        if len(ids) == 0:
            continue
        ink_label = int(np.bincount(comps[ids, 0], weights=comps[ids, 5]).argmax())
        bgc = WHITE if bg == 0 else palette[bg - 1]
        inkc = to_lab(pal_bgr[ink_label - 1][None, None])[0, 0]
        v = inkc - bgc
        # 줄의 잉크와 다른 색 조각(압축 잡음, 옆의 장식)은 뺀다. 같은 잉크가 흐려져 바탕 쪽으로 옅어진 색은 남긴다
        pc = np.vstack([WHITE[None], palette])[comps[ids, 0]] - bgc
        tt = (pc @ v) / max(float(v @ v), 1e-6)
        same = (np.linalg.norm(pc - tt[:, None] * v[None], axis=1) < 30) & (tt > 0.25)
        same |= comps[ids, 0] == ink_label
        if not same.all():
            ids = ids[same]
            x0, y0 = int(comps[ids, 1].min()), int(comps[ids, 2].min())
            x1, y1 = int((comps[ids, 1] + comps[ids, 3]).max()), int((comps[ids, 2] + comps[ids, 4]).max())
            ln["box"] = (x0, y0, x1, y1)
        m = np.isin(cmap[y0:y1, x0:x1], ids)
        # 잉크가 덮은 정도 = (픽셀색 - 바탕색)을 '바탕→잉크' 방향에 투영한 값. 밝기 차이가 충분하면 색상 성분은 덜 믿는다
        # (JPEG와 카메라는 색상 정보를 밝기보다 뭉개서 저장하므로, 색 바탕 위의 색 글자가 실제보다 굵어 보인다)
        wv = v * np.float32([1.0, 0.3, 0.3] if abs(v[0]) > 30 else [1.0, 1.0, 1.0])
        proj = ((lab[y0:y1, x0:x1] - bgc) * wv).sum(2) / max(float(v @ wv), 1e-6)
        # 두꺼운 면에서 얻은 색(또는 검정)이면 그 색이 100%. 가는 획에서 추정한 색이면 관측된 최댓값을 100%로 본다
        full = 1.0
        if not pal_thick[ink_label - 1] and pal_bgr[ink_label - 1].any():
            full = max(1.0, float(np.percentile(proj[m], 97)))
        soft = np.clip(proj / full, 0, 1) * cv2.dilate(m.astype(np.uint8), np.ones((5, 5), np.uint8))
        pre.append(dict(ids=ids, box=ln["box"], bg=bg, ink_label=ink_label, soft=soft.astype(np.float32), mask=m))
    try:
        words, ocr_sheet = ocr_rows([p_["soft"] for p_ in pre], lang) if pre else ([], None)
    except Exception as e:  # Tesseract가 없거나 언어 데이터가 없을 때: 글자도 도형(경로)으로 남긴다
        log(f"    ! OCR 실패({type(e).__name__}: {str(e)[:80]}) → 글자는 경로로 처리합니다")
        words, ocr_sheet = [[] for _ in pre], None
    T["ocr"] = time.time() - t

    # 4) 줄 단위 글꼴 매칭
    t = time.time()
    faces = load_fonts((font_dirs or []) + default_font_dirs())
    T["fonts_index"] = time.time() - t
    t = time.time()
    fits_all = []
    for p_, ws in zip(pre, words):
        text = " ".join(w["text"] for w in ws)
        conf = float(np.mean([w["conf"] for w in ws])) if ws else 0.0
        if conf < 35 or not any(ch.isalnum() for ch in text):
            ws, conf = [], 0.0  # 첫 OCR이 못 읽은 줄: 아래 '다시 읽기'에서 한 번 더 기회를 준다
        f = LineFit(" ".join(w["text"] for w in ws), [w["text"] for w in ws], p_["soft"], p_["box"][0], p_["box"][1])
        f.ids, f.bg, f.ink_label, f.conf, f.mask, f.result = p_["ids"], p_["bg"], p_["ink_label"], conf, p_["mask"], None
        f.amp_free = bool(not pal_thick[f.ink_label - 1] and pal_bgr[f.ink_label - 1].any())
        fits_all.append(f)
    fits = [f for f in fits_all if f.text]
    log(f"[3] 글자 줄 후보 {len(pre)}개 → 첫 OCR로 읽힌 줄 {len(fits)}개")
    global KO_LOCL
    KO_LOCL = True
    match_fonts(fits, faces, stats, estimate_blur(lab, labels, palette))
    decide_locl(fits)
    stats["ko_locl"] = KO_LOCL
    T["font_match"] = time.time() - t
    n_pairs = len(fits) * len(faces)
    log(f"[4] 글꼴 대입: 줄 {len(fits)}개 × 후보 글꼴 {len(faces)}개 = {n_pairs}쌍 중, 작게 그려 훑기 {stats['coarse_evals']}회"
        f"(전체 글꼴과 견준 줄은 {stats['full_scans']}개) + 제 크기 정밀 비교 {stats['fine_evals']}쌍 ({T['font_match']:.1f}s)")
    t = time.time()
    rescue(fits_all, faces, lang, stats)
    fits = [f for f in fits_all if f.text and f.result]
    harmonize(fits)
    T["reread"] = time.time() - t
    if stats.get("reread_lines"):
        log(f"    다시 읽기: 미심쩍은 {stats['reread_lines']}줄을 다른 설정으로 재인식 → {stats['reread_changed']}줄 교체 ({T['reread']:.1f}s)")

    texts, review, text_report = [], [], []
    for f in fits:
        r = f.result
        n_ch = len(f.text.replace(" ", ""))
        # 글자 수가 적을수록 우연히 맞기 쉽다(주황 삼각형이 굵은 'A'와 꽤 닮았다) → 기준을 높인다
        ok = r is not None and r["score"] >= (min_text_score if n_ch >= 4 else 0.88 if n_ch == 3 else 0.93)
        text_report.append(dict(text=f.text, ocr_conf=round(f.conf), accepted=bool(ok),
                                font=r["face"].label if r else None, score=round(r["score"], 3) if r else None,
                                size_px=round(r["size"], 1) if r else None,
                                word_spacing_px=round(r["wsp"], 2) if r else None,
                                alternatives=[[round(s, 3), n] for s, n in getattr(f, "runner_up", [])]))
        if not ok:  # 글자로 확신 못 하면 지우지 않는다 → 아래에서 도형(경로)으로 남는다. OCR 추정값은 검토 레이어에 적어 둔다
            review.append(f'<rect x="{f.x0 - 3}" y="{f.y0 - 3}" width="{f.W + 6}" height="{f.H + 6}" fill="none" stroke="#1971c2" '
                          f'stroke-width="2" stroke-dasharray="6 4"><title>글자로 바꾸지 않음(경로로 유지). OCR 추정: {escape(f.text)}</title></rect>')
            continue
        el = dict(type="text", text=f.text, face=r["face"], size=r["size"], x=r["x"], y=r["y"],
                  wsp=r["wsp"], bgr=pal_bgr[f.ink_label - 1], score=r["score"],
                  lang=f.lang, locl=KO_LOCL, fit=f)
        if not pal_thick[f.ink_label - 1] and pal_bgr[f.ink_label - 1].any():
            # 가는 획만 있는 색(회색 본문 등)은 흐려져서 실제보다 옅게 관측된다. 이제 글꼴을 알았으니, 맞춘 글자를 같은 흐림으로
            # 그렸을 때 잉크가 가장 많이 덮이는 자리(a%)의 관측색에서 '100% 덮였다면'의 색을 되짚는다
            al = _blur(text_alpha(el, f.x0, f.y0, f.x0 + f.W, f.y0 + f.H), LineFit.sigma)
            top = al >= max(0.5, 0.92 * float(al.max()))
            if top.sum() >= 12:
                bgc_ = np.float32([255, 255, 255]) if f.bg == 0 else pal_bgr[f.bg - 1].astype(np.float32)
                obs = flat[f.y0:f.y0 + f.H, f.x0:f.x0 + f.W][top].astype(np.float32).mean(0)
                ink_ = np.clip(bgc_ + (obs - bgc_) / float(al[top].mean()), 0, 255)
                el["bgr"] = snap_color(to_lab(ink_.astype(np.uint8)[None, None])[0, 0], True)
        texts.append(el)
        weak = f.weak_cells(r)
        if r["score"] < 0.9 or f.conf < 60:
            review.append(f'<rect x="{f.x0 - 3}" y="{f.y0 - 3}" width="{f.W + 6}" height="{f.H + 6}" fill="none" '
                          f'stroke="#e8590c" stroke-width="2"><title>{escape(f.text)} (일치 {r["score"]:.2f}, OCR {f.conf:.0f})</title></rect>')
        for j, a, b, sc_ in weak:  # 줄은 맞아도 이 글자는 의심스럽다
            review.append(f'<rect x="{f.x0 + a:.1f}" y="{f.y0 - 2}" width="{max(4.0, b - a):.1f}" height="{f.H + 4}" '
                          f'fill="#e03131" fill-opacity="0.25"><title>글자 \'{escape(f.text[j])}\' 일치 {sc_:.2f}</title></rect>')
        text_report[-1]["weak_chars"] = [[f.text[j], round(sc_, 2)] for j, a, b, sc_ in weak]
        sel = np.isin(cmap[f.y0:f.y0 + f.H, f.x0:f.x0 + f.W], f.ids)
        labels[f.y0:f.y0 + f.H, f.x0:f.x0 + f.W][sel] = f.bg  # 글자가 있던 자리는 바탕(종이/색 면)으로 메운다
    # 글자가 너무 작게 찍혔으면 닮은 글자(활/할, 천/전)를 겹쳐 보기로 가려낼 수 없다. 점수는 높게 나오므로 따로 알린다
    hs = sorted(el["fit"].H for el in texts)
    stats["median_text_px"] = int(hs[len(hs) // 2]) if hs else None
    stats["low_resolution"] = bool(hs and hs[len(hs) // 2] < 28)
    if stats["low_resolution"]:
        log(f"    ! 글자가 작게 찍혔습니다(줄 높이 중앙값 {hs[len(hs) // 2]}px). 닮은 글자의 오인식을 걸러내기 어려우니 "
            "더 가까이서, 또는 더 높은 해상도로 찍어 주세요")
    # 같은 잉크로 보이는 줄들은 글자색을 하나로 통일한다(줄마다 따로 되짚은 색은 조금씩 다르다)
    by_ink = defaultdict(list)
    for el in texts:
        by_ink[el["fit"].ink_label].append(el)
    for els in by_ink.values():
        cols = np.array([to_lab(np.asarray(e["bgr"], np.uint8)[None, None])[0, 0] for e in els])
        wts = np.array([len(e["text"]) for e in els], np.float32)
        med = cols[np.argsort(cols[:, 0])[np.searchsorted(np.cumsum(wts[np.argsort(cols[:, 0])]), wts.sum() / 2)]]
        near = np.linalg.norm(cols - med, axis=1) < 10
        if near.sum() >= 2:
            c = snap_color((cols[near] * wts[near, None]).sum(0) / wts[near].sum(), True)
            for e, n_ in zip(els, near):
                if n_:
                    e["bgr"] = c
    # 글자로 확정한 줄에 붙어 있던 부스러기(줄에 끼지 못한 i의 점·쉼표, 획 가장자리의 번짐 조각)는
    # 새로 그린 글자가 이미 설명한다 → 따로 도형으로 남기지 않고 지운다
    if texts:
        cm2, cp2 = label_components(labels, len(palette), 1)
        for el in texts:
            f = el["fit"]
            g = int(0.4 * f.H) + 3
            X0, Y0, X1, Y1 = max(0, f.x0 - g), max(0, f.y0 - g), min(W, f.x0 + f.W + g), min(H, f.y0 + f.H + g)
            cover = cv2.dilate((text_alpha(el, X0, Y0, X1, Y1) > 0.25).astype(np.uint8), np.ones((5, 5), np.uint8))
            roi = cm2[Y0:Y1, X0:X1]
            ids, cnt = np.unique(roi[cover > 0], return_counts=True)
            for i, c in zip(ids, cnt):
                if i == 0 or cp2[i][0] == f.bg:
                    continue
                _, cx, cy, cw, ch, area = cp2[i]
                if c >= 0.6 * area and max(cw, ch) <= 0.8 * f.H and cx >= X0 and cy >= Y0 and cx + cw <= X1 and cy + ch <= Y1:
                    labels[Y0:Y1, X0:X1][roi == i] = f.bg
    log(f"    글자로 확정 {len(texts)}/{len(pre)}줄" + (f", 낱글자 대입으로 고친 글자 {stats['chars_fixed']}개" if stats.get("chars_fixed") else ""))

    # 5) 남은 요소 → 도형 / 경로
    t = time.time()
    cmap, comps = label_components(labels, len(palette), max(4, int(8 * u * u)))
    Lp = palette[:, 0].copy() if len(palette) else np.zeros(0)
    if paper_ink:
        Lp[paper_ink - 1] = -1.0  # 흰 잉크는 항상 맨 위에 그린다
    n_cl, cl_map = cv2.connectedComponents((cmap > 0).astype(np.uint8), 8)
    k3 = np.ones((3, 3), np.uint8)
    # 무엇이 무엇 위에 놓였나: 넓고 꽉 찬 색 면(남색 띠)에 둘러싸인 요소(그 위의 노란 글자)는 그 면 '위'에 그려야 한다.
    # 반대로 가는 외곽선에 둘러싸인 면(검은 테두리 안의 노란 채움)은 종전대로 외곽선 '밑'에 깐다.
    nC, solid = len(comps), {}

    def solidity(j):
        if j not in solid:
            _, jx, jy, jw, jh, ja = comps[j]
            Mj = (cmap[jy:jy + jh, jx:jx + jw] == j).astype(np.uint8)
            cn, _ = cv2.findContours(Mj, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            fl = np.zeros_like(Mj)
            cv2.drawContours(fl, cn, -1, 1, cv2.FILLED)
            solid[j] = ja / max(1, int(fl.sum()))
        return solid[j]

    parent = np.zeros(nC, np.int32)
    for i in range(1, nC):
        _, x, y, w, h, area = comps[i]
        X0, Y0, X1, Y1 = max(0, x - 1), max(0, y - 1), min(W, x + w + 1), min(H, y + h + 1)
        Mi = (cmap[Y0:Y1, X0:X1] == i).astype(np.uint8)
        ring = cmap[Y0:Y1, X0:X1][cv2.dilate(Mi, k3) > Mi]
        if len(ring):
            ids, cnt = np.unique(ring, return_counts=True)
            j = int(ids[cnt.argmax()])
            if j and cnt.max() >= 0.7 * len(ring) and comps[j][5] > 3 * area and solidity(j) >= 0.5:
                parent[i] = j
    level = np.zeros(nC, np.int32)
    for i in range(1, nC):
        j, d = parent[i], 0
        while j and d < 8:
            j, d = parent[j], d + 1
        level[i] = d
    items = []  # (cluster, lightness, area, [elements], is_primitive, level)
    for i in range(1, len(comps)):
        k, x, y, w, h, area = comps[i]
        p = 4
        X0, Y0, X1, Y1 = max(0, x - p), max(0, y - p), min(W, x + w + p), min(H, y + h + p)
        M = (cmap[Y0:Y1, X0:X1] == i).astype(np.uint8)
        lr = labels[Y0:Y1, X0:X1]
        # 이 요소 밑으로 면이 이어진다고 볼 것들: (a) 더 진한 이웃(외곽선 등)  (b) 이 면 위에 놓인 것(level이 더 높은 요소)
        over = (lr > 0) & (np.concatenate([[1e9], Lp])[lr] < Lp[k - 1] - 8) & (lr != paper_ink) if level[i] == 0 else np.zeros_like(M, bool)
        over |= level[cmap[Y0:Y1, X0:X1]] > level[i]
        if k != paper_ink and over.any():  # 구멍을 메우고 1px 겹친다
            cn, _ = cv2.findContours(M, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            filled = np.zeros_like(M)
            cv2.drawContours(filled, cn, -1, 1, cv2.FILLED)
            hn, hcc, hst, _ = cv2.connectedComponentsWithStats(filled & (1 - M), 4)
            for j in range(1, hn):
                hm = hcc == j
                if over[hm].mean() > 0.85:
                    M[hm] = 1
            M |= cv2.dilate(M, np.ones((5, 5), np.uint8)) & over.astype(np.uint8)
        els = fit_primitive(M, u)
        prim = els is not None
        if not prim:
            subs = trace_mask(M)
            els = [dict(type="path", subpaths=subs)] if subs else []
        els = [dict(shift_el(e, X0, Y0), bgr=pal_bgr[k - 1]) for e in els]
        if els:
            cl = int(cl_map[y, x + int(np.argmax(cmap[y, x:x + w] == i))])
            items.append([cl, float(Lp[k - 1]), int(area), els, prim, int(level[i])])
    by_cluster = defaultdict(list)
    for it in items:
        by_cluster[it[0]].append(it)
    shapes, groups_src = [], []
    for cl, its in by_cluster.items():
        if len(its) == 1 and its[0][4]:
            shapes += its[0][3]
        else:
            groups_src.append((cl, its))
    # 가까이 있는 조각들은 한 일러스트로 묶는다
    illus = []
    if groups_src:
        gm = np.isin(cl_map, [cl for cl, _ in groups_src]).astype(np.uint8)
        r = int(round(12 * u))
        gm = cv2.dilate(gm, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
        _, gcc = cv2.connectedComponents(gm, 8)
        merged = defaultdict(list)
        for cl, its in groups_src:
            yy, xx = np.nonzero(cl_map == cl)
            merged[int(gcc[yy[0], xx[0]])] += its
        for gi, (_, its) in enumerate(sorted(merged.items(), key=lambda kv: min(e_y(i[3][0]) for i in kv[1])), 1):
            its.sort(key=lambda it: (it[5], -it[1], -it[2]))  # 바탕 면 먼저. 같은 층에서는 밝은 면 먼저, 진한 외곽선이 위로
            illus.append(dict(type="group", id=f"illust-{gi}", items=[e for it in its for e in it[3]]))
    shapes = merge_dashes(shapes)
    shapes.sort(key=lambda e: -e_light(e) if e_light(e) < 254 else 1)
    images = []
    for i, (x, y, w, h) in enumerate(photos, 1):
        images.append(dict(type="image", id=f"photo-{i}", x=x, y=y, bgr=flat[y:y + h, x:x + w].copy(),
                           alpha=(photo_mask[y:y + h, x:x + w] * 255).astype(np.uint8)))
    T["shapes"] = time.time() - t
    kinds = defaultdict(int)
    for e in shapes:
        kinds[e["type"] + ("(선)" if e.get("stroke") is not None and e["type"] != "line" else "")] += 1
    log(f"[5] 기본 도형 {dict(kinds)} / 일러스트 묶음 {len(illus)}개(경로 {sum(len(g['items']) for g in illus)}개) / 이미지 {len(images)}개")

    # 6) 출력 + 검증
    layers = [("images", "이미지 조각(벡터화 안 함)", images), ("shapes", "도형", shapes),
              ("illustrations", "일러스트", illus), ("text", "글자", texts)]
    write_svg(out_svg, W, H, page_mm, layers, flat if embed_reference else None, review)
    preview = render(images + shapes + illus + texts, W, H)
    diff = cv2.absdiff(cv2.GaussianBlur(flat, (0, 0), 1.5), cv2.GaussianBlur(preview, (0, 0), 1.5)).max(2)
    stats.update(mean_abs_diff=round(float(diff.mean()), 2), pixels_off=round(float((diff > 60).mean()) * 100, 3),
                 texts=text_report, palette=[to_hex(c) for c in pal_bgr],
                 counts=dict(text_lines=len(texts), shapes=len(shapes), illustration_groups=len(illus),
                             illustration_paths=sum(len(g["items"]) for g in illus), images=len(images)),
                 seconds={k: round(v, 2) for k, v in T.items()}, total_sec=round(time.time() - t0, 2))
    base = os.path.splitext(out_svg)[0]
    heat = cv2.applyColorMap(np.clip(diff.astype(np.float32) * 2, 0, 255).astype(np.uint8), cv2.COLORMAP_INFERNO)
    cv2.imwrite(base + "_compare.jpg", np.hstack([flat, preview, heat]), [cv2.IMWRITE_JPEG_QUALITY, 85])
    cv2.imwrite(base + "_preview.png", preview)
    with open(base + "_report.json", "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)
        cv2.imwrite(os.path.join(debug_dir, "1_flat.png"), flat)
        vis = np.full((H, W, 3), 255, np.uint8)
        for k, c in enumerate(pal_bgr, 1):
            vis[labels == k] = c
        cv2.imwrite(os.path.join(debug_dir, "2_labels_after_text_removal.png"), vis)
        if ocr_sheet is not None:
            cv2.imwrite(os.path.join(debug_dir, "3_ocr_input.png"), ocr_sheet)
    log(f"[6] {out_svg} 저장 — 평균 차이 {stats['mean_abs_diff']}/255, 크게 다른 픽셀 {stats['pixels_off']}% (총 {stats['total_sec']}s)")
    return stats


def merge_dashes(shapes):
    """한 줄로 늘어선 같은 색·같은 크기의 작은 토막들은 점선 하나로 바꾼다(토막 48개보다 점선 1개가 편집하기 좋다)."""
    def box(e):
        if e.get("stroke") is not None and e["type"] != "line" or e.get("angle"):
            return None
        if e["type"] == "rect":
            return e["x"], e["y"], e["x"] + e["w"], e["y"] + e["h"]
        if e["type"] == "ellipse":
            return e["cx"] - e["rx"], e["cy"] - e["ry"], e["cx"] + e["rx"], e["cy"] + e["ry"]
        if e["type"] == "line" and not e.get("dash"):
            hw = e["sw"] / 2
            if abs(e["y1"] - e["y2"]) < 0.5:
                return min(e["x1"], e["x2"]), e["y1"] - hw, max(e["x1"], e["x2"]), e["y1"] + hw
            if abs(e["x1"] - e["x2"]) < 0.5:
                return e["x1"] - hw, min(e["y1"], e["y2"]), e["x1"] + hw, max(e["y1"], e["y2"])
        return None

    out, used = [], set()
    for horiz in (True, False):
        items = []
        for i, e in enumerate(shapes):
            b = box(e)
            if i in used or b is None:
                continue
            x0, y0, x1, y1 = b if horiz else (b[1], b[0], b[3], b[2])  # 세로 점선은 축을 바꿔 같은 식으로
            if x1 - x0 >= 1.3 * (y1 - y0) and y1 - y0 <= 12:
                items.append((tuple(int(v) for v in e["bgr"]), x0, (y0 + y1) / 2, x1 - x0, y1 - y0, i))
        items.sort(key=lambda t: (t[0], round(t[2] / 3), t[1]))
        run = []

        def flush():
            if len(run) >= 4:
                ln = float(np.median([t[3] for t in run]))
                pitch = float(np.median(np.diff([t[1] for t in run])))
                a, b_, c = run[0][1], run[-1][1] + run[-1][3], float(np.median([t[2] for t in run]))
                sw = float(np.median([t[4] for t in run]))
                e = dict(type="line", sw=sw, stroke=1, bgr=shapes[run[0][5]]["bgr"], dash=(ln, max(1.0, pitch - ln)))
                e.update(dict(x1=a, y1=c, x2=b_, y2=c) if horiz else dict(x1=c, y1=a, x2=c, y2=b_))
                out.append(e)
                used.update(t[5] for t in run)
        for t in items:
            if run:
                p = run[-1]
                ok = (t[0] == p[0] and abs(t[2] - p[2]) <= 2.5 and 0.6 < t[3] / p[3] < 1.6 and 0 < t[1] - (p[1] + p[3]) < 2.5 * p[3])
                if ok and len(run) >= 2:  # 간격이 일정해야 점선이다
                    ok = abs((t[1] - p[1]) - (p[1] - run[-2][1])) <= 0.25 * (p[1] - run[-2][1]) + 2
                if not ok:
                    flush()
                    run = []
            run.append(t)
        flush()
    return [e for i, e in enumerate(shapes) if i not in used] + out


def e_y(el):
    for k in ("y", "cy", "y1"):
        if k in el:
            return el[k]
    return float(el["subpaths"][0][0][1])


def e_light(el):
    b, g, r = (int(v) for v in el["bgr"])
    return 0.114 * b + 0.587 * g + 0.299 * r


def main():
    ap = argparse.ArgumentParser(description="문서 사진 → 편집 가능한 SVG")
    ap.add_argument("image")
    ap.add_argument("-o", "--out", help="출력 SVG 경로 (기본: 입력이름.svg)")
    ap.add_argument("--lang", default="kor+eng", help="Tesseract 언어 (기본 kor+eng)")
    ap.add_argument("--fonts", action="append", default=[], help="추가 글꼴 폴더(여러 번 지정 가능)")
    ap.add_argument("--max-side", type=int, default=2400)
    ap.add_argument("--no-rectify", action="store_true", help="용지 검출·원근 보정 생략(이미 스캔본일 때)")
    ap.add_argument("--no-reference", action="store_true", help="원본 스캔을 참고 레이어로 넣지 않음")
    ap.add_argument("--debug", help="중간 결과 이미지를 저장할 폴더")
    ap.add_argument("--min-text-score", type=float, default=0.78,
                    help="이 일치도 미만인 줄은 글자로 바꾸지 않고 원래 모양(경로)으로 둔다 (기본 0.78, 높일수록 보수적)")
    a = ap.parse_args()
    out = a.out or os.path.splitext(a.image)[0] + ".svg"
    process(a.image, out, a.lang, a.fonts, a.max_side, not a.no_rectify, not a.no_reference, a.debug, a.min_text_score)


if __name__ == "__main__":
    main()

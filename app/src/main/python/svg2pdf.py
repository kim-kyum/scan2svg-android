"""scan2svg가 만든 SVG를 PDF로 바꾼다(외부 프로그램 없이, 파이썬 + fontTools만으로).

- 글자는 글자 그대로 넣는다: 쓰인 글자만 남긴 글꼴을 PDF 안에 넣으므로 검색·복사가 되고 어느 기기에서나 똑같이 보인다.
- 도형·곡선은 벡터 그대로, 사진은 JPEG 그대로 넣는다.
- 숨긴 레이어(원본 스캔, 검토 필요)는 넣지 않는다.

scan2svg가 쓰는 SVG 기능(rect·circle·ellipse·line·path·polygon·polyline·image·text, g, transform, 불투명도)을 다룬다.
일반 SVG 변환기가 아니다.

사용:  python svg2pdf.py result.svg result.pdf [--fonts DIR ...]
코드:  svg_to_pdf("result.svg", "result.pdf", faces)   # faces = scan2svg.load_fonts(...)의 결과
"""
import base64
import io
import math
import os
import re
import sys
import zlib
import xml.etree.ElementTree as ET

SVG_NS = "{http://www.w3.org/2000/svg}"
XLINK = "{http://www.w3.org/1999/xlink}href"


# ───────────────────────── 작은 PDF 쓰기 도구 ─────────────────────────
class _Pdf:
    def __init__(self):
        self.objs = [None]  # 1번부터

    def reserve(self):
        self.objs.append(None)
        return len(self.objs) - 1

    def put(self, body, num=None):
        if isinstance(body, str):
            body = body.encode("latin-1")
        if num is None:
            self.objs.append(body)
            return len(self.objs) - 1
        self.objs[num] = body
        return num

    def stream(self, data, extra="", num=None, compress=True):
        if compress:
            data = zlib.compress(data, 6)
            extra = "/Filter /FlateDecode " + extra
        head = f"<< {extra}/Length {len(data)} >>\nstream\n".encode("latin-1")
        return self.put(head + data + b"\nendstream", num)

    def write(self, path, root):
        out = io.BytesIO()
        out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offs = [0]
        for i in range(1, len(self.objs)):
            offs.append(out.tell())
            out.write(f"{i} 0 obj\n".encode() + self.objs[i] + b"\nendobj\n")
        x = out.tell()
        out.write(f"xref\n0 {len(self.objs)}\n0000000000 65535 f \n".encode())
        for o in offs[1:]:
            out.write(f"{o:010d} 00000 n \n".encode())
        out.write(f"trailer\n<< /Size {len(self.objs)} /Root {root} 0 R >>\nstartxref\n{x}\n%%EOF\n".encode())
        with open(path, "wb") as f:
            f.write(out.getvalue())


def _pdfstr(s):
    """PDF 문자열(UTF-16BE, BOM) — 문서 정보에 한글을 넣을 때."""
    return "<FEFF" + s.encode("utf-16-be").hex().upper() + ">"


def _f(v):
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# ───────────────────────── 글꼴 넣기 ─────────────────────────
class _EmbeddedFont:
    """한 글꼴(면)을 Type0/Identity-H로 넣는다. 쓰인 글자만 남긴다."""

    def __init__(self, face, name):
        from fontTools.ttLib import TTFont
        self.face, self.name = face, name
        tt = TTFont(face.path, fontNumber=getattr(face, "index", 0) or 0, lazy=True)
        # 가변 글꼴은 쓰인 글자만 남긴 뒤(embed) 굵기를 고정한다 — 순서를 바꾸면 몇 초씩 걸린다
        self.var = dict(zip([a.axisTag for a in tt["fvar"].axes], face.var)) if getattr(face, "var", None) and "fvar" in tt else None
        self.tt = tt
        self.cmap = tt.getBestCmap() or {}
        self.order = tt.getGlyphOrder()
        self.gid = {g: i for i, g in enumerate(self.order)}
        self.cff = "CFF " in tt
        self.cid_keyed = self.cff and hasattr(tt["CFF "].cff.topDictIndex[0], "ROS")
        self.upm = tt["head"].unitsPerEm
        self.used = {}  # code -> (glyph name, 글자)

    def code(self, ch):
        g = self.cmap.get(ord(ch))
        if g is None:
            return None
        if self.cid_keyed:
            c = int(g[3:]) if g.startswith("cid") else self.gid[g]
        else:
            c = self.gid[g]
        if c not in self.used:
            self.used[c] = (g, ch)
        return c

    def advance(self, code):
        g = self.used[code][0]
        return self.tt["hmtx"][g][0] * 1000.0 / self.upm

    def embed(self, pdf):
        from fontTools import subset
        tt = self.tt
        opt = subset.Options()
        opt.retain_gids = not self.cid_keyed
        opt.notdef_outline = True
        opt.hinting = False
        opt.layout_features = []
        opt.name_IDs = [1, 2, 3, 4, 6]
        opt.drop_tables += ["DSIG", "GSUB", "GPOS", "GDEF", "BASE", "JSTF", "MATH", "VORG", "vhea", "vmtx", "kern"]
        sub = subset.Subsetter(opt)
        sub.populate(glyphs=sorted({g for g, _ in self.used.values()} | {".notdef"} & set(self.order)))
        sub.subset(tt)
        if self.var:
            from fontTools.varLib import instancer
            buf = io.BytesIO()
            tt.save(buf)
            buf.seek(0)
            from fontTools.ttLib import TTFont
            tt = instancer.instantiateVariableFont(TTFont(buf), self.var)
            self.tt = tt
        buf = io.BytesIO()
        tt.save(buf)
        data = buf.getvalue()

        ps = re.sub(r"[^A-Za-z0-9\-]", "", (getattr(self.face, "psname", "") or self.face.family)) or "Font"
        tag = "".join(chr(65 + (hash((ps, self.name, i)) % 26)) for i in range(6))
        base = f"{tag}+{ps}"
        head, hhea = tt["head"], tt["hhea"]
        os2 = tt["OS/2"] if "OS/2" in tt else None
        k = 1000.0 / self.upm
        cap = getattr(os2, "sCapHeight", 0) if os2 else 0
        flags = 4 | (64 if getattr(self.face, "italic", False) else 0)
        if self.cff:
            # CFF 글꼴은 OpenType 껍데기 없이 CFF 표만 넣는다(CIDFontType0C). 뷰어 호환성이 가장 좋다.
            ff = pdf.stream(tt.getTableData("CFF "), "/Subtype /CIDFontType0C ")
            ffkey = f"/FontFile3 {ff} 0 R"
        else:
            ff = pdf.stream(data, f"/Length1 {len(data)} ")
            ffkey = f"/FontFile2 {ff} 0 R"
        desc = pdf.put(
            f"<< /Type /FontDescriptor /FontName /{base} /Flags {flags} "
            f"/FontBBox [{_f(head.xMin * k)} {_f(head.yMin * k)} {_f(head.xMax * k)} {_f(head.yMax * k)}] "
            f"/ItalicAngle {_f(tt['post'].italicAngle)} /Ascent {_f(hhea.ascent * k)} /Descent {_f(hhea.descent * k)} "
            f"/CapHeight {_f((cap or hhea.ascent) * k)} /StemV 80 {ffkey} >>")
        codes = sorted(self.used)
        w = " ".join(f"{c} [{_f(self.advance(c))}]" for c in codes)
        if self.cff:
            cid = pdf.put(f"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /{base} "
                          f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                          f"/FontDescriptor {desc} 0 R /DW 1000 /W [{w}] >>")
        else:
            cid = pdf.put(f"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /{base} "
                          f"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                          f"/FontDescriptor {desc} 0 R /DW 1000 /W [{w}] /CIDToGIDMap /Identity >>")
        lines = []
        for c in codes:
            u = self.used[c][1].encode("utf-16-be").hex().upper()
            lines.append(f"<{c:04X}> <{u}>")
        cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
                "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
                "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n")
        for i in range(0, len(lines), 100):
            chunk = lines[i:i + 100]
            cmap += f"{len(chunk)} beginbfchar\n" + "\n".join(chunk) + "\nendbfchar\n"
        cmap += "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
        tu = pdf.stream(cmap.encode("ascii"))
        return pdf.put(f"<< /Type /Font /Subtype /Type0 /BaseFont /{base} /Encoding /Identity-H "
                       f"/DescendantFonts [{cid} 0 R] /ToUnicode {tu} 0 R >>")


class FontResolver:
    """SVG의 font-family·font-weight·font-style로 글꼴 파일(면)을 찾는다."""

    def __init__(self, faces):
        self.faces = list(faces or [])
        self.cache = {}

    def find(self, family, weight, italic, text):
        key = (family, weight, italic)
        if key not in self.cache:
            fam = [f for f in self.faces if f.family.lower() == (family or "").lower()]
            self.cache[key] = sorted(fam, key=lambda f: (abs(f.weight - weight) + (400 if f.italic != italic else 0)))
        for f in self.cache[key]:
            if all(f.supports(ch) for ch in set(text) if not ch.isspace()):
                return f
        # 같은 이름이 없으면 그 글자들을 그릴 수 있는 아무 글꼴(굵기 가까운 것)
        for f in sorted(self.faces, key=lambda f: (abs(f.weight - weight), f.italic != italic)):
            if all(f.supports(ch) for ch in set(text) if not ch.isspace()):
                self.cache[key] = [f]
                return f
        return None


# ───────────────────────── SVG 읽기 ─────────────────────────
def _style(el):
    d = {}
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            d[k.strip()] = v.strip()
    return d


def _attr(el, name, inherited, default=None):
    st = _style(el)
    if name in st:
        return st[name]
    v = el.get(name)
    if v is not None:
        return v
    return inherited.get(name, default)


def _color(v):
    if not v or v in ("none", "transparent"):
        return None
    v = v.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    m = re.match(r"rgb\(\s*([\d.]+)%?\s*,\s*([\d.]+)%?\s*,\s*([\d.]+)%?\s*\)", v)
    if m:
        return tuple(min(1.0, float(x) / 255.0) for x in m.groups())
    return {"black": (0, 0, 0), "white": (1, 1, 1), "red": (1, 0, 0), "blue": (0, 0, 1),
            "green": (0, 0.5, 0), "gray": (0.5, 0.5, 0.5), "grey": (0.5, 0.5, 0.5)}.get(v.lower(), (0, 0, 0))


def _num(v, default=0.0):
    if v is None:
        return default
    m = re.match(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)", str(v))
    return float(m.group(1)) if m else default


def _transform(v):
    """transform 속성 → 행렬 (a b c d e f)."""
    M = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", v or ""):
        a = [float(x) for x in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", args)]
        if name == "matrix" and len(a) == 6:
            T = tuple(a)
        elif name == "translate":
            T = (1, 0, 0, 1, a[0], a[1] if len(a) > 1 else 0)
        elif name == "scale":
            T = (a[0], 0, 0, a[1] if len(a) > 1 else a[0], 0, 0)
        elif name == "rotate":
            r = math.radians(a[0])
            c, s = math.cos(r), math.sin(r)
            T = (c, s, -s, c, 0, 0)
            if len(a) == 3:
                cx, cy = a[1], a[2]
                T = (c, s, -s, c, cx - c * cx + s * cy, cy - s * cx - c * cy)
        elif name == "skewX":
            T = (1, 0, math.tan(math.radians(a[0])), 1, 0, 0)
        elif name == "skewY":
            T = (1, math.tan(math.radians(a[0])), 0, 1, 0, 0)
        else:
            continue
        M = _mul(M, T)
    return M


def _mul(A, B):
    a, b, c, d, e, f = A
    A2, B2, C2, D2, E2, F2 = B
    return (a * A2 + c * B2, b * A2 + d * B2, a * C2 + c * D2, b * C2 + d * D2, a * E2 + c * F2 + e, b * E2 + d * F2 + f)


_TOK = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _arc_to_beziers(x1, y1, rx, ry, phi, fa, fs, x2, y2):
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    phi = math.radians(phi)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    rx, ry = abs(rx), abs(ry)
    lam = x1p ** 2 / rx ** 2 + y1p ** 2 / ry ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if fa == fs:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not fs and dt > 0:
        dt -= 2 * math.pi
    elif fs and dt < 0:
        dt += 2 * math.pi
    n = max(1, int(math.ceil(abs(dt) / (math.pi / 2))))
    d = dt / n
    out = []
    k = 4 / 3 * math.tan(d / 4)
    for i in range(n):
        a1, a2 = t1 + i * d, t1 + (i + 1) * d
        p = []
        for (x, y) in ((math.cos(a1) - k * math.sin(a1), math.sin(a1) + k * math.cos(a1)),
                       (math.cos(a2) + k * math.sin(a2), math.sin(a2) - k * math.cos(a2)),
                       (math.cos(a2), math.sin(a2))):
            x, y = x * rx, y * ry
            p.append((cp * x - sp * y + cx, sp * x + cp * y + cy))
        out.append(tuple(p))
    return out


def _path_ops(d):
    """SVG path d → PDF 경로 연산자 문자열."""
    t = _TOK.findall(d or "")
    i, cmd = 0, None
    x = y = sx = sy = 0.0
    lcx = lcy = None  # 직전 3차 제어점 (S용)
    lqx = lqy = None  # 직전 2차 제어점 (T용)
    out = []

    def num():
        nonlocal i
        v = float(t[i])
        i += 1
        return v

    while i < len(t):
        if re.match(r"[A-Za-z]", t[i]):
            cmd = t[i]
            i += 1
            if cmd in "Zz":
                out.append("h")
                x, y = sx, sy
                lcx = lqx = None
                continue
        if cmd is None:
            break
        rel = cmd.islower()
        C = cmd.upper()
        ox, oy = (x, y) if rel else (0.0, 0.0)
        try:
            if C == "M":
                x, y = ox + num(), oy + num()
                sx, sy = x, y
                out.append(f"{_f(x)} {_f(y)} m")
                cmd = "l" if rel else "L"
                lcx = lqx = None
            elif C == "L":
                x, y = ox + num(), oy + num()
                out.append(f"{_f(x)} {_f(y)} l")
                lcx = lqx = None
            elif C == "H":
                x = ox + num()
                out.append(f"{_f(x)} {_f(y)} l")
                lcx = lqx = None
            elif C == "V":
                y = (y if rel else 0.0) + num()
                out.append(f"{_f(x)} {_f(y)} l")
                lcx = lqx = None
            elif C == "C":
                x1, y1, x2, y2, x3, y3 = ox + num(), oy + num(), ox + num(), oy + num(), ox + num(), oy + num()
                out.append(f"{_f(x1)} {_f(y1)} {_f(x2)} {_f(y2)} {_f(x3)} {_f(y3)} c")
                lcx, lcy, x, y = x2, y2, x3, y3
                lqx = None
            elif C == "S":
                x1, y1 = (2 * x - lcx, 2 * y - lcy) if lcx is not None else (x, y)
                x2, y2, x3, y3 = ox + num(), oy + num(), ox + num(), oy + num()
                out.append(f"{_f(x1)} {_f(y1)} {_f(x2)} {_f(y2)} {_f(x3)} {_f(y3)} c")
                lcx, lcy, x, y = x2, y2, x3, y3
                lqx = None
            elif C in "QT":
                if C == "Q":
                    qx, qy = ox + num(), oy + num()
                else:
                    qx, qy = (2 * x - lqx, 2 * y - lqy) if lqx is not None else (x, y)
                x3, y3 = ox + num(), oy + num()
                out.append(f"{_f(x + 2 / 3 * (qx - x))} {_f(y + 2 / 3 * (qy - y))} "
                           f"{_f(x3 + 2 / 3 * (qx - x3))} {_f(y3 + 2 / 3 * (qy - y3))} {_f(x3)} {_f(y3)} c")
                lqx, lqy, x, y = qx, qy, x3, y3
                lcx = None
            elif C == "A":
                rx, ry, phi, fa, fs = num(), num(), num(), num(), num()
                x2, y2 = ox + num(), oy + num()
                for seg in _arc_to_beziers(x, y, rx, ry, phi, int(fa), int(fs), x2, y2):
                    if len(seg) == 2 and not isinstance(seg[0], tuple):
                        out.append(f"{_f(seg[0])} {_f(seg[1])} l")
                    else:
                        (a, b), (c, d_), (e, f) = seg
                        out.append(f"{_f(a)} {_f(b)} {_f(c)} {_f(d_)} {_f(e)} {_f(f)} c")
                x, y = x2, y2
                lcx = lqx = None
            else:
                i += 1
        except (IndexError, ValueError):
            break
    return "\n".join(out)


def _ellipse_ops(cx, cy, rx, ry):
    k = 0.5522847498
    return "\n".join([
        f"{_f(cx + rx)} {_f(cy)} m",
        f"{_f(cx + rx)} {_f(cy + k * ry)} {_f(cx + k * rx)} {_f(cy + ry)} {_f(cx)} {_f(cy + ry)} c",
        f"{_f(cx - k * rx)} {_f(cy + ry)} {_f(cx - rx)} {_f(cy + k * ry)} {_f(cx - rx)} {_f(cy)} c",
        f"{_f(cx - rx)} {_f(cy - k * ry)} {_f(cx - k * rx)} {_f(cy - ry)} {_f(cx)} {_f(cy - ry)} c",
        f"{_f(cx + k * rx)} {_f(cy - ry)} {_f(cx + rx)} {_f(cy - k * ry)} {_f(cx + rx)} {_f(cy)} c", "h"])


def _rect_ops(x, y, w, h, rx, ry):
    if rx <= 0 and ry <= 0:
        return f"{_f(x)} {_f(y)} {_f(w)} {_f(h)} re"
    rx = min(rx or ry, w / 2)
    ry = min(ry or rx, h / 2)
    k = 0.5522847498
    return "\n".join([
        f"{_f(x + rx)} {_f(y)} m", f"{_f(x + w - rx)} {_f(y)} l",
        f"{_f(x + w - rx + k * rx)} {_f(y)} {_f(x + w)} {_f(y + ry - k * ry)} {_f(x + w)} {_f(y + ry)} c",
        f"{_f(x + w)} {_f(y + h - ry)} l",
        f"{_f(x + w)} {_f(y + h - ry + k * ry)} {_f(x + w - rx + k * rx)} {_f(y + h)} {_f(x + w - rx)} {_f(y + h)} c",
        f"{_f(x + rx)} {_f(y + h)} l",
        f"{_f(x + rx - k * rx)} {_f(y + h)} {_f(x)} {_f(y + h - ry + k * ry)} {_f(x)} {_f(y + h - ry)} c",
        f"{_f(x)} {_f(y + ry)} l",
        f"{_f(x)} {_f(y + ry - k * ry)} {_f(x + rx - k * rx)} {_f(y)} {_f(x + rx)} {_f(y)} c", "h"])


def _length_pt(v):
    """width="210mm" 같은 값 → pt."""
    if v is None:
        return None
    m = re.match(r"\s*([\d.]+)\s*([a-z%]*)", v)
    if not m:
        return None
    n, u = float(m.group(1)), m.group(2)
    return n * {"mm": 72 / 25.4, "cm": 72 / 2.54, "in": 72.0, "pt": 1.0, "pc": 12.0, "px": 0.75, "": 0.75}.get(u, 0.75)


# ───────────────────────── 변환 ─────────────────────────
class _Converter:
    def __init__(self, resolver):
        self.pdf = _Pdf()
        self.res = resolver
        self.fonts = {}      # face id → _EmbeddedFont
        self.images = []     # (이름, 객체번호)
        self.gstates = {}    # (ca, CA) → 이름
        self.ops = []
        self.pages = []      # (W, H, 내용 스트림 번호)
        self.missing = set()

    # --- 자원
    def gs(self, fill_a, stroke_a, blend=None):
        key = (round(fill_a, 3), round(stroke_a, 3), blend)
        if key == (1.0, 1.0, None):
            return None
        if key not in self.gstates:
            self.gstates[key] = f"GS{len(self.gstates) + 1}"
        return self.gstates[key]

    def font(self, face):
        k = id(face)
        if k not in self.fonts:
            self.fonts[k] = _EmbeddedFont(face, f"F{len(self.fonts) + 1}")
        return self.fonts[k]

    def image(self, href):
        from PIL import Image
        m = re.match(r"data:image/(\w+);base64,(.*)", href or "", re.S)
        if not m:
            return None
        raw = base64.b64decode(m.group(2))
        im = Image.open(io.BytesIO(raw))
        name = f"Im{len(self.images) + 1}"
        if m.group(1).lower() in ("jpeg", "jpg") and im.mode in ("L", "RGB"):
            cs = "/DeviceGray" if im.mode == "L" else "/DeviceRGB"
            num = self.pdf.stream(raw, f"/Type /XObject /Subtype /Image /Width {im.width} /Height {im.height} "
                                       f"/ColorSpace {cs} /BitsPerComponent 8 /Filter /DCTDecode ", compress=False)
        else:
            smask = ""
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                a = im.getchannel("A")
                if a.getextrema() != (255, 255):
                    sm = self.pdf.stream(a.tobytes(), f"/Type /XObject /Subtype /Image /Width {im.width} "
                                                      f"/Height {im.height} /ColorSpace /DeviceGray /BitsPerComponent 8 ")
                    smask = f"/SMask {sm} 0 R "
            im = im.convert("RGB")
            num = self.pdf.stream(im.tobytes(), f"/Type /XObject /Subtype /Image /Width {im.width} /Height {im.height} "
                                                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 {smask}")
        self.images.append((name, num))
        return name

    # --- 그리기
    def paint(self, el, inh, geom, closed=True):
        fill = _color(_attr(el, "fill", inh, "#000000")) if closed else None
        stroke = _color(_attr(el, "stroke", inh, "none"))
        if fill is None and stroke is None:
            return
        op_a = _num(_attr(el, "opacity", {}, "1"), 1.0) * inh.get("_opacity", 1.0)
        fa = op_a * _num(_attr(el, "fill-opacity", inh, "1"), 1.0)
        sa = op_a * _num(_attr(el, "stroke-opacity", inh, "1"), 1.0)
        o = self.ops
        o.append("q")
        t = el.get("transform")
        if t:
            o.append("%s %s %s %s %s %s cm" % tuple(_f(v) for v in _transform(t)))
        g = self.gs(fa, sa)
        if g:
            o.append(f"/{g} gs")
        if fill:
            o.append("%s %s %s rg" % tuple(_f(v) for v in fill))
        if stroke:
            o.append("%s %s %s RG" % tuple(_f(v) for v in stroke))
            o.append(f"{_f(_num(_attr(el, 'stroke-width', inh, '1'), 1.0))} w")
            cap = {"butt": 0, "round": 1, "square": 2}.get(_attr(el, "stroke-linecap", inh, "butt"), 0)
            join = {"miter": 0, "round": 1, "bevel": 2}.get(_attr(el, "stroke-linejoin", inh, "miter"), 0)
            o.append(f"{cap} J {join} j")
            dash = _attr(el, "stroke-dasharray", inh, "none")
            if dash and dash != "none":
                arr = [float(v) for v in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)", dash)]
                if arr and any(arr):
                    o.append(f"[{' '.join(_f(v) for v in arr)}] {_f(_num(_attr(el, 'stroke-dashoffset', inh, '0')))} d")
        o.append(geom)
        eo = _attr(el, "fill-rule", inh, "nonzero") == "evenodd"
        if fill and stroke:
            o.append("B*" if eo else "B")
        elif fill:
            o.append("f*" if eo else "f")
        else:
            o.append("S")
        o.append("Q")

    def text(self, el, inh):
        s = "".join(el.itertext())
        if not s.strip():
            return
        family = (_attr(el, "font-family", inh, "") or "").split(",")[0].strip().strip("'\"")
        weight = _attr(el, "font-weight", inh, "400")
        weight = {"normal": 400, "bold": 700}.get(weight, int(_num(weight, 400)))
        italic = _attr(el, "font-style", inh, "normal") in ("italic", "oblique")
        size = _num(_attr(el, "font-size", inh, "16"), 16.0)
        face = self.res.find(family, weight, italic, s)
        if face is None:
            self.missing.add(family)
            return
        ef = self.font(face)
        x, y = _num(el.get("x")), _num(el.get("y"))
        wsp = _num(_attr(el, "word-spacing", inh, "0"), 0.0)
        lsp = _num(_attr(el, "letter-spacing", inh, "0"), 0.0)
        anchor = _attr(el, "text-anchor", inh, "start")
        parts, cur, width = [], [], 0.0
        for ch in s:
            c = ef.code(ch)
            if c is None:  # 이 글꼴에 없는 글자 → 빈 자리
                c = ef.code(" ") if ef.cmap.get(32) else None
                if c is None:
                    continue
            cur.append(f"{c:04X}")
            width += ef.advance(c) * size / 1000.0 + lsp
            extra = lsp + (wsp if ch == " " else 0.0)
            if extra:
                parts.append("<" + "".join(cur) + ">")
                parts.append(_f(-extra * 1000.0 / size))
                cur = []
                if ch == " ":
                    width += wsp
        if cur:
            parts.append("<" + "".join(cur) + ">")
        if anchor == "middle":
            x -= width / 2
        elif anchor == "end":
            x -= width
        fill = _color(_attr(el, "fill", inh, "#000000")) or (0, 0, 0)
        op = _num(_attr(el, "opacity", {}, "1"), 1.0) * inh.get("_opacity", 1.0) * _num(_attr(el, "fill-opacity", inh, "1"), 1.0)
        o = self.ops
        o.append("q")
        t = el.get("transform")
        if t:
            o.append("%s %s %s %s %s %s cm" % tuple(_f(v) for v in _transform(t)))
        g = self.gs(op, 1.0)
        if g:
            o.append(f"/{g} gs")
        o.append("%s %s %s rg" % tuple(_f(v) for v in fill))
        o.append(f"BT /{ef.name} {_f(size)} Tf 1 0 0 -1 {_f(x)} {_f(y)} Tm [{' '.join(parts)}] TJ ET")
        o.append("Q")

    def walk(self, el, inh):
        tag = el.tag.replace(SVG_NS, "")
        st = _style(el)
        if (st.get("display") or el.get("display")) == "none" or (st.get("visibility") or el.get("visibility")) == "hidden":
            return
        if tag in ("title", "desc", "metadata", "defs", "style", "script", "clipPath", "mask", "symbol"):
            return
        inh = dict(inh)
        for k in ("fill", "stroke", "stroke-width", "fill-opacity", "stroke-opacity", "fill-rule", "font-family",
                  "font-size", "font-weight", "font-style", "word-spacing", "letter-spacing", "text-anchor",
                  "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset"):
            v = st.get(k, el.get(k))
            if v is not None:
                inh[k] = v
        n = _num
        if tag in ("g", "svg", "a"):
            op = n(st.get("opacity", el.get("opacity")), 1.0)
            t = el.get("transform") if tag != "svg" else None
            if t:
                self.ops.append("q")
                self.ops.append("%s %s %s %s %s %s cm" % tuple(_f(v) for v in _transform(t)))
            inh["_opacity"] = inh.get("_opacity", 1.0) * op
            for ch in el:
                self.walk(ch, inh)
            if t:
                self.ops.append("Q")
        elif tag == "rect":
            w, h = n(el.get("width")), n(el.get("height"))
            if w > 0 and h > 0:
                self.paint(el, inh, _rect_ops(n(el.get("x")), n(el.get("y")), w, h, n(el.get("rx")), n(el.get("ry"))))
        elif tag == "circle":
            r = n(el.get("r"))
            if r > 0:
                self.paint(el, inh, _ellipse_ops(n(el.get("cx")), n(el.get("cy")), r, r))
        elif tag == "ellipse":
            rx, ry = n(el.get("rx")), n(el.get("ry"))
            if rx > 0 and ry > 0:
                self.paint(el, inh, _ellipse_ops(n(el.get("cx")), n(el.get("cy")), rx, ry))
        elif tag == "line":
            self.paint(el, inh, f"{_f(n(el.get('x1')))} {_f(n(el.get('y1')))} m {_f(n(el.get('x2')))} {_f(n(el.get('y2')))} l",
                       closed=False)
        elif tag in ("polyline", "polygon"):
            pts = [float(v) for v in re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", el.get("points", ""))]
            if len(pts) >= 4:
                g = [f"{_f(pts[0])} {_f(pts[1])} m"] + [f"{_f(pts[i])} {_f(pts[i + 1])} l" for i in range(2, len(pts) - 1, 2)]
                if tag == "polygon":
                    g.append("h")
                self.paint(el, inh, "\n".join(g), closed=(tag == "polygon" or _attr(el, "fill", inh, "#000") != "none"))
        elif tag == "path":
            d = el.get("d")
            if d:
                self.paint(el, inh, _path_ops(d))
        elif tag == "image":
            name = self.image(el.get("href") or el.get(XLINK))
            if name:
                x, y, w, h = n(el.get("x")), n(el.get("y")), n(el.get("width")), n(el.get("height"))
                o = self.ops
                o.append("q")
                t = el.get("transform")
                if t:
                    o.append("%s %s %s %s %s %s cm" % tuple(_f(v) for v in _transform(t)))
                g = self.gs(inh.get("_opacity", 1.0) * n(el.get("opacity"), 1.0), 1.0)
                if g:
                    o.append(f"/{g} gs")
                o.append(f"{_f(w)} 0 0 {_f(-h)} {_f(x)} {_f(y + h)} cm /{name} Do Q")
        elif tag == "text":
            self.text(el, inh)

    def add_page(self, svg_path, ink=None, scan_path=None):
        """한 쪽을 더한다. scan_path를 주면 재구성 대신 그 사진을 바탕으로 쓴다(크기는 SVG의 viewBox에 맞춤).
        ink는 필기 자료(dict 또는 JSON 파일 경로) — 맨 위에 벡터로 그린다."""
        root = ET.parse(svg_path).getroot()
        vb = [float(v) for v in re.split(r"[\s,]+", (root.get("viewBox") or "").strip()) if v]
        if len(vb) != 4:
            vb = [0, 0, _num(root.get("width"), 595), _num(root.get("height"), 842)]
        W = _length_pt(root.get("width")) or vb[2] * 0.75
        H = _length_pt(root.get("height")) or vb[3] * 0.75
        sx, sy = W / vb[2], H / vb[3]
        self.ops = [f"{_f(sx)} 0 0 {_f(-sy)} {_f(-vb[0] * sx)} {_f(H + vb[1] * sy)} cm"]
        if scan_path:
            with open(scan_path, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            name = self.image("data:image/jpeg;base64," + b64)
            self.ops.append(f"q {_f(vb[2])} 0 0 {_f(-vb[3])} {_f(vb[0])} {_f(vb[1] + vb[3])} cm /{name} Do Q")
        else:
            for ch in root:
                self.walk(ch, {})
        if ink:
            self.draw_ink(ink)
        self.pages.append((W, H, self.pdf.stream("\n".join(self.ops).encode("latin-1"))))

    def draw_ink(self, ink):
        import json
        if isinstance(ink, str):
            if not os.path.exists(ink):
                return
            with open(ink, encoding="utf-8") as fh:
                ink = json.load(fh)
        o = self.ops
        for st in ink.get("strokes", []):
            pts = st.get("pts") or []
            n = len(pts) // 3
            if n == 0:
                continue
            col = _color(st.get("c", "#000000")) or (0, 0, 0)
            w = float(st.get("w", 3.0))
            o.append("q 1 J 1 j")
            o.append("%s %s %s RG %s %s %s rg" % (tuple(_f(v) for v in col) * 2))
            if st.get("t") == "hl":
                g = self.gs(1.0, float(st.get("a", 0.35)), "Multiply")
                o.append(f"/{g} gs {_f(w)} w")
                o.append(f"{_f(pts[0])} {_f(pts[1])} m")
                if n == 1:
                    o.append(f"{_f(pts[0] + 0.01)} {_f(pts[1])} l")
                for i in range(1, n):
                    o.append(f"{_f(pts[3 * i])} {_f(pts[3 * i + 1])} l")
                o.append("S Q")
                continue
            if n == 1:
                r = pen_width(w, pts[2]) / 2
                o.append(_ellipse_ops(pts[0], pts[1], r, r) + " f Q")
                continue
            last = None
            for i in range(1, n):
                x0, y0, p0 = pts[3 * i - 3:3 * i]
                x1, y1, p1 = pts[3 * i:3 * i + 3]
                lw = pen_width(w, (p0 + p1) / 2)
                if last is None or abs(lw - last) > 0.05:
                    o.append(f"{_f(lw)} w")
                    last = lw
                o.append(f"{_f(x0)} {_f(y0)} m {_f(x1)} {_f(y1)} l S")
            o.append("Q")

    def save(self, pdf_path, title=None):
        pdf = self.pdf
        fonts = " ".join(f"/{ef.name} {ef.embed(pdf)} 0 R" for ef in self.fonts.values())
        xobj = " ".join(f"/{n} {num} 0 R" for n, num in self.images)
        gst = " ".join(f"/{name} << /ca {_f(k[0])} /CA {_f(k[1])}" + (f" /BM /{k[2]}" if k[2] else "") + " >>"
                       for k, name in self.gstates.items())
        res = pdf.put(f"<< /Font << {fonts} >> /XObject << {xobj} >> /ExtGState << {gst} >> >>")
        pages = pdf.reserve()
        kids = []
        for W, H, content in self.pages:
            kids.append(pdf.put(f"<< /Type /Page /Parent {pages} 0 R /MediaBox [0 0 {_f(W)} {_f(H)}] "
                                f"/Resources {res} 0 R /Contents {content} 0 R >>"))
        pdf.put(f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>", pages)
        info = "/Producer (scan2svg)" + (f" /Title {_pdfstr(title)}" if title else "")
        cat = pdf.put(f"<< /Type /Catalog /Pages {pages} 0 R >>")
        pdf.put(f"<< {info} >>")
        pdf.write(pdf_path, cat)
        return {"pages": len(self.pages), "fonts": [f"{ef.face.family} {ef.face.style}" for ef in self.fonts.values()],
                "images": len(self.images), "missing_fonts": sorted(self.missing)}

    def convert(self, svg_path, pdf_path, title=None, ink=None):
        self.add_page(svg_path, ink)
        return self.save(pdf_path, title)


def pen_width(w, p):
    """펜 굵기: 필압 p(0~1)에 따라 기본 굵기 w의 30%~100%. 앱(Kotlin)의 InkView와 같은 식."""
    p = min(1.0, max(0.0, float(p)))
    return w * (0.3 + 0.7 * p)


def svg_to_pdf(svg_path, pdf_path, faces, title=None, ink=None):
    """SVG → PDF 한 쪽. faces는 scan2svg.load_fonts()가 돌려준 글꼴 목록. 돌려주는 값: 넣은 글꼴·이미지 수·못 찾은 글꼴."""
    return _Converter(FontResolver(faces)).convert(svg_path, pdf_path, title, ink)


def pages_to_pdf(pages, pdf_path, faces, title=None):
    """여러 쪽을 PDF 하나로. pages = [{"svg": 경로, "ink": 경로|dict|None, "scan": 경로|None}, ...]
    scan을 주면 그 쪽은 재구성 대신 보정한 원본 사진을 바탕으로 쓴다."""
    c = _Converter(FontResolver(faces))
    for p in pages:
        c.add_page(p["svg"], p.get("ink"), p.get("scan"))
    return c.save(pdf_path, title)


if __name__ == "__main__":
    import argparse
    import time
    ap = argparse.ArgumentParser()
    ap.add_argument("svg")
    ap.add_argument("pdf")
    ap.add_argument("--fonts", nargs="*", default=None)
    a = ap.parse_args()
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import scan2svg as S
    t = time.time()
    faces = S.load_fonts(a.fonts or S.default_font_dirs())
    t1 = time.time()
    info = svg_to_pdf(a.svg, a.pdf, faces, title=os.path.splitext(os.path.basename(a.svg))[0])
    print(f"글꼴 목록 {t1 - t:.1f}s, 변환 {time.time() - t1:.1f}s, {os.path.getsize(a.pdf) // 1024} KB", info)

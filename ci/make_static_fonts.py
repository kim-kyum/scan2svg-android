#!/usr/bin/env python3
"""앱에 넣을 글꼴을 만든다: ci/fonts.txt의 목록을 받아(또는 로컬 폴더에서 읽어) 크기를 줄이고 assets/fonts에 넣는다.

- 가변 글꼴은 굵기별 고정 글꼴로 뽑는다. 안드로이드용 Pillow의 FreeType(2.9.1)은 일부 가변 글꼴을 제대로 그리지
  못하고, 고정 글꼴이 글꼴 대입도 빠르다. 굵기 말고 다른 축(폭·광학 크기)은 기본값으로 고정한다.
- 한글·로마자·그리스 문자·기호만 남긴다(한자·키릴 등은 뺀다).
- 이름 칸에 '='를 쓴 글꼴은 줄이지 않고 그대로 넣는다.
- 넷째 칸에 ks를 쓰면 한글은 자주 쓰는 2350자만 남긴다(크기를 크게 줄인다).

사용: make_static_fonts.py ci/fonts.txt <출력 폴더> [--src 로컬 google/fonts 폴더]"""
import io
import os
import sys
import urllib.parse
import urllib.request

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

BASE = "https://raw.githubusercontent.com/google/fonts/main/"
STYLE = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular", 500: "Medium",
         600: "SemiBold", 700: "Bold", 800: "ExtraBold", 900: "Black"}
KEEP = [(0x20, 0x7E), (0xA0, 0x24F), (0x2B0, 0x2FF), (0x300, 0x36F), (0x370, 0x3FF), (0x1E00, 0x1EFF),
        (0x2000, 0x206F), (0x2070, 0x209F), (0x20A0, 0x20BF), (0x2100, 0x214F), (0x2150, 0x218F),
        (0x2190, 0x21FF), (0x2200, 0x22FF), (0x2300, 0x23FF), (0x2460, 0x24FF), (0x2500, 0x257F),
        (0x25A0, 0x25FF), (0x2600, 0x26FF), (0x2700, 0x27BF), (0x27C0, 0x27EF), (0x2980, 0x29FF),
        (0x3000, 0x303F), (0x3130, 0x318F), (0x3200, 0x32FF), (0x3300, 0x33FF), (0x1100, 0x11FF),
        (0xA960, 0xA97F), (0xD7B0, 0xD7FF), (0xAC00, 0xD7A3), (0xFF00, 0xFFEF)]


def fetch(path, src):
    if src:
        with open(os.path.join(src, path), "rb") as f:
            return f.read()
    url = BASE + urllib.parse.quote(path)
    for _ in range(3):
        try:
            return urllib.request.urlopen(url, timeout=120).read()
        except Exception as e:
            err = e
    raise err


def _ks(u):
    """KS X 1001(완성형) 한글 2350자에 드는가"""
    try:
        return len(chr(u).encode("euc-kr")) == 2  # 파이썬은 2350자 밖의 음절을 8바이트 조합 순서로 바꿔 준다 → 제외
    except UnicodeEncodeError:
        return False


def shrink(tt, ks=False):
    """ks=True면 한글 음절을 자주 쓰는 2350자(KS X 1001)만 남긴다 — 획이 많은 글꼴의 크기를 1/3쯤으로 줄인다."""
    cmap = tt.getBestCmap()
    jamo = lambda u: 0x1100 <= u <= 0x11FF or 0xA960 <= u <= 0xA97F or 0xD7B0 <= u <= 0xD7FF
    keep = [u for a, b in KEEP for u in range(a, b + 1)
            if u in cmap and not (ks and (jamo(u) or (0xAC00 <= u <= 0xD7A3 and not _ks(u))))]
    opt = subset.Options()
    opt.name_IDs = ["*"]
    opt.name_languages = ["*"]
    opt.notdef_outline = True
    opt.hinting = False
    # 조합형 자모 대치(GSUB) 때문에 빠진 음절이 도로 딸려 오지 않게, 줄이는 글꼴은 글자 대치 기능을 뺀다
    # (앱은 글자를 대치 없이 하나씩 놓는다)
    opt.layout_features = [] if ks else ["*"]
    opt.drop_tables += ["DSIG"]
    sub = subset.Subsetter(opt)
    sub.populate(unicodes=keep)
    sub.subset(tt)
    return len(cmap), len(keep)


def set_names(tt, family, weight, italic):
    style = STYLE[weight] + (" Italic" if italic else "")
    if style == "Regular Italic":
        style = "Italic"
    ribbi = style in ("Regular", "Bold", "Italic", "Bold Italic")
    ps = family.replace(" ", "") + "-" + style.replace(" ", "")
    name = tt["name"]
    name.names = [n for n in name.names if n.nameID not in (1, 2, 3, 4, 6, 16, 17, 21, 22, 25)]
    fam1 = family if ribbi else f"{family} {STYLE[weight]}"
    sub1 = style if ribbi else ("Italic" if italic else "Regular")
    for nid, s in ((1, fam1), (2, sub1), (3, f"scan2svg:{ps}"), (4, f"{family} {style}"), (6, ps), (16, family), (17, style)):
        name.setName(s, nid, 3, 1, 0x409)
        name.setName(s, nid, 1, 0, 0)
    os2 = tt["OS/2"]
    os2.usWeightClass = weight
    sel = os2.fsSelection & ~0x61
    if italic:
        sel |= 0x01
    if weight == 700:
        sel |= 0x20
    if not italic and weight != 700:
        sel |= 0x40
    os2.fsSelection = sel
    tt["head"].macStyle = (tt["head"].macStyle & ~3) | (1 if weight == 700 else 0) | (2 if italic else 0)
    return ps


def main(listing, out_dir, src=None):
    os.makedirs(out_dir, exist_ok=True)
    total = 0
    licenses = set()
    for line in open(listing, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")] + ["", "", ""]
        path, family, weights, opts = parts[0], parts[1], parts[2], parts[3]
        ks = "ks" in opts.split()
        try:
            data = fetch(path, src)
            licenses.add(os.path.dirname(path))
            base = os.path.basename(path).replace("[", "_").replace("]", "").replace(",", "_")
            if family == "=":
                outs = [(base, data)]
            elif not family:
                tt = TTFont(io.BytesIO(data))
                shrink(tt, ks)
                b = io.BytesIO()
                tt.save(b)
                outs = [(base, b.getvalue())]
            else:
                tt = TTFont(io.BytesIO(data))
                shrink(tt, ks)
                b = io.BytesIO()
                tt.save(b)
                italic = "Italic" in os.path.basename(path)
                axes = {a.axisTag: a.defaultValue for a in tt["fvar"].axes}
                outs = []
                for w in [int(x) for x in weights.split(",") if x]:
                    loc = dict(axes)
                    loc["wght"] = w
                    inst = instancer.instantiateVariableFont(TTFont(io.BytesIO(b.getvalue())), loc)
                    for tag in ("STAT", "avar", "fvar", "gvar", "HVAR", "MVAR", "VVAR", "cvar"):
                        if tag in inst:
                            del inst[tag]
                    ps = set_names(inst, family, w, italic)
                    o = io.BytesIO()
                    inst.save(o)
                    outs.append((ps + ".ttf", o.getvalue()))
            for name, blob in outs:
                with open(os.path.join(out_dir, name), "wb") as f:
                    f.write(blob)
                total += len(blob)
                print(f"ok   {name} {len(blob) // 1024} KB", flush=True)
        except Exception as e:
            print(f"FAIL {path}: {type(e).__name__}: {e}", flush=True)
    # 라이선스 문서
    with open(os.path.join(out_dir, "LICENSES.txt"), "w", encoding="utf-8") as f:
        f.write("이 폴더의 글꼴은 Google Fonts 저장소(github.com/google/fonts)에서 가져와 글자 수를 줄이고 굵기별로 나눈 것입니다.\n"
                "모두 SIL Open Font License 1.1을 따릅니다.\n\n")
        for d in sorted(licenses):
            try:
                f.write(f"===== {d} =====\n" + fetch(d + "/OFL.txt", src).decode("utf-8", "replace") + "\n\n")
            except Exception:
                f.write(f"===== {d} ===== (OFL.txt를 받지 못함: https://github.com/google/fonts/tree/main/{d})\n\n")
    print(f"합계 {total / 1e6:.1f} MB")


if __name__ == "__main__":
    a = sys.argv[1:]
    src = None
    if "--src" in a:
        i = a.index("--src")
        src = a[i + 1]
        del a[i:i + 2]
    main(a[0], a[1], src)

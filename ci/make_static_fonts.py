#!/usr/bin/env python3
"""가변 글꼴(한 파일에 모든 굵기)에서 굵기별 고정 글꼴을 뽑아 앱에 넣을 크기로 줄인다.

안드로이드용 Pillow에 들어 있는 FreeType(2.9.1)은 한글 가변 글꼴을 제대로 그리지 못해서,
빌드할 때 미리 굵기별 파일로 만들어 넣는다. 용량을 줄이려고 한글·로마자·기호만 남긴다(한자는 뺀다).

사용: make_static_fonts.py <가변 글꼴.ttf> <출력 폴더> <글꼴 이름> <굵기,굵기,…>
 예:  make_static_fonts.py NotoSansKR-VF.ttf out "Noto Sans KR" 300,500,700,900"""
import os
import sys
import tempfile

from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

STYLE = {100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular", 500: "Medium",
         600: "SemiBold", 700: "Bold", 800: "ExtraBold", 900: "Black"}

KEEP = [(0x20, 0x7E), (0xA0, 0x17F), (0x2000, 0x206F), (0x20A0, 0x20BF), (0x2100, 0x214F), (0x2150, 0x218F),
        (0x2190, 0x21FF), (0x2200, 0x22FF), (0x2460, 0x24FF), (0x2500, 0x257F), (0x25A0, 0x25FF), (0x2600, 0x26FF),
        (0x3000, 0x303F), (0x3130, 0x318F), (0x3200, 0x32FF), (0x3300, 0x33FF), (0x1100, 0x11FF),
        (0xAC00, 0xD7A3), (0xFF00, 0xFFEF)]


def set_names(tt, family, weight):
    style = STYLE[weight]
    ribbi = style in ("Regular", "Bold")
    ps = family.replace(" ", "") + "-" + style
    name = tt["name"]
    name.names = [n for n in name.names if n.nameID not in (1, 2, 3, 4, 6, 16, 17, 21, 22, 25)]
    for nid, s in ((1, family if ribbi else f"{family} {style}"), (2, style if ribbi else "Regular"),
                   (3, f"scan2svg:{ps}"), (4, f"{family} {style}"), (6, ps), (16, family), (17, style)):
        name.setName(s, nid, 3, 1, 0x409)
        name.setName(s, nid, 1, 0, 0)
    os2 = tt["OS/2"]
    os2.usWeightClass = weight
    os2.fsSelection = (os2.fsSelection & ~0x61) | (0x20 if style == "Bold" else 0x40)   # BOLD 또는 REGULAR
    tt["head"].macStyle = (tt["head"].macStyle & ~1) | (1 if style == "Bold" else 0)


def main(src, out_dir, family, weights):
    os.makedirs(out_dir, exist_ok=True)
    tt = TTFont(src)
    cmap = tt.getBestCmap()
    keep = [u for a, b in KEEP for u in range(a, b + 1) if u in cmap]
    opt = subset.Options()
    opt.name_IDs = ["*"]
    opt.name_languages = ["*"]
    opt.notdef_outline = True
    opt.hinting = False
    opt.layout_features = ["*"]
    opt.drop_tables += ["DSIG"]
    sub = subset.Subsetter(opt)
    sub.populate(unicodes=keep)
    sub.subset(tt)
    tmp = os.path.join(tempfile.mkdtemp(), "subset.ttf")
    tt.save(tmp)
    print(f"{os.path.basename(src)}: 글자 {len(cmap)} → {len(keep)}자, {os.path.getsize(src)} → {os.path.getsize(tmp)} bytes")
    for w in weights:
        inst = instancer.instantiateVariableFont(TTFont(tmp), {"wght": w})
        for tag in ("STAT", "avar", "fvar", "gvar", "HVAR", "MVAR", "VVAR", "cvar"):
            if tag in inst:
                del inst[tag]
        set_names(inst, family, w)
        path = os.path.join(out_dir, f"{family.replace(' ', '')}-{STYLE[w]}.ttf")
        inst.save(path)
        print(f"ok   {os.path.basename(path)} {os.path.getsize(path)} bytes")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], [int(x) for x in sys.argv[4].split(",")])

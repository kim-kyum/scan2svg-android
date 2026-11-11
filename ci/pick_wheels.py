#!/usr/bin/env python3
"""Chaquopy 저장소에 실제로 올라와 있는 안드로이드용 휠을 보고, numpy·OpenCV·Pillow가 모두 있는
파이썬 버전과 패키지 버전을 고른다.
(버전을 안 정하면 pip이 PyPI의 소스 배포판을 받아 컴파일하려다 실패한다.)

사용: pick_wheels.py arm64_v8a,x86_64
  표준 출력  → gradle에 넘길 -P 옵션들
  표준 오류  → 저장소에 무엇이 있는지 표(기록용)
  python-version.txt → 고른 파이썬 버전(빌드용 파이썬을 같은 버전으로 맞추는 데 쓴다)"""
import re
import sys
import urllib.request

INDEX = "https://chaquo.com/pypi-13.1/"
ABIS = sys.argv[1].split(",")
# 새 버전부터 본다. 3.13은 이 Chaquopy 판에서 확인해 보지 않아 맨 뒤.
PYTHONS = ["3.12", "3.11", "3.10", "3.9", "3.8", "3.13"]
OPENCV = ["opencv-python", "opencv-python-headless", "opencv-contrib-python", "opencv-contrib-python-headless"]


def vkey(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.+-]", v))


def listing(pkg):
    """{cpXY: {버전: {abi,…}}}"""
    try:
        html = urllib.request.urlopen(INDEX + pkg + "/", timeout=60).read().decode("utf-8", "replace")
    except Exception as e:
        print(f"{pkg}: 목록을 읽지 못함 ({e})", file=sys.stderr)
        return {}
    table = {}
    for fn in set(re.findall(r'href="[^"]*?([^"/]+\.whl)', html)):
        m = re.match(r"[^-]+-([^-]+)-(?:\d+-)?(cp\d+)-[^-]+-android_\d+_(\w+)\.whl", fn)
        if m:
            table.setdefault(m.group(2), {}).setdefault(m.group(1), set()).add(m.group(3))
    for cp in sorted(table, key=lambda c: int(c[2:])):
        row = ", ".join(f"{v}[{'/'.join(sorted(table[cp][v]))}]" for v in sorted(table[cp], key=vkey))
        print(f"{pkg} {cp}: {row}", file=sys.stderr)
    return table


def usable(table, cp):
    return sorted((v for v, a in table.get(cp, {}).items() if all(x in a for x in ABIS)), key=vkey)


numpy, pillow = listing("numpy"), listing("pillow")
cvs = {name: listing(name) for name in OPENCV}

choice = None
for py in PYTHONS:
    cp = "cp" + py.replace(".", "")
    n, p = usable(numpy, cp), usable(pillow, cp)
    cv = next(((name, usable(t, cp)) for name, t in cvs.items() if usable(t, cp)), None)
    print(f"파이썬 {py}: numpy={n[-3:]} pillow={p[-3:]} opencv={cv}", file=sys.stderr)
    if n and p and cv and choice is None:
        n1 = [v for v in n if int(v.split(".")[0]) < 2] or n   # OpenCV 휠은 대개 numpy 1.x에 맞춰 빌드돼 있다
        choice = (py, n1[-1], cv[0], cv[1][-1], p[-1])

if choice is None:
    print("세 패키지가 모두 있는 파이썬 버전을 찾지 못했습니다.", file=sys.stderr)
    sys.exit(1)

py, nv, cvname, cvv, pv = choice
print(f"선택: 파이썬 {py}, numpy {nv}, {cvname} {cvv}, pillow {pv}", file=sys.stderr)
open("python-version.txt", "w").write(py)
print(f"-PpythonVersion={py} -PnumpyVersion={nv} -PopencvPackage={cvname} -PopencvVersion={cvv} -PpillowVersion={pv}")

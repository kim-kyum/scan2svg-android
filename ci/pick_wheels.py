#!/usr/bin/env python3
"""Chaquopy 저장소에 실제로 올라와 있는 안드로이드용 휠 가운데, 지정한 파이썬 버전과 CPU에 맞는 최신 버전을 고른다.
(버전을 안 정하면 pip이 PyPI의 최신 소스 배포판을 받아 컴파일하려다 실패한다.)
사용: pick_wheels.py 3.11 arm64_v8a,x86_64  →  -PnumpyVersion=… -PopencvVersion=… -PpillowVersion=… 를 출력"""
import re
import sys
import urllib.request

INDEX = "https://chaquo.com/pypi-13.1/"
py = "cp" + sys.argv[1].replace(".", "")
abis = sys.argv[2].split(",")


def versions(pkg):
    html = urllib.request.urlopen(INDEX + pkg + "/", timeout=60).read().decode("utf-8", "replace")
    found = {}
    for fn in set(re.findall(r'href="[^"]*?([^"/]+\.whl)', html)):
        m = re.match(r"[^-]+-([^-]+)-(?:\d+-)?(cp\d+)-[^-]+-android_\d+_(\w+)\.whl", fn)
        if m and m.group(2) == py:
            found.setdefault(m.group(1), set()).add(m.group(3))
    ok = [v for v, a in found.items() if all(x in a for x in abis)]
    key = lambda v: tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.+-]", v))
    return sorted(ok, key=key), sorted(found, key=key)


out = []
for pkg, prop, limit in (("numpy", "numpyVersion", 2), ("opencv-python", "opencvVersion", None), ("pillow", "pillowVersion", None)):
    ok, every = versions(pkg)
    if limit:  # OpenCV 휠은 numpy 1.x에 맞춰 빌드돼 있다
        ok = [v for v in ok if int(v.split(".")[0]) < limit] or ok
    print(f"{pkg}: {py} {abis} → {ok}   (이 파이썬용 전체: {every})", file=sys.stderr)
    if ok:
        out.append(f"-P{prop}={ok[-1]}")
print(" ".join(out))

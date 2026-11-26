#!/bin/bash
# 앱에 함께 넣을 무료 글꼴을 준비한다(저장소에는 넣지 않는다). 목록과 방법은 ci/fonts.txt, ci/make_static_fonts.py.
D=app/src/main/assets/fonts
mkdir -p "$D"
rm -f "$D"/*
python3 -m pip install -q fonttools || echo "FAIL pip install fonttools"
python3 ci/make_static_fonts.py ci/fonts.txt "$D"
ls "$D" | wc -l
du -sh "$D"
exit 0

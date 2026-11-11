#!/bin/bash
# 앱에 함께 넣을 글꼴을 준비한다(저장소에는 넣지 않는다). Noto Sans KR·Noto Serif KR, 둘 다 SIL Open Font License 1.1.
# 기기에는 한글 글꼴이 보통 한 굵기뿐이라, 굵은 제목 같은 줄을 맞추려면 여러 굵기가 필요하다.
# 받은 가변 글꼴은 그대로 넣지 않고 굵기별 파일로 뽑는다(ci/make_static_fonts.py에 이유를 적어 두었다).
SRC=fonts-src
D=app/src/main/assets/fonts
PROBE=app/src/androidTest/assets/probe_fonts   # 에뮬레이터 검사 때만 쓰는 표본(앱에는 안 들어간다)
mkdir -p "$SRC" "$D" "$PROBE"
rm -f "$D"/* "$PROBE"/*
BASE=https://raw.githubusercontent.com/google/fonts/main/ofl
get() { curl -fsSL --retry 3 -o "$SRC/$1" "$BASE/$2" && echo "ok   $1 $(stat -c %s "$SRC/$1") bytes" || { rm -f "$SRC/$1"; echo "FAIL $1"; }; }
get NotoSansKR-VF.ttf  "notosanskr/NotoSansKR%5Bwght%5D.ttf"
get NotoSerifKR-VF.ttf "notoserifkr/NotoSerifKR%5Bwght%5D.ttf"
get OFL-NotoSansKR.txt  "notosanskr/OFL.txt"
get OFL-NotoSerifKR.txt "notoserifkr/OFL.txt"
python3 -m pip install -q fonttools || echo "FAIL pip install fonttools"
[ -f "$SRC/NotoSansKR-VF.ttf" ]  && { python3 ci/make_static_fonts.py "$SRC/NotoSansKR-VF.ttf"  "$D" "Noto Sans KR"  300,500,700,900 || echo "FAIL static Noto Sans KR"; }
[ -f "$SRC/NotoSerifKR-VF.ttf" ] && { python3 ci/make_static_fonts.py "$SRC/NotoSerifKR-VF.ttf" "$D" "Noto Serif KR" 500,700,900     || echo "FAIL static Noto Serif KR"; }
cp "$SRC"/OFL-*.txt "$D"/ 2>/dev/null
cp "$SRC/NotoSansKR-VF.ttf" "$PROBE"/ 2>/dev/null
ls -l "$D" "$PROBE"
exit 0

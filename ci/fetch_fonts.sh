#!/bin/bash
# 앱에 함께 넣을 글꼴을 받는다(저장소에는 넣지 않는다). 둘 다 SIL Open Font License 1.1.
# 기기에는 한글 글꼴이 보통 한 굵기뿐이라, 굵은 제목 같은 줄을 맞추려면 여러 굵기가 든 글꼴이 필요하다.
D=app/src/main/assets/fonts
mkdir -p "$D"
BASE=https://raw.githubusercontent.com/google/fonts/main/ofl
get() { curl -fsSL --retry 3 -o "$D/$1" "$BASE/$2" && echo "ok   $1 $(stat -c %s "$D/$1") bytes" || { rm -f "$D/$1"; echo "FAIL $1"; }; }
get NotoSansKR-VF.ttf  "notosanskr/NotoSansKR%5Bwght%5D.ttf"
get NotoSerifKR-VF.ttf "notoserifkr/NotoSerifKR%5Bwght%5D.ttf"
get OFL-NotoSansKR.txt  "notosanskr/OFL.txt"
get OFL-NotoSerifKR.txt "notoserifkr/OFL.txt"
ls -l "$D"
exit 0

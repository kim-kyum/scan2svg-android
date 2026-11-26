# scan2svg (Android)

문서를 찍으면 **편집 가능한 요소로 다시 그려** 앱 안에 보관하고, 그 위에 바로 필기하고, 여러 장을 묶어 PDF로 내보내는 앱입니다.
글자는 글꼴·크기·색이 붙은 텍스트로, 수식은 LaTeX가 붙은 수식으로, 상자·선·원은 기본 도형으로, 손그림은 베지어 곡선으로,
사진처럼 벡터로 바꿀 수 없는 부분만 잘라낸 이미지로 남깁니다. 모든 처리는 기기 안에서 합니다(인터넷 권한 없음).

## 설치

[Releases](../../releases/latest)에서 `scan2svg.apk`를 내려받아 설치합니다(arm64 기기용).

## 쓰는 법

- **문서함**: 사진 찍기(여러 장 이어서) 또는 사진 가져오기 → 묶음이 생기고 쪽마다 뒤에서 차례로 변환됩니다.
- **쪽 보기**: 재구성 / 스캔(펴고 보정한 사진) / 원본 사진을 바꿔 보며 두 손가락으로 확대해 확인합니다.
  재구성이 마음에 들지 않는 쪽은 '스캔'으로 두면 PDF에도 사진으로 들어갑니다.
- **필기**: S펜으로 씁니다(필압 반영, 손바닥 무시). 펜·형광펜·지우개(S펜 버튼·지우개 끝도 지우개), 되돌리기,
  긋고 끝에서 잠깐 멈추면 직선·원·사각형·다각형으로 보정, 올가미로 골라 옮기기·복제·크기·색·삭제.
- **내보내기**: 묶음 전체나 한 쪽을 필기와 함께 PDF로 → 다른 앱으로 열기·보내기·기기에 저장. 쪽별 SVG·사진 저장도 됩니다.

## 구조

- `app/src/main/python/scan2svg.py` — 변환 파이프라인(PC에서도 그대로 실행 가능)
- `app/src/main/python/svg2pdf.py` — SVG(+필기) → PDF. 글꼴을 넣어 글자는 검색·복사 가능한 글자로
- `app/src/main/python/math_ocr.py` — 수식 찾기·읽기(전·후처리)와 다시 그리기
- `app/src/main/python/android_main.py` — 앱에서 파이썬을 부르는 진입점
- `Ocr.kt` 글자 인식(ML Kit 한국어) · `Ort.kt` 수식 모델 실행(ONNX Runtime) · `PageView.kt` 쪽 보기·필기 ·
  `Ink.kt` 필기 자료·도형 보정 · `Store.kt` 문서함 · `LibraryActivity.kt`, `DocActivity.kt` 화면
- 파이썬은 [Chaquopy](https://chaquo.com/chaquopy/)로 앱에 넣습니다.

원리는 "가설을 세우고 → 그려 보고 → 원본과 겹쳐 본다"입니다. OCR이 읽은 글을 후보 글꼴로 그려서 원본과 가장 잘 겹치는
글꼴·크기·위치를 고르고, 잘 겹치지 않는 줄은 글자로 바꾸지 않고 원래 모양대로 둡니다. 수식도 읽은 LaTeX를 그려 보고
원본과 충분히 겹칠 때만 채택합니다.

## 글꼴

찾는 곳은 세 군데입니다: 기기 글꼴(`/system/fonts`), 앱에 넣은 무료 글꼴(`ci/fonts.txt`: Noto Sans/Serif KR, 나눔고딕·명조,
고딕 A1, IBM Plex Sans KR, 고운바탕·돋움, 함렛, Inter, Lato, Poppins, STIX Two 등, 모두 SIL Open Font License 1.1),
앱의 "글꼴 추가"로 넣은 글꼴(.ttf .otf .ttc). 시험지에 흔한 한컴 글꼴처럼 앱에 넣을 수 없는 글꼴은 PC의 글꼴 파일을
"글꼴 추가"로 넣으면 가장 정확합니다.

## 수식 모델

[Pix2Text](https://github.com/breezedeus/Pix2Text)의 MFD(수식 찾기)·MFR(수식 읽기) 모델(MIT 라이선스)을 빌드 때 받아 넣습니다.
읽기 모델은 int8로 줄였습니다. 다시 그리기는 [ziamath](https://github.com/cdelker/ziamath)(MIT)와 STIX Two Math 글꼴로 합니다.
`math-check` 작업이 CI에서 같은 코드를 onnxruntime으로 돌려 결과를 `ci-math` 브랜치에 남깁니다.

## 빌드

`main`에 올리면 GitHub Actions가 APK를 만들어 Releases에 올리고, 에뮬레이터(태블릿)에서 예시 사진과 수학 문제지를 변환하고,
PDF 내보내기와 펜 입력까지 해 봅니다. 결과는 `ci-build`, `ci-test` 브랜치에 남습니다.

- numpy·OpenCV·Pillow는 Chaquopy 저장소에 올라온 안드로이드용 빌드만 쓸 수 있어서, `ci/pick_wheels.py`가
  세 패키지가 모두 있는 파이썬 버전과 패키지 버전을 골라 줍니다(현재 파이썬 3.10, OpenCV 4.5.1).
- 글꼴(`ci/fetch_fonts.sh`)과 수식 모델(`ci/app_models.sh`)은 저장소에 두지 않고 빌드할 때 받습니다.
- 서명 키(`app/scan2svg-prototype.jks`)는 시제품용으로 공개되어 있습니다(덮어 설치가 되게 하려는 것).

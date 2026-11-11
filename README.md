# scan2svg (Android)

문서 사진을 **편집 가능한 SVG**로 재구성하는 시제품 앱입니다.
글자는 글꼴·크기·색이 붙은 텍스트로, 상자·선·원은 기본 도형으로, 손그림 같은 나머지는 베지어 곡선으로,
사진처럼 벡터로 바꿀 수 없는 부분만 잘라낸 이미지로 남깁니다. 모든 처리는 기기 안에서 합니다(인터넷 권한 없음).

## 설치

[Releases](../../releases/latest)에서 `scan2svg.apk`를 내려받아 설치합니다(arm64 기기용).

## 구조

- `app/src/main/python/scan2svg.py` — 변환 파이프라인(파이썬, PC에서도 그대로 실행 가능)
- `app/src/main/python/android_main.py` — 앱에서 파이프라인을 부르는 진입점
- `app/src/main/java/.../Ocr.kt` — 기기 내장 글자 인식(ML Kit 한국어)
- `app/src/main/java/.../MainActivity.kt` — 화면
- 파이썬은 [Chaquopy](https://chaquo.com/chaquopy/)로 앱에 넣습니다.

원리는 "가설을 세우고 → 그려 보고 → 원본과 겹쳐 본다"입니다. OCR이 읽은 글을 후보 글꼴로 그려서 원본과 가장 잘 겹치는
글꼴·크기·위치를 고르고, 잘 겹치지 않는 줄은 글자로 바꾸지 않고 원래 모양대로 둡니다.

## 글꼴

글자를 텍스트로 되살리려면 원본과 같은(또는 닮은) 글꼴이 기기에 있어야 합니다. 찾는 곳은 세 군데입니다.

- 기기에 깔린 글꼴(`/system/fonts`)
- 앱에 함께 넣은 Noto Sans KR·Noto Serif KR(여러 굵기, SIL Open Font License 1.1)
- 앱의 "글꼴 추가"로 넣은 글꼴 파일(.ttf .otf .ttc)

맞는 글꼴이 없으면 가장 닮은 글꼴로 대신하거나, 충분히 겹치지 않는 줄은 글자로 바꾸지 않고 모양 그대로 둡니다.

## 빌드

`main`에 올리면 GitHub Actions가 APK를 만들어 Releases에 올리고, 에뮬레이터에서 예시 사진을 변환해 봅니다.
결과는 `ci-build`, `ci-test` 브랜치에 남습니다.

- numpy·OpenCV·Pillow는 Chaquopy 저장소에 올라온 안드로이드용 빌드만 쓸 수 있어서, `ci/pick_wheels.py`가
  세 패키지가 모두 있는 파이썬 버전과 패키지 버전을 골라 줍니다(현재 파이썬 3.10, OpenCV 4.5.1).
- 함께 넣는 글꼴은 저장소에 두지 않고 빌드할 때 `ci/fetch_fonts.sh`로 받습니다.

`app/scan2svg-prototype.jks`는 시제품용 서명 키로, 비밀이 아닙니다(빌드마다 같은 키로 서명해야 덮어 설치가 됩니다).

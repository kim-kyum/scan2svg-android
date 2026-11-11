#!/bin/bash
# 에뮬레이터에 앱과 테스트를 깔고, 예시 사진을 실제로 변환해 본 뒤 결과물을 꺼낸다.
PKG=io.github.kimkyum.scan2svg
mkdir -p ci-out
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb install -r app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk
adb logcat -c
adb shell am instrument -w -e class $PKG.PipelineTest $PKG.test/androidx.test.runner.AndroidJUnitRunner 2>&1 | tee ci-out/instrument.txt
adb logcat -d -v time > logcat-full.txt
grep -E "scan2svg|python|AndroidRuntime|FATAL|chaquopy|TestRunner" logcat-full.txt | tail -n 500 > ci-out/logcat.txt
adb pull /sdcard/Android/data/$PKG/files/out ci-out/out || adb shell ls -R /sdcard/Android/data/$PKG > ci-out/ls.txt 2>&1
adb shell ls -l /system/fonts > ci-out/system-fonts.txt 2>&1
grep -q "OK (1 test)" ci-out/instrument.txt

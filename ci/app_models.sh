#!/bin/bash
# 앱에 넣을 수식 모델을 준비한다: 찾기(MFD)는 그대로, 읽기(MFR)는 int8로 줄인 판.
python3 -m pip install -q onnxruntime onnx || echo "FAIL pip onnxruntime"
bash ci/fetch_models.sh models-src > models-src.log 2>&1 || true
grep -E "^(ok|FAIL|int8)" models-src.log
D=app/src/main/assets/models
mkdir -p "$D"
rm -f "$D"/*
cp models-src/mfd.onnx "$D"/ 2>/dev/null
cp models-src/q/mfr_encoder.onnx models-src/q/mfr_decoder.onnx "$D"/ 2>/dev/null
cp models-src/*.json "$D"/ 2>/dev/null
ls -la "$D"
du -sh "$D"
exit 0

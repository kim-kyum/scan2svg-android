#!/bin/bash
# 수식 모델(Pix2Text MFD·MFR, MIT)을 받아 앱에 넣을 이름으로 둔다. MFR은 int8로 줄인 판(q/)도 만든다.
# 사용: fetch_models.sh <폴더>
D=${1:-models}
mkdir -p "$D" "$D/q"
HF=https://huggingface.co/breezedeus
get() { curl -fsSL --retry 3 -o "$D/$2" "$HF/$1" && echo "ok   $2 $(stat -c %s "$D/$2")" || echo "FAIL $2"; }
get pix2text-mfd/resolve/main/mfd-v20240618.onnx mfd.onnx
get pix2text-mfr/resolve/main/encoder_model.onnx mfr_encoder.onnx
get pix2text-mfr/resolve/main/decoder_model.onnx mfr_decoder.onnx
for f in config.json generation_config.json preprocessor_config.json tokenizer.json tokenizer_config.json special_tokens_map.json; do
  get pix2text-mfr/resolve/main/$f $f
done
curl -fsSL --retry 3 -o "$D/STIXTwoMath-Regular.ttf" "https://raw.githubusercontent.com/google/fonts/main/ofl/stixtwomath/STIXTwoMath-Regular.ttf" || echo "FAIL stix"
python3 - "$D" <<'PY'
import sys, os
from onnxruntime.quantization import quantize_dynamic, QuantType
d = sys.argv[1]
for n in ("mfr_encoder", "mfr_decoder"):
    try:
        # 행렬곱(MatMul)만 줄인다: 안드로이드용 ONNX Runtime에는 ConvInteger 구현이 없다
        quantize_dynamic(os.path.join(d, n + ".onnx"), os.path.join(d, "q", n + ".onnx"), weight_type=QuantType.QInt8,
                         op_types_to_quantize=["MatMul"])
        print("int8", n, os.path.getsize(os.path.join(d, "q", n + ".onnx")))
    except Exception as e:
        print("FAIL int8", n, e)
PY
for f in preprocessor_config.json generation_config.json config.json tokenizer.json; do echo "== $f"; head -c 1500 "$D/$f"; echo; done
ls -la "$D" "$D/q"

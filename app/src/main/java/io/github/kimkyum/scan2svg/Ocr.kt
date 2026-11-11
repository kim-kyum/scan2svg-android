package io.github.kimkyum.scan2svg

import android.graphics.BitmapFactory
import com.google.android.gms.tasks.Tasks
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.korean.KoreanTextRecognizerOptions
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 기기 안에서 도는 글자 인식(ML Kit, 한국어 모델 — 한글과 로마자·숫자를 함께 읽는다).
 * 파이썬 쪽은 여기서 '후보'만 받아 가고, 그 후보가 맞는지는 글꼴로 다시 그려 원본과 겹쳐 보고 판단한다.
 */
object Ocr {
    private val recognizer by lazy {
        TextRecognition.getClient(KoreanTextRecognizerOptions.Builder().build())
    }

    /** PNG 파일 하나를 읽어 {"words":[{text,x,y,w,h,conf}], "chars":[[글자,왼쪽,오른쪽],…]} JSON으로 돌려준다. 메인 스레드에서 부르면 안 된다. */
    @JvmStatic
    fun recognize(path: String): String {
        val words = JSONArray()
        val chars = JSONArray()
        val bmp = BitmapFactory.decodeFile(path)
        if (bmp != null) {
            val text = Tasks.await(recognizer.process(InputImage.fromBitmap(bmp, 0)), 120, TimeUnit.SECONDS)
            for (block in text.textBlocks) {
                for (line in block.lines) {
                    for (el in line.elements) {
                        val b = el.boundingBox ?: continue
                        words.put(
                            JSONObject()
                                .put("text", el.text)
                                .put("x", b.left).put("y", b.top)
                                .put("w", b.width()).put("h", b.height())
                                .put("conf", (el.confidence * 100f).toDouble())
                        )
                        for (sym in el.symbols) {
                            val sb = sym.boundingBox ?: continue
                            chars.put(JSONArray().put(sym.text).put(sb.left).put(sb.right))
                        }
                    }
                }
            }
            bmp.recycle()
        }
        return JSONObject().put("words", words).put("chars", chars).toString()
    }
}

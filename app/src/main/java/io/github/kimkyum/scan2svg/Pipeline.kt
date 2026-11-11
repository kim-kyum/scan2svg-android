package io.github.kimkyum.scan2svg

import android.content.Context
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONObject
import java.io.File

/** 사진 → SVG 변환. 실제 일은 앱에 들어 있는 파이썬(scan2svg.py)이 한다. */
object Pipeline {
    class Result(
        val ok: Boolean,
        val message: String,
        val svg: File,
        val compare: File,
        val stats: JSONObject?,
    )

    /** 파이썬이 부르는 쪽: 진행 메시지 전달과 OCR. */
    class Bridge(private val onLog: (String) -> Unit) {
        fun log(s: String) {
            Log.i(TAG, s)
            onLog(s)
        }

        fun ocr(path: String): String = Ocr.recognize(path)
    }

    const val TAG = "scan2svg"

    fun python(ctx: Context): Python {
        if (!Python.isStarted()) Python.start(AndroidPlatform(ctx.applicationContext))
        return Python.getInstance()
    }

    /** 글꼴을 찾을 폴더: 사용자가 앱에 추가한 글꼴(시스템 글꼴 폴더는 파이썬 쪽에서 알아서 본다). */
    fun userFontDir(ctx: Context): File = File(ctx.filesDir, "fonts").apply { mkdirs() }

    fun outDir(ctx: Context): File = File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "out").apply { mkdirs() }

    fun versions(ctx: Context): String =
        python(ctx).getModule("android_main").callAttr("versions").toString()

    /** 오래 걸린다(수십 초). 반드시 백그라운드 스레드에서 부른다. */
    fun run(ctx: Context, input: File, onLog: (String) -> Unit): Result {
        val out = outDir(ctx)
        val svg = File(out, "result.svg")
        val compare = File(out, "result_compare.jpg")
        return try {
            listOf(svg, compare, File(out, "result_preview.png"), File(out, "result_report.json")).forEach { it.delete() }
            val cache = File(ctx.cacheDir, "scan2svg").apply { mkdirs() }
            val json = python(ctx).getModule("android_main").callAttr(
                "run",
                input.absolutePath,
                out.absolutePath,
                arrayOf(userFontDir(ctx).absolutePath),
                cache.absolutePath,
                Bridge(onLog),
            ).toString()
            val o = JSONObject(json)
            if (o.optBoolean("ok") && svg.exists()) {
                Result(true, "", svg, compare, o.optJSONObject("stats"))
            } else {
                Result(false, o.optString("error", "결과 파일이 만들어지지 않았습니다") + "\n" + o.optString("trace"), svg, compare, null)
            }
        } catch (e: Throwable) {
            Log.e(TAG, "pipeline failed", e)
            Result(false, e.toString(), svg, compare, null)
        }
    }
}

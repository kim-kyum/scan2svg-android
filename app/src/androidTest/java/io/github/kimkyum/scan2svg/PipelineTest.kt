package io.github.kimkyum.scan2svg

import android.content.Intent
import android.graphics.Bitmap
import android.os.SystemClock
import android.view.MotionEvent
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * 기기(에뮬레이터)에서 실제로 해 본다: 예시 사진 변환 → 쪽 PDF → 필기 넣어 여러 쪽 PDF 내보내기 → 화면 띄우고 펜으로 그어 보기.
 * 결과물은 앱의 out 폴더에 남겨 CI가 꺼내 간다.
 */
@RunWith(AndroidJUnit4::class)
class PipelineTest {
    private val inst = InstrumentationRegistry.getInstrumentation()
    private val ctx = inst.targetContext
    private val out = Pipeline.outDir(ctx)
    private val log = StringBuilder()

    private fun note(s: String) {
        log.appendLine(s)
        File(out, "log.txt").writeText(log.toString())
    }

    private fun shot(name: String) {
        SystemClock.sleep(1500)
        val b: Bitmap? = inst.uiAutomation.takeScreenshot()
        if (b != null) File(out, name).outputStream().use { b.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    @Test
    fun scanDocumentAndWrite() {
        note("versions: " + Pipeline.versions(ctx))

        // 1) 문서함에 새 묶음을 만들고 예시 사진을 넣어 변환
        val doc = Store.create(ctx, "검사 묶음")
        val input = File(ctx.cacheDir, "sample.jpg")
        ctx.assets.open("sample_photo.jpg").use { i -> input.outputStream().use { i.copyTo(it) } }
        val page = Store.addPage(doc, input)
        val t0 = SystemClock.elapsedRealtime()
        val r = Pipeline.runPage(ctx, page.src(doc), page.dir(doc)) { note(it) }
        note("runPage ${(SystemClock.elapsedRealtime() - t0) / 1000.0}s ok=${r.optBoolean("ok")} error=${r.optString("error")}")
        note("stats: " + r.optJSONObject("stats")?.toString())
        assertTrue(r.optString("error") + r.optString("trace"), r.optBoolean("ok"))
        assertTrue("쪽 PDF가 없다: " + r.optJSONObject("stats")?.optString("pdf_error"), page.pdf(doc).exists())
        val st = r.getJSONObject("stats")
        page.status = Page.STATUS_DONE
        page.widthPx = st.getJSONArray("page_px").getInt(0)
        page.heightPx = st.getJSONArray("page_px").getInt(1)
        page.textLines = st.optJSONObject("counts")?.optInt("text_lines") ?: 0
        Store.save(doc)
        listOf("result.svg", "page.pdf", "result_compare.jpg", "result_report.json", "result_scan.jpg").forEach {
            File(page.dir(doc), it).takeIf { f -> f.exists() }?.copyTo(File(out, it), overwrite = true)
        }

        // 2) 같은 쪽을 하나 더(스캔 바탕)로 복제하고 필기를 넣어 2쪽 PDF로 내보내기
        val page2 = Page(Store.newId(), Page.STATUS_DONE, Page.BG_SCAN, "", page.widthPx, page.heightPx)
        page.dir(doc).copyRecursively(page2.dir(doc), overwrite = true)
        doc.pages.add(page2)
        Store.save(doc)
        val pts = JSONArray()
        for (i in 0..40) {
            val t = i / 40.0
            pts.put(200 + 1000 * t).put(1650 + 60 * Math.sin(t * 8)).put(0.2 + 0.8 * t)
        }
        val ink = JSONObject().put("version", 1).put("strokes", JSONArray()
            .put(JSONObject().put("t", "pen").put("c", "#1a4fd6").put("w", 6).put("a", 1).put("pts", pts))
            .put(JSONObject().put("t", "hl").put("c", "#ffd400").put("w", 30).put("a", 0.4)
                .put("pts", JSONArray().put(130).put(985).put(1).put(1500).put(985).put(1))))
        page.ink(doc).writeText(ink.toString())
        page2.ink(doc).writeText(ink.toString())
        val pdf = File(out, "export_2pages.pdf")
        val t1 = SystemClock.elapsedRealtime()
        val e = Pipeline.exportPdf(ctx, doc, doc.pages, pdf)
        note("export ${(SystemClock.elapsedRealtime() - t1) / 1000.0}s: $e")
        assertTrue(e.toString(), e.optBoolean("ok") && pdf.length() > 1000)

        // 3) 화면: 문서함 → 묶음 열기 → 펜으로 긋기 → 저장 확인
        val lib = inst.startActivitySync(Intent(ctx, LibraryActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        shot("screen_library.png")
        val act = inst.startActivitySync(Intent(ctx, DocActivity::class.java)
            .putExtra(DocActivity.EXTRA_DOC, doc.id).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) as DocActivity
        shot("screen_doc.png")
        val dm = ctx.resources.displayMetrics
        val y0 = dm.heightPixels * 0.55f
        stylusLine(dm.widthPixels * 0.25f, y0, dm.widthPixels * 0.75f, y0 + 60f, hold = false)
        // 끝에서 멈춰 도형 보정(원)
        stylusCircle(dm.widthPixels * 0.5f, dm.heightPixels * 0.4f, dm.widthPixels * 0.12f)
        shot("screen_doc_ink.png")
        inst.runOnMainSync { act.finish() }
        SystemClock.sleep(1500)
        val saved = Ink.load(page.ink(doc))
        note("ink after drawing: ${saved.size} strokes (before 2); last tool=${saved.lastOrNull()?.tool} points=${saved.lastOrNull()?.n}")
        inst.runOnMainSync { lib.finish() }
        assertTrue("펜으로 그은 획이 저장되지 않았다", saved.size >= 3)
        note("OK")
    }

    private fun stylusEvent(down: Long, action: Int, x: Float, y: Float, p: Float): MotionEvent {
        val props = arrayOf(MotionEvent.PointerProperties().apply { id = 0; toolType = MotionEvent.TOOL_TYPE_STYLUS })
        val coords = arrayOf(MotionEvent.PointerCoords().apply { this.x = x; this.y = y; pressure = p; size = 0.01f })
        return MotionEvent.obtain(down, SystemClock.uptimeMillis(), action, 1, props, coords, 0, 0, 1f, 1f, 0, 0,
            android.view.InputDevice.SOURCE_STYLUS, 0)
    }

    private fun stylusLine(x0: Float, y0: Float, x1: Float, y1: Float, hold: Boolean) {
        val down = SystemClock.uptimeMillis()
        inst.sendPointerSync(stylusEvent(down, MotionEvent.ACTION_DOWN, x0, y0, 0.3f))
        for (i in 1..30) {
            val t = i / 30f
            inst.sendPointerSync(stylusEvent(down, MotionEvent.ACTION_MOVE, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t + 15f * Math.sin(t * 9.0).toFloat(), 0.3f + 0.6f * t))
            SystemClock.sleep(8)
        }
        if (hold) SystemClock.sleep(900)
        inst.sendPointerSync(stylusEvent(down, MotionEvent.ACTION_UP, x1, y1, 0.5f))
    }

    private fun stylusCircle(cx: Float, cy: Float, r: Float) {
        val down = SystemClock.uptimeMillis()
        inst.sendPointerSync(stylusEvent(down, MotionEvent.ACTION_DOWN, cx + r, cy, 0.6f))
        for (i in 1..48) {
            val a = 2 * Math.PI * i / 48
            val rr = r * (1 + 0.06f * Math.sin(a * 5).toFloat())
            inst.sendPointerSync(stylusEvent(down, MotionEvent.ACTION_MOVE, cx + rr * Math.cos(a).toFloat(), cy + rr * 0.9f * Math.sin(a).toFloat(), 0.6f))
            SystemClock.sleep(8)
        }
        SystemClock.sleep(1000)   // 멈춤 → 도형 보정
        inst.sendPointerSync(stylusEvent(down, MotionEvent.ACTION_UP, cx + r, cy, 0.6f))
    }
}

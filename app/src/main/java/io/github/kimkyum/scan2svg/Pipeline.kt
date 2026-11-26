package io.github.kimkyum.scan2svg

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.Executors

/** 사진 → SVG·PDF 변환. 실제 일은 앱에 들어 있는 파이썬(scan2svg.py, svg2pdf.py)이 한다. */
object Pipeline {
    const val TAG = "scan2svg"

    /** 파이썬이 부르는 쪽: 진행 메시지 전달과 OCR. */
    class Bridge(private val onLog: (String) -> Unit) {
        fun log(s: String) {
            Log.i(TAG, s)
            onLog(s)
        }

        fun ocr(path: String): String = Ocr.recognize(path)
    }

    fun python(ctx: Context): Python {
        if (!Python.isStarted()) Python.start(AndroidPlatform(ctx.applicationContext))
        return Python.getInstance()
    }

    /** 사용자가 앱에 추가한 글꼴 */
    fun userFontDir(ctx: Context): File = File(ctx.filesDir, "fonts").apply { mkdirs() }

    /** 앱에 함께 넣은 글꼴(assets/fonts)을 파일로 풀어 둔 폴더. 파이썬은 파일 경로로만 글꼴을 읽을 수 있다. */
    fun bundledFontDir(ctx: Context): File {
        val dir = File(ctx.filesDir, "bundled_fonts").apply { mkdirs() }
        val stamp = File(dir, ".stamp")
        val version = try {
            ctx.packageManager.getPackageInfo(ctx.packageName, 0).lastUpdateTime.toString()
        } catch (e: Exception) {
            "0"
        }
        synchronized(this) {
            if (!stamp.exists() || stamp.readText() != version) {
                dir.listFiles()?.forEach { it.delete() }
                for (name in ctx.assets.list("fonts") ?: emptyArray()) {
                    ctx.assets.open("fonts/$name").use { i -> File(dir, name).outputStream().use { i.copyTo(it) } }
                }
                stamp.writeText(version)
            }
        }
        return dir
    }

    fun fontDirs(ctx: Context): Array<String> =
        arrayOf(userFontDir(ctx).absolutePath, bundledFontDir(ctx).absolutePath)

    fun cacheDir(ctx: Context) = File(ctx.cacheDir, "scan2svg").apply { mkdirs() }

    /** 검사·내보내기용 폴더(앱 외부 저장공간, 앱을 지우면 같이 지워진다) */
    fun outDir(ctx: Context): File = File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "out").apply { mkdirs() }

    fun versions(ctx: Context): String =
        python(ctx).getModule("android_main").callAttr("versions").toString()

    /** 검사용: 글꼴 표본 그림을 out 폴더에 만들고 글꼴별 메모를 돌려준다. */
    fun fontProbe(ctx: Context, extraDirs: List<File>): String =
        python(ctx).getModule("android_main").callAttr(
            "font_probe",
            outDir(ctx).absolutePath,
            (listOf(bundledFontDir(ctx)) + extraDirs).map { it.absolutePath }.toTypedArray(),
        ).toString()

    /** 사진 한 장을 변환해 page_dir에 결과를 남긴다. 오래 걸린다(수십 초): 백그라운드 스레드에서만 부른다. */
    fun runPage(ctx: Context, input: File, pageDir: File, onLog: (String) -> Unit): JSONObject = try {
        pageDir.mkdirs()
        JSONObject(
            python(ctx).getModule("android_main").callAttr(
                "run_page", input.absolutePath, pageDir.absolutePath, fontDirs(ctx), cacheDir(ctx).absolutePath, Bridge(onLog),
            ).toString()
        )
    } catch (e: Throwable) {
        Log.e(TAG, "pipeline failed", e)
        JSONObject().put("ok", false).put("error", e.toString())
    }

    /** 여러 쪽(필기 포함)을 PDF 하나로. 백그라운드 스레드에서만 부른다. */
    fun exportPdf(ctx: Context, doc: Doc, pages: List<Page>, out: File): JSONObject = try {
        val arr = JSONArray()
        for (p in pages) {
            if (!p.svg(doc).exists()) continue
            val o = JSONObject().put("svg", p.svg(doc).absolutePath)
            if (p.ink(doc).exists()) o.put("ink", p.ink(doc).absolutePath)
            if (p.bg == Page.BG_SCAN && p.scan(doc).exists()) o.put("scan", p.scan(doc).absolutePath)
            arr.put(o)
        }
        out.parentFile?.mkdirs()
        val spec = JSONObject().put("title", doc.title).put("pages", arr)
        JSONObject(
            python(ctx).getModule("android_main").callAttr(
                "export_pdf", spec.toString(), out.absolutePath, fontDirs(ctx), cacheDir(ctx).absolutePath,
            ).toString()
        )
    } catch (e: Throwable) {
        Log.e(TAG, "export failed", e)
        JSONObject().put("ok", false).put("error", e.toString())
    }
}

/**
 * 변환 대기열. 쪽을 하나씩 차례로 변환하고, 바뀔 때마다 화면에 알린다(메인 스레드).
 * 앱을 다시 열면 끝나지 않은 쪽을 이어서 변환한다.
 */
object Engine {
    interface Listener {
        fun onPageChanged(docId: String, pageId: String)
    }

    private val worker = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val listeners = mutableSetOf<Listener>()
    private val queued = mutableSetOf<String>()
    private var resumed = false

    fun addListener(l: Listener) = synchronized(listeners) { listeners.add(l) }
    fun removeListener(l: Listener) = synchronized(listeners) { listeners.remove(l) }

    private fun notify(doc: Doc, page: Page) {
        val ls = synchronized(listeners) { listeners.toList() }
        main.post { ls.forEach { it.onPageChanged(doc.id, page.id) } }
    }

    /** 앱을 열 때 한 번: 지난번에 끝나지 않은 쪽을 다시 대기열에 넣는다. */
    fun resume(ctx: Context) {
        if (resumed) return
        resumed = true
        for (d in Store.all(ctx)) for (p in d.pages) {
            if (p.status == Page.STATUS_QUEUED || p.status == Page.STATUS_RUNNING) enqueue(ctx, d, p)
        }
    }

    fun enqueue(ctx: Context, doc: Doc, page: Page) {
        val key = doc.id + "/" + page.id
        synchronized(queued) { if (!queued.add(key)) return }
        page.status = Page.STATUS_QUEUED
        page.message = "변환 대기 중"
        Store.save(doc)
        notify(doc, page)
        val app = ctx.applicationContext
        worker.execute {
            try {
                if (Store.get(app, doc.id)?.page(page.id) == null) return@execute  // 그 사이 지워짐
                page.status = Page.STATUS_RUNNING
                page.message = "변환 시작"
                Store.save(doc)
                notify(doc, page)
                var last = 0L
                val r = Pipeline.runPage(app, page.src(doc), page.dir(doc)) { msg ->
                    page.message = msg.trim().lines().firstOrNull() ?: ""
                    val now = System.currentTimeMillis()
                    if (now - last > 300) {
                        last = now
                        notify(doc, page)
                    }
                }
                File(page.dir(doc), "log.txt").writeText(r.toString(1))
                if (r.optBoolean("ok") && page.svg(doc).exists()) {
                    val st = r.optJSONObject("stats") ?: JSONObject()
                    val px = st.optJSONArray("page_px")
                    page.widthPx = px?.optInt(0) ?: 0
                    page.heightPx = px?.optInt(1) ?: 0
                    page.seconds = st.optDouble("total_sec", 0.0)
                    page.textLines = st.optJSONObject("counts")?.optInt("text_lines") ?: 0
                    page.status = Page.STATUS_DONE
                    page.message = "완료 — 글자 ${page.textLines}줄 (${"%.0f".format(page.seconds)}초)"
                } else {
                    page.status = Page.STATUS_FAILED
                    page.message = "변환 실패: " + r.optString("error")
                }
            } catch (e: Throwable) {
                page.status = Page.STATUS_FAILED
                page.message = "변환 실패: $e"
            } finally {
                synchronized(queued) { queued.remove(key) }
                if (Store.get(app, doc.id)?.page(page.id) != null) Store.save(doc)
                notify(doc, page)
            }
        }
    }

    /** 백그라운드에서 일을 하고 결과를 메인 스레드로 돌려준다(내보내기 등). 변환과 같은 줄에 서지 않도록 따로 돈다. */
    private val side = Executors.newSingleThreadExecutor()
    fun <T> background(work: () -> T, done: (T) -> Unit) {
        side.execute {
            val r = work()
            main.post { done(r) }
        }
    }
}

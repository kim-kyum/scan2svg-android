package io.github.kimkyum.scan2svg

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * 문서함: 스캔한 쪽들을 앱 안(앱 전용 저장공간)에 묶음 단위로 보관한다.
 *
 *  files/docs/<묶음 id>/doc.json            제목, 쪽 순서, 쪽별 상태
 *  files/docs/<묶음 id>/<쪽 id>/src.jpg     찍은(가져온) 사진 그대로
 *                               result.svg   재구성 결과
 *                               result_scan.jpg  펴고 보정한 사진(재구성과 같은 크기·좌표)
 *                               page.pdf     재구성 결과의 PDF 한 쪽(화면 표시용)
 *                               ink.json     필기
 */
class Page(
    val id: String,
    var status: String = STATUS_QUEUED,   // queued, running, done, failed
    var bg: String = BG_VECTOR,           // 화면·PDF의 바탕: vector(재구성) 또는 scan(보정한 사진)
    var message: String = "",             // 마지막 진행 메시지나 오류
    var widthPx: Int = 0,
    var heightPx: Int = 0,
    var seconds: Double = 0.0,
    var textLines: Int = 0,
) {
    fun dir(doc: Doc) = File(doc.dir, id)
    fun src(doc: Doc) = File(dir(doc), "src.jpg")
    fun svg(doc: Doc) = File(dir(doc), "result.svg")
    fun scan(doc: Doc) = File(dir(doc), "result_scan.jpg")
    fun pdf(doc: Doc) = File(dir(doc), "page.pdf")
    fun preview(doc: Doc) = File(dir(doc), "result_preview.png")
    fun ink(doc: Doc) = File(dir(doc), "ink.json")
    fun report(doc: Doc) = File(dir(doc), "result_report.json")
    val done get() = status == STATUS_DONE

    fun toJson(): JSONObject = JSONObject()
        .put("id", id).put("status", status).put("bg", bg).put("message", message)
        .put("w", widthPx).put("h", heightPx).put("sec", seconds).put("lines", textLines)

    companion object {
        const val STATUS_QUEUED = "queued"
        const val STATUS_RUNNING = "running"
        const val STATUS_DONE = "done"
        const val STATUS_FAILED = "failed"
        const val BG_VECTOR = "vector"
        const val BG_SCAN = "scan"

        fun fromJson(o: JSONObject) = Page(
            o.getString("id"), o.optString("status", STATUS_QUEUED), o.optString("bg", BG_VECTOR),
            o.optString("message"), o.optInt("w"), o.optInt("h"), o.optDouble("sec", 0.0), o.optInt("lines"),
        )
    }
}

class Doc(val id: String, val dir: File, var title: String, val created: Long, val pages: MutableList<Page>) {
    fun page(id: String) = pages.firstOrNull { it.id == id }

    fun save() {
        dir.mkdirs()
        val o = JSONObject().put("id", id).put("title", title).put("created", created)
            .put("pages", JSONArray().apply { pages.forEach { put(it.toJson()) } })
        val tmp = File(dir, "doc.json.tmp")
        tmp.writeText(o.toString(1))
        tmp.renameTo(File(dir, "doc.json"))
    }
}

object Store {
    private val lock = Any()
    private val docs = LinkedHashMap<String, Doc>()
    private var loaded = false

    fun root(ctx: Context) = File(ctx.filesDir, "docs").apply { mkdirs() }

    fun all(ctx: Context): List<Doc> = synchronized(lock) {
        load(ctx)
        docs.values.sortedByDescending { it.created }
    }

    fun get(ctx: Context, id: String): Doc? = synchronized(lock) {
        load(ctx)
        docs[id]
    }

    private fun load(ctx: Context) {
        if (loaded) return
        loaded = true
        for (d in root(ctx).listFiles() ?: emptyArray()) {
            val f = File(d, "doc.json")
            if (!f.exists()) continue
            try {
                val o = JSONObject(f.readText())
                val arr = o.getJSONArray("pages")
                val pages = MutableList(arr.length()) { Page.fromJson(arr.getJSONObject(it)) }
                docs[o.getString("id")] = Doc(o.getString("id"), d, o.optString("title", "문서"), o.optLong("created"), pages)
            } catch (e: Exception) {
                android.util.Log.w(Pipeline.TAG, "문서 읽기 실패 $d", e)
            }
        }
    }

    fun create(ctx: Context, title: String? = null): Doc = synchronized(lock) {
        load(ctx)
        val id = newId()
        val now = System.currentTimeMillis()
        val t = title ?: ("스캔 " + SimpleDateFormat("M월 d일 HH:mm", Locale.KOREA).format(Date(now)))
        val doc = Doc(id, File(root(ctx), id), t, now, mutableListOf())
        docs[id] = doc
        doc.save()
        doc
    }

    /** 사진 파일을 새 쪽으로 넣는다(복사). 변환은 Engine이 따로 한다. */
    fun addPage(doc: Doc, photo: File): Page = synchronized(lock) {
        val p = Page(newId())
        p.dir(doc).mkdirs()
        photo.copyTo(p.src(doc), overwrite = true)
        doc.pages.add(p)
        doc.save()
        p
    }

    fun delete(ctx: Context, doc: Doc) = synchronized(lock) {
        docs.remove(doc.id)
        doc.dir.deleteRecursively()
    }

    fun deletePage(doc: Doc, page: Page) = synchronized(lock) {
        doc.pages.remove(page)
        page.dir(doc).deleteRecursively()
        doc.save()
    }

    fun save(doc: Doc) = synchronized(lock) { doc.save() }

    fun newId(): String = UUID.randomUUID().toString().replace("-", "").substring(0, 12)

    /** 파일 이름으로 쓸 수 있게 다듬은 제목 */
    fun fileName(title: String): String =
        title.replace(Regex("[\\\\/:*?\"<>|\\n\\r\\t]"), " ").trim().ifEmpty { "scan" }.take(60)
}

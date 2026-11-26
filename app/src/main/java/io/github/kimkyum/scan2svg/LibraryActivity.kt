package io.github.kimkyum.scan2svg

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.BaseAdapter
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ListView
import android.widget.TextView
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

/** 첫 화면: 문서함. 새로 찍기·가져오기, 묶음 목록. */
class LibraryActivity : Activity(), Engine.Listener {
    private lateinit var list: ListView
    private lateinit var empty: TextView
    private lateinit var fontInfo: TextView
    private val adapter = DocAdapter()
    private val thumbs = HashMap<String, Bitmap?>()
    private val thumbPool = Executors.newSingleThreadExecutor()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Ui.BG
        val root = Ui.col(this).apply {
            setBackgroundColor(Ui.BG)
            val p = Ui.dpi(this@LibraryActivity, 16)
            setPadding(p, p, p, 0)
        }
        root.addView(Ui.text(this, "문서함", 26f, bold = true))
        root.addView(Ui.text(this, "찍은 문서를 글자·도형·그림으로 다시 그려 앱 안에 보관합니다. 쪽마다 확대해 확인하고, 바로 필기하고, 묶어서 PDF로 내보낼 수 있습니다.",
            14f, Ui.DIM).apply { setPadding(0, Ui.dpi(context, 6), 0, Ui.dpi(context, 12)) })

        val actions = Ui.row(this)
        actions.addView(Ui.button(this, "사진 찍기", 0) { newDoc(DocActivity.START_CAMERA) }, Ui.lp(0, -2, 1f, Ui.dpi(this, 4)))
        actions.addView(Ui.button(this, "사진 가져오기", 1) { newDoc(DocActivity.START_IMPORT) }, Ui.lp(0, -2, 1f, Ui.dpi(this, 4)))
        actions.addView(Ui.button(this, "예시로 해보기", 2) { newDoc(DocActivity.START_SAMPLE) }, Ui.lp(0, -2, 1f, Ui.dpi(this, 4)))
        root.addView(actions)

        val fonts = Ui.row(this).apply { setPadding(0, Ui.dpi(context, 4), 0, Ui.dpi(context, 8)) }
        fonts.addView(Ui.button(this, "글꼴 추가", 2) { pickFonts() })
        fontInfo = Ui.text(this, "", 13f, Ui.DIM).apply { setPadding(Ui.dpi(context, 10), 0, 0, 0) }
        fonts.addView(fontInfo, Ui.lp(0, -2, 1f))
        root.addView(fonts)

        list = ListView(this).apply {
            divider = null
            dividerHeight = Ui.dpi(context, 8)
            adapter = this@LibraryActivity.adapter
            setOnItemClickListener { _, _, pos, _ -> openDoc(this@LibraryActivity.adapter.getItem(pos).id) }
            setOnItemLongClickListener { _, _, pos, _ -> docMenu(this@LibraryActivity.adapter.getItem(pos)); true }
        }
        empty = Ui.text(this, "아직 문서가 없습니다. 위에서 사진을 찍거나 가져와 보세요.", 15f, Ui.DIM).apply {
            gravity = Gravity.CENTER; setPadding(0, Ui.dpi(context, 40), 0, 0)
        }
        root.addView(empty)
        root.addView(list, Ui.lp(-1, 0, 1f))
        setContentView(root)

        Engine.resume(this)
        handleShare(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShare(intent)
    }

    override fun onResume() {
        super.onResume()
        Engine.addListener(this)
        refresh()
    }

    override fun onPause() {
        super.onPause()
        Engine.removeListener(this)
    }

    override fun onPageChanged(docId: String, pageId: String) {
        val d = Store.get(this, docId)
        if (d != null && d.pages.firstOrNull()?.id == pageId) thumbs.remove(docId)
        adapter.notifyDataSetChanged()
    }

    private fun refresh() {
        adapter.docs = Store.all(this)
        adapter.notifyDataSetChanged()
        empty.visibility = if (adapter.docs.isEmpty()) View.VISIBLE else View.GONE
        val n = Pipeline.userFontDir(this).listFiles()?.count { it.isFile } ?: 0
        fontInfo.text = "기기 글꼴, 앱에 든 무료 글꼴, 추가한 글꼴 ${n}개 중에서 가장 닮은 글꼴을 고릅니다"
    }

    private fun newDoc(start: String) {
        val d = Store.create(this)
        startActivity(Intent(this, DocActivity::class.java).putExtra(DocActivity.EXTRA_DOC, d.id).putExtra(DocActivity.EXTRA_START, start))
    }

    private fun openDoc(id: String) {
        startActivity(Intent(this, DocActivity::class.java).putExtra(DocActivity.EXTRA_DOC, id))
    }

    /** 다른 앱(갤러리 등)의 '공유'로 받은 사진 → 새 묶음 */
    private fun handleShare(i: Intent?) {
        if (i == null) return
        val uris = ArrayList<Uri>()
        when (i.action) {
            Intent.ACTION_SEND -> (i.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))?.let { uris.add(it) }
            Intent.ACTION_SEND_MULTIPLE -> i.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)?.let { uris.addAll(it) }
            else -> return
        }
        i.action = null
        if (uris.isEmpty()) return
        val d = Store.create(this)
        for (u in uris) DocActivity.importUri(this, d, u)
        openDoc(d.id)
    }

    private fun docMenu(d: Doc) {
        Ui.choose(this, d.title, listOf("열기", "제목 바꾸기", "삭제")) { k ->
            when (k) {
                0 -> openDoc(d.id)
                1 -> rename(d)
                2 -> AlertDialog.Builder(this).setTitle("'${d.title}'을 지울까요?")
                    .setMessage("쪽 ${d.pages.size}개와 필기가 모두 지워집니다.")
                    .setPositiveButton("삭제") { _, _ -> Store.delete(this, d); refresh() }
                    .setNegativeButton("취소", null).show()
            }
        }
    }

    private fun rename(d: Doc) {
        val e = EditText(this).apply { setText(d.title); setSelection(text.length) }
        AlertDialog.Builder(this).setTitle("제목").setView(e)
            .setPositiveButton("확인") { _, _ ->
                d.title = e.text.toString().trim().ifEmpty { d.title }; Store.save(d); refresh()
            }.setNegativeButton("취소", null).show()
    }

    // ───────────── 글꼴 추가 ─────────────
    private fun pickFonts() {
        val i = Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*")
            .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
        startActivityForResult(i, REQ_FONTS)
    }

    @Deprecated("framework API")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_FONTS || resultCode != RESULT_OK || data == null) return
        val uris = ArrayList<Uri>()
        data.clipData?.let { c -> for (k in 0 until c.itemCount) uris.add(c.getItemAt(k).uri) }
        data.data?.let { if (uris.isEmpty()) uris.add(it) }
        var ok = 0
        for (u in uris) {
            val name = DocActivity.displayName(this, u) ?: "font_${System.currentTimeMillis()}.ttf"
            if (!name.lowercase().matches(Regex(".*\\.(ttf|otf|ttc|otc)$"))) continue
            contentResolver.openInputStream(u)?.use { i ->
                File(Pipeline.userFontDir(this), name).outputStream().use { i.copyTo(it) }
            }
            ok++
        }
        // 글꼴 목록 캐시를 지워 다음 변환 때 새로 읽게 한다
        Pipeline.cacheDir(this).listFiles()?.filter { it.name.startsWith("fonts_index") }?.forEach { it.delete() }
        Ui.toast(this, if (ok > 0) "글꼴 ${ok}개를 추가했습니다" else "글꼴 파일(.ttf .otf .ttc)이 아닙니다")
        refresh()
    }

    // ───────────── 목록 ─────────────
    inner class DocAdapter : BaseAdapter() {
        var docs: List<Doc> = emptyList()
        override fun getCount() = docs.size
        override fun getItem(p: Int) = docs[p]
        override fun getItemId(p: Int) = p.toLong()

        override fun getView(p: Int, convert: View?, parent: ViewGroup): View {
            val ctx = this@LibraryActivity
            val row = (convert as? LinearLayout) ?: Ui.row(ctx).apply {
                background = Ui.round(Ui.SURFACE, Ui.dp(ctx, 14f))
                val pd = Ui.dpi(ctx, 10)
                setPadding(pd, pd, pd, pd)
                addView(ImageView(ctx).apply {
                    scaleType = ImageView.ScaleType.CENTER_CROP
                    background = Ui.round(0xFFFFFFFF.toInt(), Ui.dp(ctx, 6f))
                    clipToOutline = true
                }, LinearLayout.LayoutParams(Ui.dpi(ctx, 64), Ui.dpi(ctx, 88)))
                addView(Ui.col(ctx).apply {
                    setPadding(Ui.dpi(ctx, 14), 0, 0, 0)
                    addView(Ui.text(ctx, "", 17f, bold = true))
                    addView(Ui.text(ctx, "", 13f, Ui.DIM))
                }, Ui.lp(0, -2, 1f))
            }
            val d = docs[p]
            val img = row.getChildAt(0) as ImageView
            val texts = row.getChildAt(1) as LinearLayout
            (texts.getChildAt(0) as TextView).text = d.title
            val busy = d.pages.count { it.status == Page.STATUS_QUEUED || it.status == Page.STATUS_RUNNING }
            val failed = d.pages.count { it.status == Page.STATUS_FAILED }
            val date = SimpleDateFormat("yyyy.M.d HH:mm", Locale.KOREA).format(Date(d.created))
            (texts.getChildAt(1) as TextView).text = buildString {
                append("${d.pages.size}쪽 · $date")
                if (busy > 0) append(" · 변환 중 ${busy}쪽")
                if (failed > 0) append(" · 실패 ${failed}쪽")
            }
            img.tag = d.id
            if (thumbs.containsKey(d.id)) img.setImageBitmap(thumbs[d.id]) else {
                img.setImageBitmap(null)
                val first = d.pages.firstOrNull()
                if (first != null) {
                    thumbs[d.id] = null
                    thumbPool.execute {
                        val f = listOf(first.preview(d), first.src(d)).firstOrNull { it.exists() }
                        val b = f?.let { decodeSmall(it, 200) }
                        runOnUiThread {
                            thumbs[d.id] = b
                            if (img.tag == d.id) img.setImageBitmap(b)
                        }
                    }
                }
            }
            return row
        }
    }

    private fun decodeSmall(f: File, target: Int): Bitmap? {
        val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(f.absolutePath, o)
        var s = 1
        while (maxOf(o.outWidth, o.outHeight) / s > target * 2) s *= 2
        return BitmapFactory.decodeFile(f.absolutePath, BitmapFactory.Options().apply { inSampleSize = s })
    }

    companion object {
        const val REQ_FONTS = 6
    }
}

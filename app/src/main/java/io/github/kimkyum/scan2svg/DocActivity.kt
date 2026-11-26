package io.github.kimkyum.scan2svg

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.PopupMenu
import android.widget.ScrollView
import android.widget.TextView
import java.io.File
import java.util.concurrent.Executors

/**
 * 묶음 하나를 보는 화면: 쪽 보기(확대·축소), 필기, 쪽 추가·삭제·순서, 내보내기.
 */
class DocActivity : Activity(), Engine.Listener, PageView.Callback {
    private lateinit var doc: Doc
    private var index = 0
    private lateinit var pageView: PageView
    private lateinit var status: TextView
    private lateinit var counter: TextView
    private lateinit var titleView: TextView
    private lateinit var strip: LinearLayout
    private lateinit var selBar: LinearLayout
    private lateinit var selLabel: TextView
    private lateinit var undoBtn: Button
    private lateinit var redoBtn: Button
    private lateinit var colorBtn: Button
    private val modeButtons = HashMap<PageView.Mode, Button>()
    private val toolButtons = HashMap<Int, Button>()
    private val main = Handler(Looper.getMainLooper())
    private val thumbPool = Executors.newSingleThreadExecutor()
    private var pendingPhoto: File? = null
    private var exportAfter: Int = 0
    private var exportFile: File? = null
    private var shownPage: String? = null
    private var shownStatus: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.statusBarColor = Ui.BG
        val id = intent.getStringExtra(EXTRA_DOC)
        val d = id?.let { Store.get(this, it) }
        if (d == null) { finish(); return }
        doc = d
        savedInstanceState?.getString("pendingPhoto")?.let { pendingPhoto = File(it) }
        getPreferences(MODE_PRIVATE).getString("pendingPhoto", null)?.let { if (pendingPhoto == null) pendingPhoto = File(it) }
        buildUi()
        restoreTools()
        showPage(0)
        if (savedInstanceState == null) when (intent.getStringExtra(EXTRA_START)) {
            START_CAMERA -> takePhoto()
            START_IMPORT -> importPhotos()
            START_SAMPLE -> addSample()
        }
    }

    override fun onSaveInstanceState(out: Bundle) {
        super.onSaveInstanceState(out)
        pendingPhoto?.let { out.putString("pendingPhoto", it.absolutePath) }
    }

    override fun onResume() {
        super.onResume()
        Engine.addListener(this)
        refreshStrip()
        updateStatus()
    }

    override fun onPause() {
        super.onPause()
        Engine.removeListener(this)
        saveInkNow()
        saveTools()
    }

    override fun onDestroy() {
        super.onDestroy()
        if (::pageView.isInitialized) pageView.release()
        thumbPool.shutdown()
    }

    // ───────────── 화면 구성 ─────────────

    private fun buildUi() {
        val ctx = this
        val root = Ui.col(ctx).apply { setBackgroundColor(Ui.BG) }

        // 윗줄: 제목, 보기, 도구
        val bar = Ui.row(ctx).apply { setPadding(Ui.dpi(ctx, 6), Ui.dpi(ctx, 6), Ui.dpi(ctx, 6), Ui.dpi(ctx, 6)) }
        fun add(v: View, gapAfter: Int = 4) {
            // 구분선처럼 크기를 정해 둔 뷰는 그 크기를 지킨다(wrap_content면 높이를 다 차지한다)
            val lp = (v.layoutParams as? LinearLayout.LayoutParams) ?: Ui.lp().apply { marginEnd = Ui.dpi(ctx, gapAfter) }
            bar.addView(v, lp)
        }
        add(Ui.toolButton(ctx, "←") { finish() })
        titleView = Ui.text(ctx, doc.title, 17f, bold = true).apply {
            maxWidth = Ui.dpi(ctx, 220); isSingleLine = true
            ellipsize = android.text.TextUtils.TruncateAt.END
            setPadding(Ui.dpi(ctx, 6), 0, Ui.dpi(ctx, 10), 0)
            setOnClickListener { rename() }
        }
        add(titleView)
        for ((m, label) in listOf(PageView.Mode.VECTOR to "재구성", PageView.Mode.SCAN to "스캔", PageView.Mode.PHOTO to "원본")) {
            val b = Ui.toolButton(ctx, label) { setViewMode(m) }
            modeButtons[m] = b
            add(b, 2)
        }
        add(sep())
        for ((t, label) in listOf(PageView.TOOL_PEN to "펜", PageView.TOOL_HL to "형광펜", PageView.TOOL_ERASER to "지우개",
                PageView.TOOL_LASSO to "올가미")) {
            val b = Ui.toolButton(ctx, label) { setTool(t) }
            toolButtons[t] = b
            add(b, 2)
        }
        colorBtn = Ui.toolButton(ctx, "●") { pickColor() }
        add(colorBtn, 2)
        add(Ui.toolButton(ctx, "굵기") { pickWidth() }, 2)
        add(sep())
        undoBtn = Ui.toolButton(ctx, "↶") { pageView.undo(); updateUndo() }
        redoBtn = Ui.toolButton(ctx, "↷") { pageView.redo(); updateUndo() }
        add(undoBtn, 2); add(redoBtn)
        add(sep())
        add(Ui.toolButton(ctx, "쪽 추가") { addPagesMenu(it) }, 2)
        add(Ui.button(ctx, "PDF 내보내기", 0) { exportDoc() }, 2)
        add(Ui.toolButton(ctx, "⋯") { moreMenu(it) })
        root.addView(HorizontalScrollView(ctx).apply {
            isHorizontalScrollBarEnabled = false
            addView(bar)
            setBackgroundColor(Ui.SURFACE)
        })

        // 선택 막대(올가미로 고른 뒤)
        selBar = Ui.row(ctx).apply {
            setBackgroundColor(Ui.SURFACE2)
            setPadding(Ui.dpi(ctx, 10), Ui.dpi(ctx, 4), Ui.dpi(ctx, 10), Ui.dpi(ctx, 4))
            visibility = View.GONE
        }
        selLabel = Ui.text(ctx, "", 14f)
        selBar.addView(selLabel, Ui.lp(0, -2, 1f))
        for ((label, act) in listOf<Pair<String, () -> Unit>>(
            "삭제" to { pageView.deleteSelection() }, "복제" to { pageView.duplicateSelection() },
            "크게" to { pageView.scaleSelection(1.2f) }, "작게" to { pageView.scaleSelection(1 / 1.2f) },
            "색" to { pickColor(forSelection = true) }, "해제" to { pageView.clearSelection() })) {
            selBar.addView(Ui.toolButton(ctx, label) { act(); updateUndo() }, Ui.lp().apply { marginStart = Ui.dpi(ctx, 4) })
        }
        root.addView(selBar)

        // 쪽
        val frame = FrameLayout(ctx)
        pageView = PageView(ctx).also { it.callback = this }
        frame.addView(pageView, FrameLayout.LayoutParams(-1, -1))
        status = Ui.text(ctx, "", 13f).apply {
            background = Ui.round(0xCC000000.toInt(), Ui.dp(ctx, 12f))
            setPadding(Ui.dpi(ctx, 12), Ui.dpi(ctx, 6), Ui.dpi(ctx, 12), Ui.dpi(ctx, 6))
            setOnClickListener { showLog() }
        }
        frame.addView(status, FrameLayout.LayoutParams(-2, -2, Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL).apply {
            bottomMargin = Ui.dpi(ctx, 10)
        })
        root.addView(frame, Ui.lp(-1, 0, 1f))

        // 아랫줄: 쪽 넘기기와 미리보기 띠
        val bottom = Ui.row(ctx).apply {
            setBackgroundColor(Ui.SURFACE)
            setPadding(Ui.dpi(ctx, 6), Ui.dpi(ctx, 4), Ui.dpi(ctx, 6), Ui.dpi(ctx, 4))
        }
        bottom.addView(Ui.toolButton(ctx, "◀") { showPage(index - 1) })
        strip = Ui.row(ctx)
        bottom.addView(HorizontalScrollView(ctx).apply { addView(strip); isHorizontalScrollBarEnabled = false }, Ui.lp(0, -2, 1f))
        counter = Ui.text(ctx, "", 14f).apply { setPadding(Ui.dpi(ctx, 8), 0, Ui.dpi(ctx, 8), 0) }
        bottom.addView(counter)
        bottom.addView(Ui.toolButton(ctx, "▶") { showPage(index + 1) })
        root.addView(bottom)

        setContentView(root)
    }

    private fun sep() = View(this).apply {
        setBackgroundColor(0x33FFFFFF)
        layoutParams = LinearLayout.LayoutParams(Ui.dpi(context, 1), Ui.dpi(context, 26)).apply {
            marginStart = Ui.dpi(context, 6); marginEnd = Ui.dpi(context, 8)
        }
    }

    // ───────────── 쪽 보기 ─────────────

    private fun current(): Page? = doc.pages.getOrNull(index)

    private fun showPage(i: Int) {
        if (doc.pages.isEmpty()) {
            index = 0
            pageView.setPage(null, null, null, 0, 0, PageView.Mode.PHOTO, mutableListOf())
            updateCounter(); updateStatus(); refreshStrip()
            return
        }
        saveInkNow()
        index = i.coerceIn(0, doc.pages.size - 1)
        val p = doc.pages[index]
        val mode = when {
            !p.done -> PageView.Mode.PHOTO
            p.bg == Page.BG_SCAN -> PageView.Mode.SCAN
            else -> PageView.Mode.VECTOR
        }
        pageView.setPage(p.pdf(doc), p.scan(doc), p.src(doc), p.widthPx, p.heightPx, mode, Ink.load(p.ink(doc)))
        shownPage = p.id
        shownStatus = p.status
        updateModeButtons(); updateCounter(); updateStatus(); updateUndo(); highlightStrip()
    }

    private fun setViewMode(m: PageView.Mode) {
        val p = current() ?: return
        if (!p.done && m != PageView.Mode.PHOTO) {
            Ui.toast(this, "아직 변환 중입니다"); return
        }
        if (m != PageView.Mode.PHOTO) {
            val bg = if (m == PageView.Mode.SCAN) Page.BG_SCAN else Page.BG_VECTOR
            if (p.bg != bg) { p.bg = bg; Store.save(doc) }
        }
        pageView.setMode(m)
        updateModeButtons()
        updateStatus()
    }

    private fun updateModeButtons() {
        for ((m, b) in modeButtons) Ui.setActive(b, m == pageView.mode)
    }

    private fun updateCounter() {
        counter.text = if (doc.pages.isEmpty()) "0쪽" else "${index + 1} / ${doc.pages.size}"
    }

    private fun updateStatus() {
        val p = current()
        val text = when {
            p == null -> "쪽이 없습니다. 위의 '쪽 추가'로 사진을 넣으세요."
            p.status == Page.STATUS_DONE && pageView.mode == PageView.Mode.PHOTO -> "찍은 사진 그대로 — 확대해서 흔들림·초점을 확인하세요"
            p.status == Page.STATUS_DONE && pageView.mode == PageView.Mode.SCAN -> "스캔 보기 — 이 쪽은 PDF에 사진으로 들어갑니다"
            p.status == Page.STATUS_DONE -> p.message
            else -> p.message.ifEmpty { "변환 대기 중" }
        }
        status.text = text
        status.visibility = if (text.isEmpty()) View.GONE else View.VISIBLE
    }

    override fun onPageChanged(docId: String, pageId: String) {
        if (docId != doc.id) return
        val p = doc.page(pageId) ?: return
        if (p.id == shownPage && p.status != shownStatus && (p.status == Page.STATUS_DONE || shownStatus == Page.STATUS_DONE)) {
            showPage(index)   // 변환이 끝나면 재구성 결과로 바꿔 보여 준다
        } else if (p.id == shownPage) {
            shownStatus = p.status
        }
        updateStatus()
        updateThumbLabel(pageId)
    }

    // ───────────── 미리보기 띠 ─────────────

    private fun refreshStrip() {
        if (!::strip.isInitialized) return
        strip.removeAllViews()
        for ((k, p) in doc.pages.withIndex()) {
            val cell = Ui.col(this).apply {
                gravity = Gravity.CENTER_HORIZONTAL
                tag = p.id
                setPadding(Ui.dpi(context, 3), Ui.dpi(context, 3), Ui.dpi(context, 3), Ui.dpi(context, 3))
                setOnClickListener { showPage(doc.pages.indexOfFirst { it.id == p.id }) }
            }
            val img = ImageView(this).apply {
                scaleType = ImageView.ScaleType.CENTER_CROP
                setBackgroundColor(Color.WHITE)
            }
            cell.addView(img, LinearLayout.LayoutParams(Ui.dpi(this, 40), Ui.dpi(this, 56)))
            cell.addView(Ui.text(this, label(k, p), 11f, Ui.DIM))
            strip.addView(cell)
            thumbPool.execute {
                val f = listOf(p.preview(doc), p.src(doc)).firstOrNull { it.exists() }
                val b = f?.let { decodeSmall(it, 80) }
                runOnUiThread { img.setImageBitmap(b) }
            }
        }
        highlightStrip()
        updateCounter()
    }

    private fun label(k: Int, p: Page) = "${k + 1}" + when (p.status) {
        Page.STATUS_DONE -> ""
        Page.STATUS_FAILED -> " 실패"
        Page.STATUS_RUNNING -> " 변환중"
        else -> " 대기"
    }

    private fun updateThumbLabel(pageId: String) {
        for (i in 0 until strip.childCount) {
            val cell = strip.getChildAt(i) as LinearLayout
            if (cell.tag == pageId) {
                val k = doc.pages.indexOfFirst { it.id == pageId }
                val p = doc.pages.getOrNull(k) ?: return
                (cell.getChildAt(1) as TextView).text = label(k, p)
                if (p.done) thumbPool.execute {
                    val b = decodeSmall(p.preview(doc), 80)
                    runOnUiThread { (cell.getChildAt(0) as ImageView).setImageBitmap(b) }
                }
            }
        }
    }

    private fun highlightStrip() {
        for (i in 0 until strip.childCount) {
            strip.getChildAt(i).background = if (i == index) Ui.round(Ui.ACCENT, Ui.dp(this, 6f)) else null
        }
    }

    private fun decodeSmall(f: File, target: Int): Bitmap? {
        if (!f.exists()) return null
        val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(f.absolutePath, o)
        var s = 1
        while (maxOf(o.outWidth, o.outHeight) / s > target * 3) s *= 2
        return BitmapFactory.decodeFile(f.absolutePath, BitmapFactory.Options().apply { inSampleSize = s })
    }

    // ───────────── 필기 도구 ─────────────

    private val colors = listOf(
        "검정" to Color.BLACK, "파랑" to 0xFF1A4FD6.toInt(), "빨강" to 0xFFD62828.toInt(), "초록" to 0xFF2B8A3E.toInt(),
        "보라" to 0xFF7048E8.toInt(), "주황" to 0xFFE8590C.toInt(), "회색" to 0xFF868E96.toInt(),
    )
    private val hlColors = listOf(
        "노랑" to 0xFFFFD400.toInt(), "연두" to 0xFF8CE99A.toInt(), "하늘" to 0xFF74C0FC.toInt(), "분홍" to 0xFFFFA8D8.toInt(),
    )

    private fun setTool(t: Int) {
        pageView.tool = t
        for ((k, b) in toolButtons) Ui.setActive(b, k == t)
        updateColorButton()
    }

    private fun updateColorButton() {
        val c = if (pageView.tool == PageView.TOOL_HL) pageView.hlColor else pageView.penColor
        colorBtn.setTextColor(c or 0xFF000000.toInt())
        if (c == Color.BLACK) colorBtn.setTextColor(Color.WHITE)
    }

    private fun pickColor(forSelection: Boolean = false) {
        val list = if (!forSelection && pageView.tool == PageView.TOOL_HL) hlColors else colors
        Ui.choose(this, "색", list.map { it.first }) { k ->
            val c = list[k].second
            when {
                forSelection -> pageView.recolorSelection(c)
                pageView.tool == PageView.TOOL_HL -> pageView.hlColor = c
                else -> pageView.penColor = c
            }
            updateColorButton()
            updateUndo()
        }
    }

    private fun pickWidth() {
        if (pageView.tool == PageView.TOOL_HL) {
            val ws = listOf("가늘게" to 18f, "보통" to 28f, "굵게" to 44f)
            Ui.choose(this, "형광펜 굵기", ws.map { it.first }) { pageView.hlWidth = ws[it].second }
        } else {
            val ws = listOf("아주 가늘게" to 2f, "가늘게" to 3f, "보통" to 4.5f, "굵게" to 7f, "아주 굵게" to 11f)
            Ui.choose(this, "펜 굵기", ws.map { it.first }) { pageView.penWidth = ws[it].second }
        }
    }

    private fun saveTools() {
        getPreferences(MODE_PRIVATE).edit()
            .putInt("penColor", pageView.penColor).putFloat("penWidth", pageView.penWidth)
            .putInt("hlColor", pageView.hlColor).putFloat("hlWidth", pageView.hlWidth)
            .putBoolean("finger", pageView.fingerDraws).putInt("tool", pageView.tool).apply()
    }

    private fun restoreTools() {
        val pr = getPreferences(MODE_PRIVATE)
        pageView.penColor = pr.getInt("penColor", Color.BLACK)
        pageView.penWidth = pr.getFloat("penWidth", 3f)
        pageView.hlColor = pr.getInt("hlColor", 0xFFFFD400.toInt())
        pageView.hlWidth = pr.getFloat("hlWidth", 28f)
        pageView.fingerDraws = pr.getBoolean("finger", false)
        setTool(pr.getInt("tool", PageView.TOOL_PEN))
    }

    override fun onInkChanged() {
        updateUndo()
        main.removeCallbacks(saveInk)
        main.postDelayed(saveInk, 600)
    }

    override fun onSelectionChanged(count: Int) {
        selBar.visibility = if (count > 0) View.VISIBLE else View.GONE
        selLabel.text = "${count}개 선택 — 끌어서 옮기기"
    }

    private val saveInk = Runnable { saveInkNow() }

    private fun saveInkNow() {
        main.removeCallbacks(saveInk)
        val p = doc.pages.firstOrNull { it.id == shownPage } ?: return
        if (!p.done) return
        val f = p.ink(doc)
        if (pageView.strokes.isEmpty() && !f.exists()) return
        try { Ink.save(f, pageView.strokes) } catch (e: Exception) { Ui.toast(this, "필기 저장 실패: $e") }
    }

    private fun updateUndo() {
        undoBtn.alpha = if (pageView.canUndo()) 1f else 0.4f
        redoBtn.alpha = if (pageView.canRedo()) 1f else 0.4f
    }

    // ───────────── 쪽 추가 ─────────────

    private fun addPagesMenu(anchor: View) {
        val m = PopupMenu(this, anchor)
        m.menu.add(0, 1, 0, "사진 찍기")
        m.menu.add(0, 2, 1, "사진 가져오기")
        m.setOnMenuItemClickListener {
            if (it.itemId == 1) takePhoto() else importPhotos(); true
        }
        m.show()
    }

    private fun takePhoto() {
        val f = File(File(cacheDir, "camera").apply { mkdirs() }, "shot_${System.currentTimeMillis()}.jpg")
        pendingPhoto = f
        getPreferences(MODE_PRIVATE).edit().putString("pendingPhoto", f.absolutePath).apply()
        val i = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
            .putExtra(MediaStore.EXTRA_OUTPUT, Ui.uriFor(this, f))
            .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION)
        try {
            startActivityForResult(i, REQ_CAMERA)
        } catch (e: Exception) {
            Ui.toast(this, "카메라 앱을 열 수 없습니다")
            dropIfEmpty()
        }
    }

    private fun importPhotos() {
        val i = Intent(Intent.ACTION_GET_CONTENT).setType("image/*").addCategory(Intent.CATEGORY_OPENABLE)
            .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
        startActivityForResult(Intent.createChooser(i, "사진 고르기(여러 장 가능)"), REQ_IMPORT)
    }

    private fun addSample() {
        val f = File(cacheDir, "sample.jpg")
        assets.open("sample_photo.jpg").use { i -> f.outputStream().use { i.copyTo(it) } }
        addPhoto(f)
    }

    private fun addPhoto(f: File) {
        val p = Store.addPage(doc, f)
        Engine.enqueue(this, doc, p)
        refreshStrip()
        showPage(doc.pages.size - 1)
    }

    /** 쪽이 하나도 없는 새 묶음에서 취소하면 묶음을 지우고 나간다. */
    private fun dropIfEmpty() {
        if (doc.pages.isEmpty() && intent.getStringExtra(EXTRA_START) != null) {
            Store.delete(this, doc)
            finish()
        }
    }

    @Deprecated("framework API")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        when (requestCode) {
            REQ_CAMERA -> {
                val f = pendingPhoto
                pendingPhoto = null
                getPreferences(MODE_PRIVATE).edit().remove("pendingPhoto").apply()
                if (resultCode == RESULT_OK && f != null && f.exists() && f.length() > 0) {
                    addPhoto(f)
                    f.delete()
                    AlertDialog.Builder(this).setMessage("${doc.pages.size}쪽을 넣었습니다. 변환은 뒤에서 이어집니다.")
                        .setPositiveButton("다음 장 찍기") { _, _ -> takePhoto() }
                        .setNegativeButton("그만 찍기", null).show()
                } else dropIfEmpty()
            }
            REQ_IMPORT -> {
                if (resultCode != RESULT_OK || data == null) { dropIfEmpty(); return }
                val uris = ArrayList<Uri>()
                data.clipData?.let { c -> for (k in 0 until c.itemCount) uris.add(c.getItemAt(k).uri) }
                if (uris.isEmpty()) data.data?.let { uris.add(it) }
                for (u in uris) importUri(this, doc, u)
                refreshStrip()
                showPage(doc.pages.size - 1)
            }
            REQ_SAVE -> if (resultCode == RESULT_OK && data?.data != null) exportFile?.let { Ui.copyTo(this, it, data.data!!) }
        }
    }

    // ───────────── 내보내기 ─────────────

    private fun exportDoc() {
        saveInkNow()
        val pages = doc.pages.filter { it.done }
        if (pages.isEmpty()) { Ui.toast(this, "변환이 끝난 쪽이 없습니다"); return }
        val waiting = doc.pages.size - pages.size
        val out = File(File(Pipeline.outDir(this), "export").apply { mkdirs() }, Store.fileName(doc.title) + ".pdf")
        runExport(pages, out, "PDF ${pages.size}쪽" + if (waiting > 0) " (변환 중인 ${waiting}쪽은 빠짐)" else "")
    }

    private fun exportPage() {
        saveInkNow()
        val p = current() ?: return
        if (!p.done) { Ui.toast(this, "아직 변환 중입니다"); return }
        val out = File(File(Pipeline.outDir(this), "export").apply { mkdirs() },
            Store.fileName(doc.title) + "_${index + 1}쪽.pdf")
        runExport(listOf(p), out, "${index + 1}쪽 PDF")
    }

    private fun runExport(pages: List<Page>, out: File, what: String) {
        val dlg = AlertDialog.Builder(this).setMessage("$what 만드는 중…").setCancelable(false).show()
        Engine.background({ Pipeline.exportPdf(this, doc, pages, out) }) { r ->
            dlg.dismiss()
            if (!r.optBoolean("ok") || !out.exists()) {
                AlertDialog.Builder(this).setTitle("PDF를 만들지 못했습니다").setMessage(r.optString("error") + "\n\n" + r.optString("trace"))
                    .setPositiveButton("닫기", null).show()
                return@background
            }
            exportFile = out
            Ui.choose(this, "$what 완성 (${out.length() / 1024} KB)", listOf("다른 앱으로 열기", "보내기·공유", "기기에 저장")) { k ->
                when (k) {
                    0 -> Ui.open(this, out, "application/pdf")
                    1 -> Ui.share(this, out, "application/pdf")
                    2 -> Ui.createDocument(this, out.name, "application/pdf", REQ_SAVE)
                }
            }
        }
    }

    private fun saveFile(f: File, mime: String) {
        if (!f.exists()) { Ui.toast(this, "파일이 없습니다"); return }
        exportFile = f
        val name = Store.fileName(doc.title) + "_${index + 1}쪽." + f.extension
        Ui.choose(this, name, listOf("보내기·공유", "기기에 저장")) { k ->
            if (k == 0) {
                val copy = File(File(Pipeline.outDir(this), "export").apply { mkdirs() }, name)
                f.copyTo(copy, overwrite = true)
                Ui.share(this, copy, mime)
            } else Ui.createDocument(this, name, mime, REQ_SAVE)
        }
    }

    // ───────────── 그 밖의 메뉴 ─────────────

    private fun moreMenu(anchor: View) {
        val p = current()
        val m = PopupMenu(this, anchor)
        val items = mutableListOf<Pair<String, () -> Unit>>()
        if (p != null) {
            items += "이 쪽 PDF로 내보내기" to { exportPage() }
            items += "이 쪽 SVG 저장" to { saveFile(p.svg(doc), "image/svg+xml") }
            items += "이 쪽 스캔 사진 저장" to { saveFile(p.scan(doc), "image/jpeg") }
            items += "이 쪽 원본 사진 저장" to { saveFile(p.src(doc), "image/jpeg") }
            items += "이 쪽 다시 변환" to { reconvert(p) }
            items += "이 쪽 앞으로" to { movePage(-1) }
            items += "이 쪽 뒤로" to { movePage(1) }
            items += "이 쪽 필기 모두 지우기" to { clearInk() }
            items += "이 쪽 삭제" to { deletePage(p) }
            items += "변환 기록 보기" to { showLog() }
        }
        items += (if (pageView.fingerDraws) "✓ 손가락으로도 필기" else "손가락으로도 필기") to {
            pageView.fingerDraws = !pageView.fingerDraws
            Ui.toast(this, if (pageView.fingerDraws) "손가락으로도 필기합니다(이동은 두 손가락)" else "펜으로만 필기합니다")
        }
        items += "제목 바꾸기" to { rename() }
        for ((k, it) in items.withIndex()) m.menu.add(0, k, k, it.first)
        m.setOnMenuItemClickListener { items[it.itemId].second(); true }
        m.show()
    }

    private fun reconvert(p: Page) {
        AlertDialog.Builder(this).setMessage("이 쪽을 다시 변환할까요? 필기는 그대로 둡니다.")
            .setPositiveButton("다시 변환") { _, _ ->
                saveInkNow()
                Engine.enqueue(this, doc, p)
                showPage(index)
                refreshStrip()
            }.setNegativeButton("취소", null).show()
    }

    private fun movePage(d: Int) {
        val j = index + d
        if (j !in doc.pages.indices) return
        saveInkNow()
        val p = doc.pages.removeAt(index)
        doc.pages.add(j, p)
        Store.save(doc)
        index = j
        refreshStrip()
        showPage(j)
    }

    private fun clearInk() {
        if (pageView.strokes.isEmpty()) return
        AlertDialog.Builder(this).setMessage("이 쪽의 필기를 모두 지울까요? (되돌리기로 살릴 수 있습니다)")
            .setPositiveButton("지우기") { _, _ ->
                pageView.replaceAll(emptyList())
                updateUndo()
            }.setNegativeButton("취소", null).show()
    }

    private fun deletePage(p: Page) {
        AlertDialog.Builder(this).setMessage("${index + 1}쪽을 지울까요? 필기도 함께 지워집니다.")
            .setPositiveButton("삭제") { _, _ ->
                shownPage = null
                Store.deletePage(doc, p)
                refreshStrip()
                showPage(index)
            }.setNegativeButton("취소", null).show()
    }

    private fun rename() {
        val e = EditText(this).apply { setText(doc.title); setSelection(text.length) }
        AlertDialog.Builder(this).setTitle("제목").setView(e)
            .setPositiveButton("확인") { _, _ ->
                doc.title = e.text.toString().trim().ifEmpty { doc.title }
                Store.save(doc)
                titleView.text = doc.title
            }.setNegativeButton("취소", null).show()
    }

    private fun showLog() {
        val p = current() ?: return
        val f = File(p.dir(doc), "log.txt")
        val txt = if (f.exists()) f.readText().take(20000) else p.message
        val tv = Ui.text(this, txt, 12f, Color.BLACK).apply {
            setTextIsSelectable(true); setPadding(Ui.dpi(context, 16), Ui.dpi(context, 8), Ui.dpi(context, 16), 0)
        }
        AlertDialog.Builder(this).setTitle("변환 기록").setView(ScrollView(this).apply { addView(tv) })
            .setPositiveButton("닫기", null).show()
    }

    companion object {
        const val EXTRA_DOC = "doc"
        const val EXTRA_START = "start"
        const val START_CAMERA = "camera"
        const val START_IMPORT = "import"
        const val START_SAMPLE = "sample"
        const val REQ_CAMERA = 1
        const val REQ_IMPORT = 2
        const val REQ_SAVE = 3

        /** 다른 앱이 준 사진(content://)을 새 쪽으로 넣고 변환 대기열에 올린다. */
        fun importUri(ctx: Context, doc: Doc, u: Uri) {
            val tmp = File(File(ctx.cacheDir, "import").apply { mkdirs() }, "in_${System.currentTimeMillis()}.jpg")
            try {
                ctx.contentResolver.openInputStream(u)?.use { i -> tmp.outputStream().use { i.copyTo(it) } }
                if (tmp.length() > 0) {
                    val p = Store.addPage(doc, tmp)
                    Engine.enqueue(ctx, doc, p)
                }
            } catch (e: Exception) {
                Ui.toast(ctx, "사진을 읽지 못했습니다: $e")
            } finally {
                tmp.delete()
            }
        }

        fun displayName(ctx: Context, u: Uri): String? = try {
            ctx.contentResolver.query(u, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getString(0) else null
            }
        } catch (e: Exception) {
            null
        }
    }
}

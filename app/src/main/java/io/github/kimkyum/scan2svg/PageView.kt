package io.github.kimkyum.scan2svg

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Matrix
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.graphics.pdf.PdfRenderer
import android.media.ExifInterface
import android.os.Handler
import android.os.Looper
import android.os.ParcelFileDescriptor
import android.view.GestureDetector
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import java.io.File
import java.util.concurrent.Executors
import kotlin.math.abs
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min

/**
 * 한 쪽을 보여 주고 그 위에 필기하는 화면.
 *  - 두 손가락: 확대·축소·이동, 한 손가락: 이동, 손가락 두 번 톡: 맞춤 ↔ 2배
 *  - 펜(스타일러스): 필기. 펜이 닿아 있는 동안 손(손바닥) 터치는 무시한다.
 *  - S펜 옆 버튼을 누른 채 쓰거나 펜 뒤쪽(지우개 끝)으로 문지르면 지우개
 *  - 펜을 그은 끝에서 잠깐 멈추면 반듯한 도형으로 바뀐다
 */
@SuppressLint("ViewConstructor")
class PageView(ctx: Context) : View(ctx) {
    enum class Mode { VECTOR, SCAN, PHOTO }

    interface Callback {
        fun onInkChanged()
        fun onSelectionChanged(count: Int)
    }

    var callback: Callback? = null

    // 쪽
    private var pdfFile: File? = null
    private var scanFile: File? = null
    private var photoFile: File? = null
    var mode = Mode.VECTOR
        private set
    private var pageW = 1000f
    private var pageH = 1414f
    private var base: Bitmap? = null
    private var baseRect = RectF()
    private var loadToken = 0

    // 확대 영역의 선명한 그림(재구성 보기에서만)
    private var tile: Bitmap? = null
    private var tileMatrix: Matrix? = null
    private var renderer: PdfRenderer? = null
    private var rendererFd: ParcelFileDescriptor? = null
    private val renderPool = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())

    // 보기 변환: 쪽 좌표 → 화면
    private val m = Matrix()
    private val inv = Matrix()
    private var fitted = false

    // 필기
    var strokes: MutableList<Stroke> = mutableListOf()
        private set
    private val undoStack = ArrayDeque<List<Stroke>>()
    private val redoStack = ArrayDeque<List<Stroke>>()
    var tool = TOOL_PEN
        set(v) { field = v; clearSelection(); invalidate() }
    var penColor = Color.BLACK
    var penWidth = 3f          // 쪽 좌표(픽셀) 단위
    var hlColor = 0xFFFFD400.toInt()
    var hlWidth = 28f
    var fingerDraws = false
    var inkEnabled = true

    private var cur: FloatArray? = null
    private var curN = 0
    private var curTool = TOOL_PEN
    private var snapped: Stroke? = null
    private var erasing = false
    private var eraseBefore: List<Stroke>? = null
    private var lasso: FloatArray? = null
    private var lassoN = 0
    private var selection: Set<Int> = emptySet()
    private var moving = false
    private var moveFrom = FloatArray(2)
    private var moveD = FloatArray(2)
    private var stylusDown = false
    private var lastStylus = 0L

    private val paperPaint = Paint().apply { color = Color.WHITE }
    private val shadowPaint = Paint().apply { color = 0x33000000 }
    private val bmpPaint = Paint(Paint.FILTER_BITMAP_FLAG or Paint.ANTI_ALIAS_FLAG)
    private val lassoPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; color = 0xFF3B82F6.toInt(); strokeWidth = 2f
        pathEffect = android.graphics.DashPathEffect(floatArrayOf(10f, 8f), 0f)
    }
    private val selPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; color = 0xFF3B82F6.toInt(); strokeWidth = 2f
    }
    private val eraserPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; color = 0x88666666.toInt(); strokeWidth = 2f
    }
    private var eraserAt: FloatArray? = null

    // ───────────── 쪽 불러오기 ─────────────

    /** 쪽을 바꾼다. 그림은 백그라운드에서 읽는다. */
    fun setPage(pdf: File?, scan: File?, photo: File?, w: Int, h: Int, mode: Mode, ink: MutableList<Stroke>) {
        pdfFile = pdf; scanFile = scan; photoFile = photo
        if (w > 0 && h > 0) { pageW = w.toFloat(); pageH = h.toFloat() }
        strokes = ink
        undoStack.clear(); redoStack.clear()
        clearSelection()
        fitted = false
        setMode(mode, force = true)
    }

    fun setMode(md: Mode, force: Boolean = false) {
        if (md == mode && !force) return
        val pageChanged = force
        mode = md
        val token = ++loadToken
        tile = null; tileMatrix = null
        val pdf = pdfFile; val scan = scanFile; val photo = photoFile
        val vw = max(width, 800)
        renderPool.execute {
            if (pageChanged) closeRenderer()
            var bmp: Bitmap? = null
            var w = pageW
            var h = pageH
            try {
                when (md) {
                    Mode.VECTOR -> if (pdf != null && pdf.exists()) {
                        val r = openRenderer(pdf)
                        r?.openPage(0)?.use { pg ->
                            val bw = min(2000, max(1200, (vw * 1.5f).toInt()))
                            val bh = (bw.toFloat() * pg.height / pg.width).toInt()
                            bmp = Bitmap.createBitmap(bw, bh, Bitmap.Config.ARGB_8888).also {
                                it.eraseColor(Color.WHITE)
                                pg.render(it, null, null, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                            }
                        }
                    }
                    Mode.SCAN -> if (scan != null && scan.exists()) bmp = decode(scan, 3000)
                    Mode.PHOTO -> if (photo != null && photo.exists()) {
                        bmp = decode(photo, 3000)?.let { rotateByExif(photo, it) }
                        bmp?.let { w = it.width.toFloat(); h = it.height.toFloat() }
                    }
                }
            } catch (e: Throwable) {
                android.util.Log.w(Pipeline.TAG, "page load failed", e)
            }
            main.post {
                if (token != loadToken) { bmp?.recycle(); return@post }
                val old = base
                base = bmp
                if (old != null && old !== bmp) old.recycle()
                if (md == Mode.PHOTO) {
                    viewW = w; viewH = h
                } else {
                    viewW = pageW; viewH = pageH
                }
                baseRect.set(0f, 0f, viewW, viewH)
                if (!fitted || md == Mode.PHOTO || pageChanged) fit()
                scheduleTile()
                invalidate()
            }
        }
        invalidate()
    }

    // 현재 보기의 좌표 크기(사진 보기에서는 사진 크기, 그 밖에는 쪽 크기)
    private var viewW = pageW
    private var viewH = pageH

    private fun decode(f: File, maxSide: Int): Bitmap? {
        val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(f.absolutePath, o)
        var s = 1
        while (max(o.outWidth, o.outHeight) / s > maxSide) s *= 2
        return BitmapFactory.decodeFile(f.absolutePath, BitmapFactory.Options().apply { inSampleSize = s })
    }

    private fun rotateByExif(f: File, b: Bitmap): Bitmap {
        val deg = try {
            when (ExifInterface(f.absolutePath).getAttributeInt(ExifInterface.TAG_ORIENTATION, 1)) {
                ExifInterface.ORIENTATION_ROTATE_90 -> 90f
                ExifInterface.ORIENTATION_ROTATE_180 -> 180f
                ExifInterface.ORIENTATION_ROTATE_270 -> 270f
                else -> 0f
            }
        } catch (e: Exception) { 0f }
        if (deg == 0f) return b
        val r = Bitmap.createBitmap(b, 0, 0, b.width, b.height, Matrix().apply { postRotate(deg) }, true)
        if (r !== b) b.recycle()
        return r
    }

    private fun openRenderer(f: File): PdfRenderer? {
        renderer?.let { return it }
        return try {
            val fd = ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY)
            rendererFd = fd
            PdfRenderer(fd).also { renderer = it }
        } catch (e: Exception) {
            null
        }
    }

    private fun closeRenderer() {
        try { renderer?.close() } catch (e: Exception) {}
        try { rendererFd?.close() } catch (e: Exception) {}
        renderer = null; rendererFd = null
    }

    fun release() {
        loadToken++
        renderPool.execute { closeRenderer() }
        renderPool.shutdown()
    }

    // ───────────── 확대·이동 ─────────────

    fun fit() {
        if (width == 0 || height == 0) return
        val s = min(width / viewW, height / viewH) * 0.98f
        m.reset()
        m.postScale(s, s)
        m.postTranslate((width - viewW * s) / 2, max(0f, (height - viewH * s) / 2))
        fitted = true
        afterMove()
    }

    override fun onSizeChanged(w: Int, h: Int, ow: Int, oh: Int) {
        super.onSizeChanged(w, h, ow, oh)
        fit()
    }

    private fun scale(): Float {
        val v = FloatArray(9)
        m.getValues(v)
        return v[Matrix.MSCALE_X]
    }

    private fun afterMove() {
        m.invert(inv)
        tile = null
        scheduleTile()
        invalidate()
    }

    private val tileRunnable = Runnable { renderTile() }
    private fun scheduleTile() {
        main.removeCallbacks(tileRunnable)
        if (mode == Mode.VECTOR) main.postDelayed(tileRunnable, 220)
    }

    /** 확대했을 때 보이는 영역만 화면 해상도로 다시 그린다. */
    private fun renderTile() {
        val b = base ?: return
        val s = scale()
        if (s * viewW <= b.width * 1.15f || width == 0 || height == 0) return
        val mm = Matrix(m)
        val token = loadToken
        val w = width; val h = height
        val pw = pageW
        renderPool.execute {
            val r = renderer ?: return@execute
            try {
                r.openPage(0).use { pg ->
                    val k = pw / pg.width  // PDF 포인트 → 쪽 좌표
                    val t = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)
                    t.eraseColor(Color.TRANSPARENT)
                    val pm = Matrix()
                    pm.postScale(k, k)
                    pm.postConcat(mm)
                    // 쪽 밖은 흰색으로 칠하지 않도록, 흰 바탕은 onDraw의 종이가 맡는다
                    pg.render(t, null, pm, PdfRenderer.Page.RENDER_MODE_FOR_DISPLAY)
                    main.post {
                        if (token == loadToken && mm == m) {
                            tile = t; tileMatrix = mm; invalidate()
                        } else t.recycle()
                    }
                }
            } catch (e: Exception) {
                android.util.Log.w(Pipeline.TAG, "tile failed", e)
            }
        }
    }

    private val scaleDet = ScaleGestureDetector(ctx, object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
        override fun onScale(d: ScaleGestureDetector): Boolean {
            val s = scale()
            val f = (s * d.scaleFactor).coerceIn(fitScale() * 0.5f, fitScale() * 12f) / s
            m.postScale(f, f, d.focusX, d.focusY)
            afterMove()
            return true
        }
    })

    private fun fitScale() = if (width == 0) 1f else min(width / viewW, height / viewH)

    private val gestureDet = GestureDetector(ctx, object : GestureDetector.SimpleOnGestureListener() {
        override fun onDoubleTap(e: MotionEvent): Boolean {
            if (scale() > fitScale() * 1.3f) fit() else {
                m.postScale(2.2f, 2.2f, e.x, e.y); afterMove()
            }
            return true
        }
    })

    private var panX = 0f
    private var panY = 0f
    private var panPointers = 0

    // ───────────── 터치 ─────────────

    override fun onGenericMotionEvent(e: MotionEvent): Boolean {
        if (e.getToolType(0) == MotionEvent.TOOL_TYPE_STYLUS || e.getToolType(0) == MotionEvent.TOOL_TYPE_ERASER) {
            lastStylus = System.currentTimeMillis()  // 펜이 화면 위에 떠 있으면 손바닥 터치를 무시하기 위해
            if (e.actionMasked == MotionEvent.ACTION_HOVER_MOVE && (tool == TOOL_ERASER || isEraserEvent(e))) {
                eraserAt = toPage(e.x, e.y); invalidate()
            } else if (e.actionMasked == MotionEvent.ACTION_HOVER_EXIT) {
                eraserAt = null; invalidate()
            }
        }
        return super.onGenericMotionEvent(e)
    }

    private fun isEraserEvent(e: MotionEvent) =
        e.getToolType(0) == MotionEvent.TOOL_TYPE_ERASER || (e.buttonState and MotionEvent.BUTTON_STYLUS_PRIMARY) != 0

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(e: MotionEvent): Boolean {
        val stylus = e.getToolType(0) == MotionEvent.TOOL_TYPE_STYLUS || e.getToolType(0) == MotionEvent.TOOL_TYPE_ERASER
        val draws = inkEnabled && mode != Mode.PHOTO && (stylus || (fingerDraws && e.pointerCount == 1 && panPointers == 0))
        if (stylus) {
            lastStylus = System.currentTimeMillis()
            stylusDown = e.actionMasked != MotionEvent.ACTION_UP && e.actionMasked != MotionEvent.ACTION_CANCEL
        } else if (stylusDown || System.currentTimeMillis() - lastStylus < 400) {
            return true  // 펜을 쓰는 중의 손바닥
        }
        if (draws && (cur != null || erasing || lasso != null || moving || e.actionMasked == MotionEvent.ACTION_DOWN)) {
            if (e.pointerCount > 1 && !stylus) { cancelDrawing(); return gesture(e) }
            draw(e, stylus)
            return true
        }
        return gesture(e)
    }

    private fun gesture(e: MotionEvent): Boolean {
        scaleDet.onTouchEvent(e)
        gestureDet.onTouchEvent(e)
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN, MotionEvent.ACTION_POINTER_UP -> {
                val skip = if (e.actionMasked == MotionEvent.ACTION_POINTER_UP) e.actionIndex else -1
                var sx = 0f; var sy = 0f; var c = 0
                for (i in 0 until e.pointerCount) if (i != skip) { sx += e.getX(i); sy += e.getY(i); c++ }
                panX = sx / max(1, c); panY = sy / max(1, c); panPointers = c
            }
            MotionEvent.ACTION_MOVE -> {
                var sx = 0f; var sy = 0f
                for (i in 0 until e.pointerCount) { sx += e.getX(i); sy += e.getY(i) }
                sx /= e.pointerCount; sy /= e.pointerCount
                if (!scaleDet.isInProgress || e.pointerCount > 1) {
                    m.postTranslate(sx - panX, sy - panY)
                    clampPan()
                    afterMove()
                }
                panX = sx; panY = sy
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> panPointers = 0
        }
        return true
    }

    private fun clampPan() {
        val r = RectF(0f, 0f, viewW, viewH)
        m.mapRect(r)
        var dx = 0f; var dy = 0f
        val mx = width * 0.5f; val my = height * 0.5f
        if (r.right < mx) dx = mx - r.right
        if (r.left > width - mx) dx = width - mx - r.left
        if (r.bottom < my) dy = my - r.bottom
        if (r.top > height - my) dy = height - my - r.top
        m.postTranslate(dx, dy)
    }

    private fun toPage(x: Float, y: Float): FloatArray {
        val p = floatArrayOf(x, y)
        inv.mapPoints(p)
        return p
    }

    private val snapRunnable = Runnable { trySnap() }
    private var holdX = 0f
    private var holdY = 0f

    private fun draw(e: MotionEvent, stylus: Boolean) {
        val effTool = when {
            stylus && isEraserEvent(e) -> TOOL_ERASER
            else -> tool
        }
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                when (effTool) {
                    TOOL_ERASER -> {
                        erasing = true
                        eraseBefore = strokes.toList()
                        eraseAt(e.x, e.y)
                    }
                    TOOL_LASSO -> {
                        val p = toPage(e.x, e.y)
                        val b = selectionBounds()
                        if (b != null && b.contains(p[0], p[1])) {
                            moving = true; moveFrom = p; moveD = floatArrayOf(0f, 0f)
                        } else {
                            clearSelection()
                            lasso = FloatArray(512); lassoN = 0
                            addLasso(p)
                        }
                    }
                    else -> {
                        clearSelection()
                        curTool = effTool
                        cur = FloatArray(768); curN = 0; snapped = null
                        addPoint(e.x, e.y, pressure(e, stylus))
                        holdX = e.x; holdY = e.y
                        main.postDelayed(snapRunnable, SNAP_MS)
                    }
                }
            }
            MotionEvent.ACTION_MOVE -> {
                if (erasing) {
                    for (h in 0 until e.historySize) eraseAt(e.getHistoricalX(h), e.getHistoricalY(h))
                    eraseAt(e.x, e.y)
                } else if (moving) {
                    val p = toPage(e.x, e.y)
                    moveD = floatArrayOf(p[0] - moveFrom[0], p[1] - moveFrom[1])
                    invalidate()
                } else if (lasso != null) {
                    for (h in 0 until e.historySize) addLasso(toPage(e.getHistoricalX(h), e.getHistoricalY(h)))
                    addLasso(toPage(e.x, e.y))
                    invalidate()
                } else if (cur != null && snapped == null) {
                    for (h in 0 until e.historySize) {
                        addPoint(e.getHistoricalX(h), e.getHistoricalY(h),
                            if (stylus) e.getHistoricalPressure(h) else FINGER_PRESSURE)
                    }
                    addPoint(e.x, e.y, pressure(e, stylus))
                    if (hypot(e.x - holdX, e.y - holdY) > HOLD_SLOP) {
                        holdX = e.x; holdY = e.y
                        main.removeCallbacks(snapRunnable)
                        main.postDelayed(snapRunnable, SNAP_MS)
                    }
                    invalidate()
                }
            }
            MotionEvent.ACTION_UP -> {
                main.removeCallbacks(snapRunnable)
                when {
                    erasing -> {
                        erasing = false
                        val before = eraseBefore
                        eraseBefore = null
                        if (before != null && before.size != strokes.size) pushUndo(before)
                    }
                    moving -> {
                        moving = false
                        if (abs(moveD[0]) + abs(moveD[1]) > 0.5f) {
                            val before = strokes.toList()
                            for (i in selection) strokes[i] = strokes[i].moved(moveD[0], moveD[1])
                            pushUndo(before)
                        }
                        moveD = floatArrayOf(0f, 0f)
                    }
                    lasso != null -> {
                        val poly = lasso!!.copyOf(lassoN * 2)
                        lasso = null
                        selection = Ink.lassoSelect(strokes, poly)
                        callback?.onSelectionChanged(selection.size)
                    }
                    cur != null -> commitStroke()
                }
                invalidate()
            }
            MotionEvent.ACTION_CANCEL -> cancelDrawing()
        }
    }

    private fun pressure(e: MotionEvent, stylus: Boolean) = if (stylus) e.pressure else FINGER_PRESSURE

    private fun addPoint(x: Float, y: Float, p: Float) {
        var a = cur ?: return
        val q = toPage(x, y)
        if (curN > 0) {
            val dx = q[0] - a[3 * curN - 3]
            val dy = q[1] - a[3 * curN - 2]
            if (dx * dx + dy * dy < 0.04f) return
        }
        if (3 * curN + 3 > a.size) { a = a.copyOf(a.size * 2); cur = a }
        a[3 * curN] = q[0]; a[3 * curN + 1] = q[1]; a[3 * curN + 2] = p.coerceIn(0f, 1f)
        curN++
    }

    private fun addLasso(p: FloatArray) {
        var a = lasso ?: return
        if (2 * lassoN + 2 > a.size) { a = a.copyOf(a.size * 2); lasso = a }
        a[2 * lassoN] = p[0]; a[2 * lassoN + 1] = p[1]; lassoN++
    }

    private fun currentStroke(): Stroke? {
        val a = cur ?: return null
        if (curN == 0) return null
        return if (curTool == TOOL_HL) Stroke(Stroke.TOOL_HL, hlColor, hlWidth, a.copyOf(curN * 3), 0.4f)
        else Stroke(Stroke.TOOL_PEN, penColor, penWidth, a.copyOf(curN * 3))
    }

    private fun trySnap() {
        val s = currentStroke() ?: return
        val r = Ink.recognize(s) ?: return
        snapped = r
        performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS)
        invalidate()
    }

    private fun commitStroke() {
        val s = snapped ?: currentStroke()
        cur = null; curN = 0; snapped = null
        if (s == null) return
        val before = strokes.toList()
        strokes.add(s)
        pushUndo(before)
    }

    private fun cancelDrawing() {
        main.removeCallbacks(snapRunnable)
        cur = null; curN = 0; snapped = null
        if (erasing) {
            eraseBefore?.let { strokes = it.toMutableList() }
            erasing = false; eraseBefore = null
        }
        lasso = null; moving = false
        invalidate()
    }

    private fun eraseAt(x: Float, y: Float) {
        val p = toPage(x, y)
        eraserAt = p
        val r = ERASER_PX / scale()
        val it = strokes.iterator()
        var changed = false
        while (it.hasNext()) if (Ink.hits(it.next(), p[0], p[1], r)) { it.remove(); changed = true }
        if (changed) callback?.onInkChanged()
        invalidate()
    }

    // ───────────── 되돌리기·선택 ─────────────

    private fun pushUndo(before: List<Stroke>) {
        undoStack.addLast(before)
        if (undoStack.size > 200) undoStack.removeFirst()
        redoStack.clear()
        callback?.onInkChanged()
    }

    fun canUndo() = undoStack.isNotEmpty()
    fun canRedo() = redoStack.isNotEmpty()

    fun undo() {
        val prev = undoStack.removeLastOrNull() ?: return
        redoStack.addLast(strokes.toList())
        strokes = prev.toMutableList()
        clearSelection()
        callback?.onInkChanged()
        invalidate()
    }

    fun redo() {
        val next = redoStack.removeLastOrNull() ?: return
        undoStack.addLast(strokes.toList())
        strokes = next.toMutableList()
        clearSelection()
        callback?.onInkChanged()
        invalidate()
    }

    /** 필기 전체를 바꾼다(되돌리기 가능). */
    fun replaceAll(list: List<Stroke>) {
        val before = strokes.toList()
        strokes = list.toMutableList()
        selection = emptySet()
        callback?.onSelectionChanged(0)
        pushUndo(before)
        invalidate()
    }

    fun clearSelection() {
        if (selection.isNotEmpty()) {
            selection = emptySet()
            callback?.onSelectionChanged(0)
        }
        invalidate()
    }

    fun deleteSelection() {
        if (selection.isEmpty()) return
        val before = strokes.toList()
        strokes = strokes.filterIndexed { i, _ -> i !in selection }.toMutableList()
        selection = emptySet()
        pushUndo(before)
        callback?.onSelectionChanged(0)
        invalidate()
    }

    fun duplicateSelection() {
        if (selection.isEmpty()) return
        val before = strokes.toList()
        val start = strokes.size
        for (i in selection.sorted()) strokes.add(strokes[i].moved(24f, 24f))
        selection = (start until strokes.size).toSet()
        pushUndo(before)
        callback?.onSelectionChanged(selection.size)
        invalidate()
    }

    fun scaleSelection(f: Float) {
        val b = selectionBounds() ?: return
        val before = strokes.toList()
        for (i in selection) strokes[i] = strokes[i].scaled(b.centerX(), b.centerY(), f)
        pushUndo(before)
        invalidate()
    }

    fun recolorSelection(color: Int) {
        if (selection.isEmpty()) return
        val before = strokes.toList()
        for (i in selection) {
            val s = strokes[i]
            strokes[i] = Stroke(s.tool, color, s.width, s.pts, s.alpha)
        }
        pushUndo(before)
        invalidate()
    }

    private fun selectionBounds(): RectF? {
        if (selection.isEmpty()) return null
        val r = RectF()
        var first = true
        for (i in selection) {
            if (i >= strokes.size) continue
            if (first) { r.set(strokes[i].bounds); first = false } else r.union(strokes[i].bounds)
        }
        if (first) return null
        r.inset(-8f, -8f)
        return r
    }

    // ───────────── 그리기 ─────────────

    override fun onDraw(c: Canvas) {
        c.drawColor(0xFF2B2B30.toInt())
        val page = RectF(0f, 0f, viewW, viewH)
        val vr = RectF(page)
        m.mapRect(vr)
        c.drawRect(vr.left + 4, vr.top + 6, vr.right + 4, vr.bottom + 6, shadowPaint)
        c.drawRect(vr, paperPaint)
        val b = base
        if (b != null && !b.isRecycled) c.drawBitmap(b, null, vr, bmpPaint)
        val t = tile
        if (t != null && tileMatrix == m && !t.isRecycled) {
            c.save(); c.clipRect(vr); c.drawBitmap(t, 0f, 0f, null); c.restore()
        }
        if (mode == Mode.PHOTO) return

        c.save()
        c.concat(m)
        c.clipRect(page)
        val sel = selection
        for ((i, s) in strokes.withIndex()) {
            if (moving && i in sel) {
                c.save(); c.translate(moveD[0], moveD[1]); Ink.draw(c, s); c.restore()
            } else Ink.draw(c, s)
        }
        (snapped ?: currentStroke())?.let { Ink.draw(c, it) }
        lasso?.let {
            if (lassoN > 1) {
                val p = Path()
                p.moveTo(it[0], it[1])
                for (k in 1 until lassoN) p.lineTo(it[2 * k], it[2 * k + 1])
                lassoPaint.strokeWidth = 2f / scale()
                c.drawPath(p, lassoPaint)
            }
        }
        selectionBounds()?.let {
            val r = RectF(it)
            if (moving) r.offset(moveD[0], moveD[1])
            selPaint.strokeWidth = 2f / scale()
            c.drawRect(r, selPaint)
        }
        eraserAt?.let {
            if (tool == TOOL_ERASER || erasing) {
                eraserPaint.strokeWidth = 1.5f / scale()
                c.drawCircle(it[0], it[1], ERASER_PX / scale(), eraserPaint)
            }
        }
        c.restore()
    }

    companion object {
        const val TOOL_PEN = 0
        const val TOOL_HL = 1
        const val TOOL_ERASER = 2
        const val TOOL_LASSO = 3
        const val FINGER_PRESSURE = 0.6f
        const val SNAP_MS = 650L
        const val HOLD_SLOP = 6f
        const val ERASER_PX = 14f
    }
}

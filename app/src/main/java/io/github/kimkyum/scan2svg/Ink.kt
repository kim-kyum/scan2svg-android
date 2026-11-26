package io.github.kimkyum.scan2svg

import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PorterDuff
import android.graphics.PorterDuffXfermode
import android.graphics.RectF
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.math.abs
import kotlin.math.atan2
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * 필기 한 획. 좌표는 쪽의 좌표(재구성 SVG의 viewBox, 픽셀 단위)이고 pts는 [x, y, 필압, x, y, 필압, …].
 * 한 번 만든 획은 바꾸지 않는다(옮기면 새 획을 만든다) — 되돌리기를 목록 복사만으로 하기 위해서.
 */
class Stroke(val tool: String, val color: Int, val width: Float, val pts: FloatArray, val alpha: Float = 1f) {
    val n get() = pts.size / 3
    val bounds: RectF by lazy {
        val r = RectF(Float.MAX_VALUE, Float.MAX_VALUE, -Float.MAX_VALUE, -Float.MAX_VALUE)
        for (i in 0 until n) r.union(pts[3 * i], pts[3 * i + 1])
        r.inset(-width, -width)
        r
    }

    /** 화면에 그릴 모양(쪽 좌표). 펜은 필압에 따라 굵기가 변하는 윤곽, 형광펜은 같은 굵기의 선. */
    val path: Path by lazy { if (tool == TOOL_HL) Ink.polyline(pts) else Ink.outline(pts, width) }

    fun moved(dx: Float, dy: Float): Stroke {
        val q = pts.copyOf()
        for (i in 0 until n) {
            q[3 * i] += dx
            q[3 * i + 1] += dy
        }
        return Stroke(tool, color, width, q, alpha)
    }

    fun scaled(cx: Float, cy: Float, s: Float): Stroke {
        val q = pts.copyOf()
        for (i in 0 until n) {
            q[3 * i] = cx + (q[3 * i] - cx) * s
            q[3 * i + 1] = cy + (q[3 * i + 1] - cy) * s
        }
        return Stroke(tool, color, width * s, q, alpha)
    }

    fun toJson(): JSONObject = JSONObject()
        .put("t", tool).put("c", String.format("#%06x", color and 0xffffff)).put("w", width.toDouble())
        .put("a", alpha.toDouble())
        .put("pts", JSONArray().apply { pts.forEach { put(Math.round(it * 100) / 100.0) } })

    companion object {
        const val TOOL_PEN = "pen"
        const val TOOL_HL = "hl"

        fun fromJson(o: JSONObject): Stroke {
            val a = o.getJSONArray("pts")
            val pts = FloatArray(a.length()) { a.getDouble(it).toFloat() }
            val c = android.graphics.Color.parseColor(o.optString("c", "#000000"))
            return Stroke(o.optString("t", TOOL_PEN), c, o.optDouble("w", 3.0).toFloat(), pts, o.optDouble("a", 1.0).toFloat())
        }
    }
}

object Ink {
    /** 펜 굵기: 필압 p(0~1)에 따라 기본 굵기의 30~100%. 파이썬 svg2pdf.pen_width와 같은 식. */
    fun penWidth(w: Float, p: Float) = w * (0.3f + 0.7f * p.coerceIn(0f, 1f))

    fun load(f: File): MutableList<Stroke> {
        if (!f.exists()) return mutableListOf()
        return try {
            val a = JSONObject(f.readText()).getJSONArray("strokes")
            MutableList(a.length()) { Stroke.fromJson(a.getJSONObject(it)) }
        } catch (e: Exception) {
            mutableListOf()
        }
    }

    fun save(f: File, strokes: List<Stroke>) {
        val o = JSONObject().put("version", 1).put("strokes", JSONArray().apply { strokes.forEach { put(it.toJson()) } })
        val tmp = File(f.parentFile, f.name + ".tmp")
        tmp.writeText(o.toString())
        tmp.renameTo(f)
    }

    fun polyline(pts: FloatArray): Path {
        val p = Path()
        val n = pts.size / 3
        if (n == 0) return p
        p.moveTo(pts[0], pts[1])
        if (n == 1) p.lineTo(pts[0] + 0.01f, pts[1])
        for (i in 1 until n) p.lineTo(pts[3 * i], pts[3 * i + 1])
        return p
    }

    /** 굵기가 변하는 선의 윤곽(양옆 가장자리 + 둥근 끝). */
    fun outline(pts: FloatArray, w: Float): Path {
        val path = Path()
        val n = pts.size / 3
        if (n == 0) return path
        if (n == 1) {
            path.addCircle(pts[0], pts[1], penWidth(w, pts[2]) / 2, Path.Direction.CW)
            return path
        }
        val lx = FloatArray(n)
        val ly = FloatArray(n)
        val rx = FloatArray(n)
        val ry = FloatArray(n)
        for (i in 0 until n) {
            val a = max(0, i - 1)
            val b = min(n - 1, i + 1)
            var dx = pts[3 * b] - pts[3 * a]
            var dy = pts[3 * b + 1] - pts[3 * a + 1]
            val len = hypot(dx, dy)
            if (len < 1e-4f) {
                dx = 1f; dy = 0f
            } else {
                dx /= len; dy /= len
            }
            val h = penWidth(w, pts[3 * i + 2]) / 2
            lx[i] = pts[3 * i] - dy * h; ly[i] = pts[3 * i + 1] + dx * h
            rx[i] = pts[3 * i] + dy * h; ry[i] = pts[3 * i + 1] - dx * h
        }
        path.moveTo(lx[0], ly[0])
        for (i in 1 until n) path.lineTo(lx[i], ly[i])
        for (i in n - 1 downTo 0) path.lineTo(rx[i], ry[i])
        path.close()
        // 둥근 끝과 꺾이는 곳의 틈을 메운다
        path.addCircle(pts[0], pts[1], penWidth(w, pts[2]) / 2, Path.Direction.CW)
        path.addCircle(pts[3 * n - 3], pts[3 * n - 2], penWidth(w, pts[3 * n - 1]) / 2, Path.Direction.CW)
        return path
    }

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeCap = Paint.Cap.ROUND; strokeJoin = Paint.Join.ROUND
    }
    private val multiply = PorterDuffXfermode(PorterDuff.Mode.MULTIPLY)

    /** 캔버스는 이미 쪽 좌표로 맞춰져 있어야 한다. */
    fun draw(c: Canvas, s: Stroke, highlight: Boolean = false) {
        if (s.tool == Stroke.TOOL_HL) {
            line.color = s.color
            line.alpha = (s.alpha * 255).toInt().coerceIn(0, 255)
            line.strokeWidth = s.width
            line.xfermode = multiply
            c.drawPath(s.path, line)
            line.xfermode = null
            line.alpha = 255
        } else {
            fill.color = s.color
            c.drawPath(s.path, fill)
        }
        if (highlight) {
            line.color = 0x553b82f6
            line.strokeWidth = 2f
            c.drawRect(s.bounds, line)
        }
    }

    // ───────────── 지우개·올가미 ─────────────

    /** 점(x, y)에서 반지름 r 안에 획이 닿는가 */
    fun hits(s: Stroke, x: Float, y: Float, r: Float): Boolean {
        if (!s.bounds.let { x >= it.left - r && x <= it.right + r && y >= it.top - r && y <= it.bottom + r }) return false
        val n = s.n
        val half = if (s.tool == Stroke.TOOL_HL) s.width / 2 else s.width / 2
        if (n == 1) return hypot(s.pts[0] - x, s.pts[1] - y) <= r + half
        for (i in 1 until n) {
            if (segDist(x, y, s.pts[3 * i - 3], s.pts[3 * i - 2], s.pts[3 * i], s.pts[3 * i + 1]) <= r + half) return true
        }
        return false
    }

    fun segDist(px: Float, py: Float, ax: Float, ay: Float, bx: Float, by: Float): Float {
        val dx = bx - ax
        val dy = by - ay
        val l2 = dx * dx + dy * dy
        val t = if (l2 < 1e-6f) 0f else (((px - ax) * dx + (py - ay) * dy) / l2).coerceIn(0f, 1f)
        return hypot(px - (ax + t * dx), py - (ay + t * dy))
    }

    fun inside(poly: FloatArray, x: Float, y: Float): Boolean {
        val n = poly.size / 2
        var c = false
        var j = n - 1
        for (i in 0 until n) {
            val xi = poly[2 * i]
            val yi = poly[2 * i + 1]
            val xj = poly[2 * j]
            val yj = poly[2 * j + 1]
            if ((yi > y) != (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi + 1e-9f) + xi) c = !c
            j = i
        }
        return c
    }

    /** 올가미 안에 점의 절반 이상이 들어간 획 */
    fun lassoSelect(strokes: List<Stroke>, poly: FloatArray): Set<Int> {
        if (poly.size < 6) return emptySet()
        val out = mutableSetOf<Int>()
        for ((k, s) in strokes.withIndex()) {
            var inn = 0
            for (i in 0 until s.n) if (inside(poly, s.pts[3 * i], s.pts[3 * i + 1])) inn++
            if (inn * 2 >= s.n && inn > 0) out.add(k)
        }
        return out
    }

    // ───────────── 도형 보정 ─────────────

    /**
     * 손으로 그린 획을 반듯한 도형으로 바꾼다(직선, 원·타원, 삼각형, 사각형, 다각형, 꺾은선).
     * 알아볼 수 없으면 null. 결과 획은 필압을 평균값으로 고르게 맞춘다.
     */
    fun recognize(s: Stroke): Stroke? {
        val n = s.n
        if (n < 4) return null
        val xs = FloatArray(n) { s.pts[3 * it] }
        val ys = FloatArray(n) { s.pts[3 * it + 1] }
        var p = 0f
        for (i in 0 until n) p += s.pts[3 * i + 2]
        p /= n
        var len = 0f
        for (i in 1 until n) len += hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1])
        if (len < 12f) return null
        val gap = hypot(xs[n - 1] - xs[0], ys[n - 1] - ys[0])

        fun make(vararg q: Float): Stroke {
            val out = FloatArray(q.size / 2 * 3)
            for (i in 0 until q.size / 2) {
                out[3 * i] = q[2 * i]; out[3 * i + 1] = q[2 * i + 1]; out[3 * i + 2] = p
            }
            return Stroke(s.tool, s.color, s.width, out, s.alpha)
        }

        // 1) 직선
        var dev = 0f
        for (i in 0 until n) dev = max(dev, segDist(xs[i], ys[i], xs[0], ys[0], xs[n - 1], ys[n - 1]))
        if (gap > 0.85f * len && dev < max(0.05f * len, 4f)) {
            var x0 = xs[0]; var y0 = ys[0]; var x1 = xs[n - 1]; var y1 = ys[n - 1]
            val ang = Math.toDegrees(atan2((y1 - y0).toDouble(), (x1 - x0).toDouble()))
            val a = ((ang % 180) + 180) % 180
            if (a < 5 || a > 175) { val m = (y0 + y1) / 2; y0 = m; y1 = m }       // 수평으로
            else if (abs(a - 90) < 5) { val m = (x0 + x1) / 2; x0 = m; x1 = m }    // 수직으로
            return make(x0, y0, x1, y1)
        }

        val closed = gap < max(0.18f * len, 10f)
        if (closed) {
            // 2) 원·타원: 중심에서의 거리가 고른가
            var cx = 0f; var cy = 0f
            for (i in 0 until n) { cx += xs[i]; cy += ys[i] }
            cx /= n; cy /= n
            // 주축 방향(공분산)
            var sxx = 0f; var syy = 0f; var sxy = 0f
            for (i in 0 until n) {
                val dx = xs[i] - cx; val dy = ys[i] - cy
                sxx += dx * dx; syy += dy * dy; sxy += dx * dy
            }
            var th = 0.5f * atan2(2 * sxy, sxx - syy)
            if (abs(Math.toDegrees(th.toDouble())) < 8) th = 0f
            if (abs(abs(Math.toDegrees(th.toDouble())) - 90) < 8) th = 0f
            val ct = cos(th); val st = sin(th)
            var ra = 0f; var rb = 0f
            for (i in 0 until n) {
                val u = (xs[i] - cx) * ct + (ys[i] - cy) * st
                val v = -(xs[i] - cx) * st + (ys[i] - cy) * ct
                ra = max(ra, abs(u)); rb = max(rb, abs(v))
            }
            if (ra > 3 && rb > 3) {
                var err = 0f
                for (i in 0 until n) {
                    val u = (xs[i] - cx) * ct + (ys[i] - cy) * st
                    val v = -(xs[i] - cx) * st + (ys[i] - cy) * ct
                    err += abs(sqrt((u / ra) * (u / ra) + (v / rb) * (v / rb)) - 1f)
                }
                err /= n
                val corners = corners(xs, ys, true, 0.06f * len)
                if (err < 0.09f && corners.size !in 3..5 || err < 0.05f) {
                    if (abs(ra - rb) < 0.1f * max(ra, rb)) { val r = (ra + rb) / 2; ra = r; rb = r }
                    val m = 72
                    val q = FloatArray(2 * (m + 1))
                    for (k in 0..m) {
                        val a = 2 * Math.PI.toFloat() * k / m
                        val u = ra * cos(a); val v = rb * sin(a)
                        q[2 * k] = cx + u * ct - v * st
                        q[2 * k + 1] = cy + u * st + v * ct
                    }
                    return make(*q)
                }
            }
            // 3) 다각형
            val c = corners(xs, ys, true, 0.06f * len)
            if (c.size in 3..8) {
                if (c.size == 4) {
                    // 거의 직사각형이면 반듯하게
                    var axis = true
                    for (k in 0 until 4) {
                        val a = c[k]; val b = c[(k + 1) % 4]
                        val ang = Math.toDegrees(atan2((ys[b] - ys[a]).toDouble(), (xs[b] - xs[a]).toDouble()))
                        val m = ((ang % 90) + 90) % 90
                        if (m > 10 && m < 80) axis = false
                    }
                    if (axis) {
                        var l = Float.MAX_VALUE; var t = Float.MAX_VALUE; var r = -Float.MAX_VALUE; var b = -Float.MAX_VALUE
                        for (k in c) { l = min(l, xs[k]); r = max(r, xs[k]); t = min(t, ys[k]); b = max(b, ys[k]) }
                        return make(l, t, r, t, r, b, l, b, l, t)
                    }
                }
                val q = FloatArray(2 * (c.size + 1))
                for ((k, i) in c.withIndex()) { q[2 * k] = xs[i]; q[2 * k + 1] = ys[i] }
                q[2 * c.size] = xs[c[0]]; q[2 * c.size + 1] = ys[c[0]]
                return make(*q)
            }
            return null
        }
        // 4) 꺾은선(열린 도형): 꼭짓점 몇 개로 잘 설명되면
        val c = corners(xs, ys, false, 0.04f * len)
        if (c.size in 3..6) {
            val q = FloatArray(2 * c.size)
            for ((k, i) in c.withIndex()) { q[2 * k] = xs[i]; q[2 * k + 1] = ys[i] }
            return make(*q)
        }
        return null
    }

    /** 더글러스-포이커로 꼭짓점 찾기. closed면 시작점과 가장 먼 점에서 둘로 나눠 잡는다. 돌려주는 값: 점 번호 */
    private fun corners(xs: FloatArray, ys: FloatArray, closed: Boolean, eps: Float): List<Int> {
        val n = xs.size
        fun dp(a: Int, b: Int, out: MutableList<Int>) {
            var best = -1
            var bd = eps
            for (i in a + 1 until b) {
                val d = segDist(xs[i], ys[i], xs[a], ys[a], xs[b], ys[b])
                if (d > bd) { bd = d; best = i }
            }
            if (best >= 0) {
                dp(a, best, out)
                out.add(best)
                dp(best, b, out)
            }
        }
        if (!closed) {
            val out = mutableListOf(0)
            dp(0, n - 1, out)
            out.add(n - 1)
            return out
        }
        var far = 0
        var fd = 0f
        for (i in 0 until n) {
            val d = hypot(xs[i] - xs[0], ys[i] - ys[0])
            if (d > fd) { fd = d; far = i }
        }
        val out = mutableListOf(0)
        dp(0, far, out)
        out.add(far)
        dp(far, n - 1, out)
        // 시작점이 실제 꼭짓점이 아닐 수 있다: 이웃 꼭짓점과 거의 일직선이면 뺀다
        val res = out.distinct().toMutableList()
        if (res.size >= 4) {
            val a = res[res.size - 1]; val b = res[1]
            if (segDist(xs[0], ys[0], xs[a], ys[a], xs[b], ys[b]) < eps) res.removeAt(0)
        }
        return res
    }
}

package io.github.kimkyum.scan2svg

import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.FileProvider
import java.io.File

/** 화면을 코드로 짜기 위한 작은 도구들(색, 단추, 공유). */
object Ui {
    const val BG = 0xFF141218.toInt()
    const val SURFACE = 0xFF211F26.toInt()
    const val SURFACE2 = 0xFF2B2930.toInt()
    const val ON = 0xFFE6E0E9.toInt()
    const val DIM = 0xFFB0A8B9.toInt()
    const val ACCENT = 0xFFD0BCFF.toInt()
    const val ON_ACCENT = 0xFF381E72.toInt()
    const val DANGER = 0xFFF2B8B5.toInt()

    fun dp(ctx: Context, v: Float) = v * ctx.resources.displayMetrics.density
    fun dpi(ctx: Context, v: Int) = (v * ctx.resources.displayMetrics.density + 0.5f).toInt()

    fun round(color: Int, radius: Float, stroke: Int = 0, strokeW: Int = 0) = GradientDrawable().apply {
        setColor(color); cornerRadius = radius
        if (strokeW > 0) setStroke(strokeW, stroke)
    }

    fun text(ctx: Context, s: String, size: Float = 15f, color: Int = ON, bold: Boolean = false) = TextView(ctx).apply {
        text = s; textSize = size; setTextColor(color)
        if (bold) typeface = Typeface.DEFAULT_BOLD
    }

    /** kind: 0 채움(강조), 1 옅은 채움, 2 테두리 */
    fun button(ctx: Context, label: String, kind: Int = 1, onClick: (View) -> Unit): Button = Button(ctx).apply {
        text = label
        isAllCaps = false
        textSize = 14f
        stateListAnimator = null
        minHeight = 0; minimumHeight = 0; minWidth = 0; minimumWidth = 0
        val r = dp(ctx, 22f)
        val (bg, fg) = when (kind) {
            0 -> ACCENT to ON_ACCENT
            1 -> SURFACE2 to ON
            else -> Color.TRANSPARENT to ACCENT
        }
        background = round(bg, r, DIM, if (kind == 2) dpi(ctx, 1) else 0)
        setTextColor(fg)
        val ph = dpi(ctx, 16); val pv = dpi(ctx, 9)
        setPadding(ph, pv, ph, pv)
        setOnClickListener(onClick)
    }

    /** 도구 단추(눌림 상태 표시) */
    fun toolButton(ctx: Context, label: String, onClick: (View) -> Unit): Button = button(ctx, label, 1, onClick).apply {
        val ph = dpi(ctx, 12); val pv = dpi(ctx, 7)
        setPadding(ph, pv, ph, pv)
        textSize = 14f
    }

    fun setActive(b: View, on: Boolean) {
        val ctx = b.context
        (b as? Button)?.let {
            it.background = round(if (on) ACCENT else SURFACE2, dp(ctx, 18f))
            it.setTextColor(if (on) ON_ACCENT else ON)
        }
    }

    fun row(ctx: Context, gravity: Int = Gravity.CENTER_VERTICAL) = LinearLayout(ctx).apply {
        orientation = LinearLayout.HORIZONTAL; this.gravity = gravity
    }

    fun col(ctx: Context) = LinearLayout(ctx).apply { orientation = LinearLayout.VERTICAL }

    fun lp(w: Int = ViewGroup.LayoutParams.WRAP_CONTENT, h: Int = ViewGroup.LayoutParams.WRAP_CONTENT, weight: Float = 0f,
           margin: Int = 0) = LinearLayout.LayoutParams(w, h, weight).apply { setMargins(margin, margin, margin, margin) }

    fun toast(ctx: Context, s: String) = Toast.makeText(ctx, s, Toast.LENGTH_LONG).show()

    fun choose(ctx: Context, title: String, items: List<String>, pick: (Int) -> Unit) {
        AlertDialog.Builder(ctx).setTitle(title).setItems(items.toTypedArray()) { _, i -> pick(i) }.show()
    }

    fun uriFor(ctx: Context, f: File): Uri = FileProvider.getUriForFile(ctx, ctx.packageName + ".files", f)

    /** 다른 앱으로 보내기·열기 */
    fun share(a: Activity, f: File, mime: String) {
        val i = Intent(Intent.ACTION_SEND).setType(mime)
            .putExtra(Intent.EXTRA_STREAM, uriFor(a, f))
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        a.startActivity(Intent.createChooser(i, "보내기"))
    }

    fun open(a: Activity, f: File, mime: String) {
        val i = Intent(Intent.ACTION_VIEW).setDataAndType(uriFor(a, f), mime)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        try {
            a.startActivity(Intent.createChooser(i, "열 앱 고르기"))
        } catch (e: Exception) {
            toast(a, "열 수 있는 앱이 없습니다")
        }
    }

    /** 기기의 원하는 폴더에 저장(파일 앱 화면이 뜬다). 결과는 onActivityResult의 requestCode로 받는다. */
    fun createDocument(a: Activity, name: String, mime: String, requestCode: Int) {
        val i = Intent(Intent.ACTION_CREATE_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType(mime)
            .putExtra(Intent.EXTRA_TITLE, name)
        a.startActivityForResult(i, requestCode)
    }

    fun copyTo(a: Activity, src: File, uri: Uri) {
        try {
            a.contentResolver.openOutputStream(uri)?.use { o -> src.inputStream().use { it.copyTo(o) } }
            toast(a, "저장했습니다")
        } catch (e: Exception) {
            toast(a, "저장 실패: $e")
        }
    }
}

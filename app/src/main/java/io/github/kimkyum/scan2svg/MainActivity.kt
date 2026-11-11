package io.github.kimkyum.scan2svg

import android.content.ActivityNotFoundException
import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.ImageView
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.FileProvider
import java.io.File
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity() {
    private lateinit var status: TextView
    private lateinit var logView: TextView
    private lateinit var fontInfo: TextView
    private lateinit var preview: ImageView
    private lateinit var previewCaption: TextView
    private lateinit var progress: ProgressBar
    private lateinit var resultButtons: View
    private lateinit var inputButtons: List<Button>

    private val worker = Executors.newSingleThreadExecutor()
    private var busy = false
    private var cameraFile: File? = null
    private var lastSvg: File? = null

    private val pickImage = registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri != null) startFromUri(uri)
    }
    private val takePicture = registerForActivityResult(ActivityResultContracts.TakePicture()) { ok ->
        val f = cameraFile
        if (ok && f != null && f.length() > 0) start(f)
    }
    private val pickFonts = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (!uris.isNullOrEmpty()) importFonts(uris)
    }
    private val saveSvg = registerForActivityResult(ActivityResultContracts.CreateDocument("image/svg+xml")) { uri ->
        val src = lastSvg
        if (uri != null && src != null) {
            try {
                contentResolver.openOutputStream(uri)?.use { o -> src.inputStream().use { it.copyTo(o) } }
                toast("저장했습니다")
            } catch (e: Exception) {
                toast("저장 실패: ${e.message}")
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        status = findViewById(R.id.status)
        logView = findViewById(R.id.log)
        fontInfo = findViewById(R.id.fontInfo)
        preview = findViewById(R.id.preview)
        previewCaption = findViewById(R.id.previewCaption)
        progress = findViewById(R.id.progress)
        resultButtons = findViewById(R.id.resultButtons)
        val btnPick = findViewById<Button>(R.id.btnPick)
        val btnCamera = findViewById<Button>(R.id.btnCamera)
        val btnSample = findViewById<Button>(R.id.btnSample)
        val btnFonts = findViewById<Button>(R.id.btnFonts)
        inputButtons = listOf(btnPick, btnCamera, btnSample, btnFonts)

        btnPick.setOnClickListener { pickImage.launch("image/*") }
        btnCamera.setOnClickListener { openCamera() }
        btnSample.setOnClickListener {
            val f = File(cacheDir, "sample.jpg")
            assets.open("sample_photo.jpg").use { i -> f.outputStream().use { i.copyTo(it) } }
            start(f)
        }
        btnFonts.setOnClickListener { pickFonts.launch(arrayOf("*/*")) }
        findViewById<Button>(R.id.btnSave).setOnClickListener { saveSvg.launch("scan.svg") }
        findViewById<Button>(R.id.btnShare).setOnClickListener { shareSvg(Intent.ACTION_SEND) }
        findViewById<Button>(R.id.btnOpen).setOnClickListener { shareSvg(Intent.ACTION_VIEW) }

        updateFontInfo()
        handleShareIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleShareIntent(intent)
    }

    /** 다른 앱에서 '공유'로 넘어온 사진 */
    private fun handleShareIntent(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND || intent.type?.startsWith("image/") != true) return
        val uri: Uri? = if (Build.VERSION.SDK_INT >= 33) {
            intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        } else {
            @Suppress("DEPRECATION")
            intent.getParcelableExtra(Intent.EXTRA_STREAM)
        }
        if (uri != null) startFromUri(uri)
    }

    private fun openCamera() {
        try {
            val f = File(File(cacheDir, "camera").apply { mkdirs() }, "photo.jpg")
            f.delete()
            cameraFile = f
            takePicture.launch(FileProvider.getUriForFile(this, "$packageName.files", f))
        } catch (e: ActivityNotFoundException) {
            toast("카메라 앱을 찾지 못했습니다")
        }
    }

    private fun startFromUri(uri: Uri) {
        try {
            val f = File(cacheDir, "input.jpg")
            contentResolver.openInputStream(uri)?.use { i -> f.outputStream().use { i.copyTo(it) } }
            start(f)
        } catch (e: Exception) {
            toast("사진을 읽지 못했습니다: ${e.message}")
        }
    }

    private fun start(input: File) {
        if (busy) {
            toast("변환 중입니다")
            return
        }
        setBusy(true)
        logView.text = ""
        status.setText(R.string.working)
        preview.visibility = View.GONE
        previewCaption.visibility = View.GONE
        resultButtons.visibility = View.GONE
        worker.execute {
            val r = Pipeline.run(this, input) { line -> runOnUiThread { logView.append(line + "\n") } }
            runOnUiThread { onFinished(r) }
        }
    }

    private fun onFinished(r: Pipeline.Result) {
        setBusy(false)
        if (!r.ok) {
            status.text = "변환하지 못했습니다"
            logView.append("\n" + r.message)
            return
        }
        lastSvg = r.svg
        val s = r.stats
        val counts = s?.optJSONObject("counts")
        val sb = StringBuilder("완료")
        if (counts != null) {
            sb.append(" — 글자 ${counts.optInt("text_lines")}줄, 도형 ${counts.optInt("shapes")}개, ")
            sb.append("곡선 ${counts.optInt("illustration_paths")}개, 이미지 ${counts.optInt("images")}개")
        }
        if (s != null) sb.append(" (${s.optDouble("total_sec").toInt()}초)")
        if (s?.optBoolean("low_resolution") == true) sb.append("\n글자가 작게 찍혀 닮은 글자를 잘못 읽었을 수 있습니다. 더 가까이서 찍어 보세요.")
        status.text = sb
        if (r.compare.exists()) {
            val opt = BitmapFactory.Options().apply { inSampleSize = 2 }
            BitmapFactory.decodeFile(r.compare.absolutePath, opt)?.let {
                preview.setImageBitmap(it)
                preview.visibility = View.VISIBLE
                previewCaption.visibility = View.VISIBLE
            }
        }
        resultButtons.visibility = View.VISIBLE
    }

    private fun shareSvg(action: String) {
        val f = lastSvg ?: return
        val uri = FileProvider.getUriForFile(this, "$packageName.files", f)
        val i = Intent(action).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        if (action == Intent.ACTION_SEND) {
            i.type = "image/svg+xml"
            i.putExtra(Intent.EXTRA_STREAM, uri)
        } else {
            i.setDataAndType(uri, "image/svg+xml")
        }
        try {
            startActivity(Intent.createChooser(i, null))
        } catch (e: ActivityNotFoundException) {
            toast("SVG를 열 수 있는 앱이 없습니다")
        }
    }

    private fun importFonts(uris: List<Uri>) {
        val dir = Pipeline.userFontDir(this)
        var n = 0
        for (uri in uris) {
            val name = displayName(uri) ?: continue
            if (!name.lowercase().matches(Regex(".*\\.(ttf|otf|ttc|otc)$"))) continue
            try {
                contentResolver.openInputStream(uri)?.use { i -> File(dir, name).outputStream().use { i.copyTo(it) } }
                n++
            } catch (e: Exception) {
                // 읽지 못한 파일은 건너뛴다
            }
        }
        toast(if (n > 0) "글꼴 ${n}개를 추가했습니다" else "글꼴 파일(.ttf .otf .ttc)을 골라 주세요")
        updateFontInfo()
    }

    private fun displayName(uri: Uri): String? =
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
            if (c.moveToFirst()) c.getString(0) else null
        }

    private fun updateFontInfo() {
        val n = Pipeline.userFontDir(this).listFiles()?.size ?: 0
        fontInfo.text = "글꼴은 기기 글꼴, 앱에 든 Noto Sans KR·Noto Serif KR(굵기별), 추가한 글꼴 ${n}개 중에서 찾습니다"
    }

    private fun setBusy(b: Boolean) {
        busy = b
        progress.visibility = if (b) View.VISIBLE else View.GONE
        inputButtons.forEach { it.isEnabled = !b }
        if (b) window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        else window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_LONG).show()
}
